"""Run with: python -m streamlit run app.py"""
import sqlite3
import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

import streamlit as st

from pmo.config import db_path, grades
from pmo.mm import calculate_full_time
from pmo.service import (
    ConflictError, Service, ValidationError,
    validate_assignment, validate_intake, validate_person,
)

st.set_page_config(page_title="CoE 인력관리", page_icon="👥", layout="wide")
st.title("CoE 인력관리")
st.caption("인력과 투입 정보를 등록하고 변경 이력을 확인하세요.")
try:
    service = Service(db_path())
except (OSError, sqlite3.Error, RuntimeError) as exc:
    st.error(f"DB를 열 수 없습니다: {exc}")
    st.stop()

actor = st.sidebar.text_input("변경 주체", value="로컬 담당자")
st.sidebar.caption("이 값은 이력 기록용이며 로그인 인증 정보가 아닙니다.")
page = st.sidebar.radio("메뉴", ["인력 관리", "투입 관리", "변경 이력"])
if st.session_state.get("page") != page:
    st.session_state.pop("pending", None)
    st.session_state["page"] = page
if message := st.session_state.pop("saved_message", None):
    st.success(message)

LABELS = {
    "id": "내부 관리번호", "account_id": "계정 ID",
    "name": "이름", "company": "업체명", "category": "분류",
    "person_id": "내부 관리번호", "project": "과제명", "start_date": "시작일", "end_date": "종료일",
    "grade": "등급", "sales_rate": "매출단가", "purchase_rate": "매입단가",
    "rate_unit": "단가 기준 단위", "version": "버전",
    "assignment_type": "투입 Type", "mm": "투입 MM"
}


def mm_fields(start, end, current, key):
    types = ["Full time", "Part time"]
    existing = current.get("assignment_type") if current else "Full time"
    mode = st.selectbox("투입 Type", types,
                        index=types.index(existing) if existing in types else None,
                        key=f"type_{key}")
    manual = None
    if mode == "Part time":
        manual = st.text_input("투입 MM (수동)",
                               value=(current.get("mm") or "") if current and existing == "Part time" else "",
                               key=f"manual_mm_{key}")
        st.caption("입력한 MM는 선택한 투입 기간 전체의 MM입니다.")
    elif mode == "Full time":
        if start and end:
            try:
                detail = calculate_full_time(start, end)
                st.metric("자동 계산 투입 MM", f"{Decimal(detail['mm']):.6f}")
                st.table([{"월": r["month"], "투입 근무일": r["assigned_days"],
                           "월 전체 근무일": r["working_days"],
                           "MM": f"{r['assigned_days']} ÷ {r['working_days']} = {Decimal(r['mm']):.6f}"}
                          for r in detail["months"]])
                with st.expander("계산에서 제외한 공휴일 확인"):
                    excluded = [d for r in detail["months"] for d in r["excluded_holidays"]]
                    st.write(excluded or "해당 월에 평일 공휴일이 없습니다.")
                st.caption("시작·종료일 포함 · 토·일요일 및 대한민국 공휴일(대체공휴일 포함) 제외")
            except ValueError as exc:
                st.error(str(exc))
        else:
            st.info("시작일과 종료일을 입력하면 투입 MM를 자동 계산합니다.")
    return mode, manual


def invalidate_preview(kind, data):
    pending = st.session_state.get("pending")
    if not pending or pending["kind"] != kind:
        return
    try:
        validators = {"intake": validate_intake, "person": validate_person, "assignment": validate_assignment}
        if validators[kind](data) == pending["data"] and actor.strip() == pending["actor"]:
            return
    except ValidationError:
        pass
    st.session_state.pop("pending", None)


def show_table(rows: list[dict], fields: list[str]) -> None:
    if rows:
        st.dataframe(
            [{LABELS.get(k, k): r.get(k) if r.get(k) is not None else "미입력" for k in fields} for r in rows],
            use_container_width=True, hide_index=True
        )
    else:
        st.info("등록된 정보가 없습니다.")


def stage(kind: str, data: dict, current: dict | None) -> None:
    try:
        if kind == "intake":
            clean = validate_intake(data)
        elif kind == "person":
            clean = validate_person(data)
        else:
            clean = validate_assignment(data)
        if not actor.strip():
            raise ValidationError("변경 주체를 입력하세요.")
        st.session_state["pending"] = {
            "kind": kind, "data": clean, "current": current,
            "token": str(uuid4()), "actor": actor.strip()
        }
    except ValidationError as exc:
        st.session_state.pop("pending", None)
        st.error(str(exc))


