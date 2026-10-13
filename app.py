"""Run with: python -m streamlit run app.py"""
import sqlite3
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from pmo.config import db_path, grades
from pmo.service import ConflictError, Service, ValidationError, validate_assignment, validate_person

st.set_page_config(page_title="CoE 인력관리", page_icon="👥", layout="wide")
st.title("CoE 인력관리")
st.caption("인력과 투입 정보를 등록하고 변경 이력을 확인하세요.")
today = datetime.now(ZoneInfo("Asia/Seoul")).date()
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
    "id": "고유 ID", "name": "이름", "company": "업체명", "category": "분류",
    "person_id": "인력 ID", "project": "과제명", "start_date": "시작일", "end_date": "종료일",
    "grade": "등급", "sales_rate": "매출단가", "purchase_rate": "매입단가",
    "rate_unit": "단가 기준 단위", "version": "버전"
}


def show_table(rows: list[dict], fields: list[str]) -> None:
    if rows:
        st.dataframe(
            [{LABELS.get(k, k): r.get(k) for k in fields} for r in rows],
            use_container_width=True, hide_index=True
        )
    else:
        st.info("등록된 정보가 없습니다.")


def stage(kind: str, data: dict, current: dict | None) -> None:
    try:
        clean = validate_person(data) if kind == "person" else validate_assignment(data)
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
    st.table([{
        "항목": LABELS[k], "기존 값": current.get(k, "") if current else "",
        "저장할 값": value
    } for k, value in data.items()])
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
            if pending["kind"] == "person":
                result_id = service.save_person(data, **args)
            else:
                result_id = service.save_assignment(
                    data, assignment_id=current["id"] if current else None,
                    overlap_reviewed=reviewed, **args
                )
            st.session_state.pop("pending", None)
            st.session_state["saved_message"] = f"저장했습니다. ID: {result_id}"
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
    query = st.text_input("이름·ID·업체 검색", key="person_search").strip().casefold()
    filtered = [r for r in people if query in
                " ".join(str(r[k]) for k in ("id", "name", "company")).casefold()]
    show_table(filtered, ["id", "name", "company", "category"])
    options = [None] + [r["id"] for r in people]
    by_id = {r["id"]: r for r in people}
    target = st.selectbox(
        "등록 또는 수정할 인력", options,
        format_func=lambda i: "신규 등록" if i is None else f"{by_id[i]['name']} · {i}",
        key="person_target"
    )
    current = by_id.get(target)
    context = ("person", target)
    if st.session_state.get("edit_context") != context:
        st.session_state.pop("pending", None)
        st.session_state["edit_context"] = context
    st.subheader("인력 수정" if current else "인력 등록")
    with st.form(f"person_form_{target}"):
        person_id = st.text_input("인력 ID", value=current["id"] if current else "",
                                  disabled=bool(current), key=f"pid_{target}")
        name = st.text_input("이름", value=current["name"] if current else "", key=f"name_{target}")
        company = st.text_input("업체명 (선택)", value=current["company"] if current else "",
                                key=f"company_{target}")
        categories = ["정", "협력", "컨설팅"]
        category = st.selectbox("분류", categories,
                                index=categories.index(current["category"]) if current else 0)
        if st.form_submit_button("미리보기", key="preview_person"):
            stage("person", dict(id=person_id, name=name, company=company, category=category), current)
    confirm()

elif page == "투입 관리":
    st.subheader("투입 목록")
    people = service.people()
    by_person = {r["id"]: r for r in people}
    person_id = st.selectbox(
        "인력 선택", [r["id"] for r in people],
        format_func=lambda i: f"{by_person[i]['name']} · {i}", key="assignment_person"
    ) if people else None
    if person_id is None:
        st.info("먼저 인력을 등록하세요.")
        st.stop()
    rows = service.assignments(person_id)
    show_table(rows, ["id", "project", "start_date", "end_date", "grade",
                      "sales_rate", "purchase_rate", "rate_unit"])
    by_id = {r["id"]: r for r in rows}
    target = st.selectbox(
        "등록 또는 수정할 투입", [None] + list(by_id),
        format_func=lambda i: "신규 등록" if i is None else f"{by_id[i]['project']} · {i}",
        key=f"assignment_target_{person_id}"
    )
    current = by_id.get(target)
    context = ("assignment", person_id, target)
    if st.session_state.get("edit_context") != context:
        st.session_state.pop("pending", None)
        st.session_state["edit_context"] = context
    st.subheader("투입 수정" if current else "투입 등록")
    st.caption("단가에는 쉼표 없이 숫자를 입력하세요. MM 계산은 아직 미설정입니다.")
    with st.form(f"assignment_form_{person_id}_{target}"):
        project = st.text_input("과제명", value=current["project"] if current else "",
                                key=f"project_{person_id}_{target}")
        a, b = st.columns(2)
        start = a.date_input("시작일", value=date.fromisoformat(current["start_date"]) if current else today,
                             min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
        end = b.date_input("종료일", value=date.fromisoformat(current["end_date"]) if current else today,
                           min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
        allowed = list(grades())
        if current and current["grade"] not in allowed:
            st.warning("기존 등급이 현재 허용 목록에 없습니다. 새 등급을 선택하세요.")
        grade = st.selectbox("등급", allowed,
                             index=allowed.index(current["grade"]) if current and current["grade"] in allowed else 0)
        sales = st.text_input("매출단가", value=current["sales_rate"] if current else "",
                              key=f"sales_{person_id}_{target}")
        purchase = st.text_input("매입단가", value=current["purchase_rate"] if current else "",
                                 key=f"purchase_{person_id}_{target}")
        unit = st.text_input("단가 기준 단위 (예: 원/MM, 원/일)",
                             value=current["rate_unit"] if current else "",
                             key=f"unit_{person_id}_{target}")
        if st.form_submit_button("미리보기", key="preview_assignment"):
            stage("assignment", dict(person_id=person_id, project=project, start_date=start,
                                     end_date=end, grade=grade, sales_rate=sales,
                                     purchase_rate=purchase, rate_unit=unit), current)
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