def confirm() -> None:
    pending = st.session_state.get("pending")
    if not pending:
        return
    st.divider()
    st.subheader("저장 전 확인")
    data, current = pending["data"], pending["current"]
    st.caption(f"변경 주체: {pending['actor']} · {'수정' if current else '신규 등록'}")
    display_data = dict(data["person"], **data["assignment"]) if pending["kind"] == "intake" else data
    st.table([{
        "항목": LABELS[k], "기존 값": current.get(k, "") if current else "",
        "저장할 값": value if value is not None else "미입력"
    } for k, value in display_data.items() if k != "mm_details"])
    if display_data.get("mm_details"):
        with st.expander("저장할 MM 산정 근거"):
            st.json(display_data["mm_details"])
    if pending["kind"] == "intake":
        st.caption("내부 관리번호는 저장 시 자동 생성됩니다. 계정 ID와 과제·단가는 나중에 보완할 수 있습니다.")
        similar = [p for p in service.people() if p["name"] == data["person"]["name"]
                   and p["company"] == data["person"]["company"]]
        if similar:
            st.warning("같은 이름·업체의 인력이 이미 있습니다. 기존 인력의 추가 투입이라면 투입 관리에서 등록하세요.")
            show_table(similar, ["id", "name", "company", "account_id"])
    reviewed = False
    if pending["kind"] == "assignment":
        overlaps = service.overlaps(data, current["id"] if current else None)
        if overlaps:
            st.warning("다른 투입 기간과 겹칩니다. 중복 허용 기준은 미확정이므로 검토가 필요합니다.")
            show_table(overlaps, ["id", "project", "start_date", "end_date"])
            reviewed = st.checkbox(
                "중복 투입 내용을 검토했습니다.", key="review_" + pending["token"]
            )
    left, right = st.columns(2)
    if left.button("확인 후 저장", type="primary", key="confirm"):
        try:
            args = dict(token=pending["token"], actor=pending["actor"],
                        expected_version=current["version"] if current else None)
            if pending["kind"] == "intake":
                result_id = service.save_intake(data, token=pending["token"], actor=pending["actor"])
            elif pending["kind"] == "person":
                result_id = service.save_person(data, **args)
            else:
                result_id = service.save_assignment(
                    data, assignment_id=current["id"] if current else None,
                    overlap_reviewed=reviewed, **args
                )
            st.session_state.pop("pending", None)
            st.session_state["saved_message"] = f"저장했습니다. 내부 관리번호: {result_id}"
            st.rerun()
        except (ValidationError, ConflictError) as exc:
            st.error(str(exc))
        except sqlite3.Error:
            st.error("DB 저장에 실패했습니다. 입력은 유지됩니다. 다시 시도하세요.")
    if right.button("취소", key="cancel"):
        st.session_state.pop("pending", None)
        st.rerun()


if page == "인력 관리":
    st.subheader("인력 목록")
    people = service.people()
    query = st.text_input("이름·업체·계정 ID·관리번호 검색", key="person_search").strip().casefold()
    latest = {a["person_id"]: a for a in service.assignments()}
    filtered = [r for r in people if query in
                " ".join(str(r.get(k) or "") for k in ("id", "name", "company", "account_id")).casefold()]
    overview = [dict(r, **{k: latest.get(r["id"], {}).get(k)
                          for k in ("grade", "start_date", "end_date", "assignment_type", "mm")}) for r in filtered]
    show_table(overview, ["name", "company", "category", "account_id", "grade", "start_date", "end_date", "assignment_type", "mm", "id"])
    st.caption("등급·기간은 시작일이 가장 최근인 투입 건을 표시합니다. 전체 투입은 투입 관리에서 확인하세요.")
    options = [None] + [r["id"] for r in people]
    by_id = {r["id"]: r for r in people}
    target = st.selectbox(
        "등록 또는 수정할 인력", options,
        format_func=lambda i: "신규 등록" if i is None else f"{by_id[i]['name']} · {by_id[i]['company']} · {i}",
        key="person_target"
    )
    current = by_id.get(target)
    context = ("person", target)
    if st.session_state.get("edit_context") != context:
        st.session_state.pop("pending", None)
        st.session_state["edit_context"] = context
    st.subheader("인력 수정 / 계정 ID 보완" if current else "신규 투입 대상 등록")
    if current:
        st.caption(f"내부 관리번호: {current['id']} · 계정 ID를 입력해도 관리번호와 투입 연결은 유지됩니다.")
    else:
        st.caption("이름·업체·분류·등급·투입 기간만으로 등록할 수 있습니다. 계정 ID는 발급 후 입력하세요.")
    with st.container():
        name = st.text_input("이름", value=current["name"] if current else "", key=f"name_{target}")
        company = st.text_input("업체명", value=current["company"] if current else "",
                                key=f"company_{target}")
        categories = ["정", "협력", "컨설팅"]
        category = st.selectbox("분류", categories,
                                index=categories.index(current["category"]) if current else None)
        account_id = st.text_input("계정 ID (발급 후 입력 / 선택)",
                                    value=(current["account_id"] or "") if current else "",
                                    key=f"account_{target}")
        if not current:
            grade = st.selectbox("투입 등급", list(grades()), index=None, key="intake_grade")
            a, b = st.columns(2)
            start = a.date_input("투입 시작일", value=None, key="intake_start",
                                 min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
            end = b.date_input("투입 종료일", value=None, key="intake_end",
                               min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
            mode, manual_mm = mm_fields(start, end, None, "intake")
        person_data = dict(name=name, company=company, category=category, account_id=account_id)
        kind = "person" if current else "intake"
        input_data = dict(person_data, id=current["id"]) if current else dict(
            person=person_data, assignment=dict(grade=grade, start_date=start, end_date=end,
                                                assignment_type=mode, mm=manual_mm))
        invalidate_preview(kind, input_data)
        if st.button("미리보기", key="preview_person"):
            stage(kind, input_data, current)
    confirm()

elif page == "투입 관리":
    st.subheader("투입 목록")
    people = service.people()
    by_person = {r["id"]: r for r in people}
    person_id = st.selectbox(
        "인력 선택", [r["id"] for r in people],
        format_func=lambda i: f"{by_person[i]['name']} · {by_person[i]['company']} · {i}", key="assignment_person"
    ) if people else None
    if person_id is None:
        st.info("먼저 인력을 등록하세요.")
        st.stop()
    rows = service.assignments(person_id)
    show_table(rows, ["id", "project", "start_date", "end_date", "grade",
                      "assignment_type", "mm", "sales_rate", "purchase_rate", "rate_unit"])
    by_id = {r["id"]: r for r in rows}
    target = st.selectbox(
        "등록 또는 수정할 투입", [None] + list(by_id),
        format_func=lambda i: "신규 등록" if i is None else f"{by_id[i]['project'] or '과제 미입력'} · {by_id[i]['start_date']} · {i}",
        key=f"assignment_target_{person_id}"
    )
    current = by_id.get(target)
    context = ("assignment", person_id, target)
    if st.session_state.get("edit_context") != context:
        st.session_state.pop("pending", None)
        st.session_state["edit_context"] = context
    st.subheader("투입 수정" if current else "투입 등록")
    st.caption("과제·단가는 나중에 보완할 수 있습니다. 단가 입력 시 기준 단위도 입력하세요.")
    with st.container():
        project = st.text_input("과제명 (선택)", value=(current["project"] or "") if current else "",
                                key=f"project_{person_id}_{target}")
        a, b = st.columns(2)
        start = a.date_input("시작일", value=date.fromisoformat(current["start_date"]) if current else None,
                             min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
        end = b.date_input("종료일", value=date.fromisoformat(current["end_date"]) if current else None,
                           min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
        allowed = list(grades())
        if current and current["grade"] not in allowed:
            st.warning("기존 등급이 현재 허용 목록에 없습니다. 새 등급을 선택하세요.")
        grade = st.selectbox("등급", allowed,
                             index=allowed.index(current["grade"]) if current and current["grade"] in allowed else None)
        mode, manual_mm = mm_fields(start, end, current, f"{person_id}_{target}")
        sales = st.text_input("매출단가 (선택)", value=(current["sales_rate"] or "") if current else "",
                              key=f"sales_{person_id}_{target}")
        purchase = st.text_input("매입단가 (선택)", value=(current["purchase_rate"] or "") if current else "",
                                 key=f"purchase_{person_id}_{target}")
        unit = st.text_input("단가 기준 단위 (예: 원/MM, 원/일)",
                             value=(current["rate_unit"] or "") if current else "",
                             key=f"unit_{person_id}_{target}")
        input_data = dict(person_id=person_id, project=project, start_date=start,
                          end_date=end, grade=grade, sales_rate=sales,
                          purchase_rate=purchase, rate_unit=unit, assignment_type=mode, mm=manual_mm)
        invalidate_preview("assignment", input_data)
        if st.button("미리보기", key="preview_assignment"):
            stage("assignment", input_data, current)
    confirm()

else:
    st.subheader("변경 이력")
    for row in service.history():
        with st.expander(f"{row['changed_at']} · {row['entity_id']} · {row['actor']}"):
            st.write("대상:", row["entity"])
            st.write("변경 전")
            if row["before_json"] is None:
                st.caption("신규 등록")
            else:
                st.json(row["before_json"])
            st.write("변경 후")
            st.json(row["after_json"])
    if not service.history():
        st.info("변경 이력이 없습니다.")
