"""Run with: python -m streamlit run app.py"""
import os
import sqlite3
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import uuid4

import pandas as pd
import streamlit as st

from pmo.config import CONTRACT_ALERT_DAYS, db_path, grades
from pmo.mm import calculate_full_time
from pmo.service import (
    ConflictError, Service, ValidationError,
    validate_assignment, validate_contract, validate_intake, validate_person,
)

from ui import theme

st.set_page_config(page_title="CoE 인력관리", page_icon=":material/groups:", layout="wide")
theme.apply()
try:
    service = Service(db_path())
except (OSError, sqlite3.Error, RuntimeError) as exc:
    st.error(f"DB를 열 수 없습니다: {exc}")
    st.stop()

_people_count = len(service.people())
# 앱 실행(재실행) 시마다 미처리 계약 만료 대상을 다시 확인한다.
try:
    pending_alerts = service.pending_alerts()
except sqlite3.Error:
    pending_alerts = []
    st.sidebar.error("계약 만료 알림을 확인하지 못했습니다.")
theme.sidebar_brand()
page = theme.sidebar_nav(len(pending_alerts))
actor = st.sidebar.text_input("변경 주체", value="로컬 담당자")
st.sidebar.caption("이 값은 이력 기록용이며 로그인 인증 정보가 아닙니다.")
# 시연 DB와 업무 DB를 헷갈리지 않도록 지금 연결된 DB를 항상 보여준다.
if _people_count == 0:
    st.sidebar.info("등록된 인력이 없습니다. 시연하려면 PMO_DB_PATH를 data\\demo.db로 지정해 실행하세요.")
theme.sidebar_footer(service.path.name, _people_count, os.environ.get("PMO_OLLAMA_MODEL", "qwen3:8b"))
theme.page_header(service.clock(), "무엇을 도와드릴까요?" if page == "홈" else
                  page if theme.GROUP_OF[page] == page else f"{theme.GROUP_OF[page]} · {page}")
if pending_alerts and page != "계약 종료 알림":
    st.warning(f"계약 종료 검토 대상이 {len(pending_alerts)}건 있습니다. "
               "왼쪽 메뉴의 '계약 종료 알림'에서 확인하세요.")
if st.session_state.get("page") != page:
    st.session_state.pop("pending", None)
    st.session_state.pop("ck_pending", None)
    st.session_state["page"] = page
if message := st.session_state.pop("saved_message", None):
    st.success(message)

LABELS = {
    "id": "내부 관리번호", "account_id": "계정 ID",
    "name": "이름", "company": "업체명", "category": "분류",
    "person_id": "내부 관리번호", "project": "과제명", "start_date": "시작일", "end_date": "종료일",
    "grade": "등급", "sales_rate": "매출단가", "purchase_rate": "매입단가",
    "rate_unit": "단가 기준 단위", "version": "버전",
    "assignment_type": "투입 Type", "mm": "투입 MM", "revenue_project": "매출귀속 프로젝트",
    "revenue_team": "매출귀속팀", "job_role": "직무",
    "assignment_id": "연결 투입", "contract_no": "계약번호", "status": "계약 상태",
    "previous_contract_id": "이전(연장 전) 계약", "note": "메모",
    "label": "만료 상태", "has_successor": "연장 계약 등록", "notify_from": "알림 시작일",
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
                st.metric("자동 계산 투입 MM", detail["mm"])
                st.caption(f"구매시스템 방식: 월별 근무일 비율 합계 {Decimal(detail['mm_exact']):.6f}를 "
                           "소수 셋째 자리에서 반올림")
                st.table([{"월": r["month"], "투입 근무일": r["assigned_days"],
                           "월 전체 근무일": r["working_days"],
                           "MM": f"{r['assigned_days']} ÷ {r['working_days']} = {Decimal(r['mm']):.6f}"}
                          for r in detail["months"]])
                with st.expander("계산에서 제외한 공휴일 확인"):
                    excluded = [d for r in detail["months"] for d in r["excluded_holidays"]]
                    st.write(excluded or "해당 월에 평일 공휴일이 없습니다.")
                st.caption("시작·종료일 포함 · 토·일요일 및 대한민국 공휴일(대체공휴일 포함) 제외 · "
                           "노동절·제헌절·지방선거일은 구매시스템과 같이 근무일로 셈")
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
        validators = {"intake": validate_intake, "person": validate_person,
                      "assignment": validate_assignment, "contract": validate_contract}
        if validators[kind](data) == pending["data"] and actor.strip() == pending["actor"]:
            return
    except ValidationError:
        pass
    st.session_state.pop("pending", None)


def person_label(person: dict) -> str:
    """선택 목록에 보여줄 인력 이름표. 내부 관리번호 대신 업체·직무·계정 ID로 구분한다."""
    return " · ".join(str(person[k]) for k in ("name", "company", "job_role", "account_id") if person.get(k))


def render_monthly_mm() -> None:
    """전체 투입 · 월별 MM 표 (회계연도 4월~다음 해 3월, 10/20 담당자 요청)."""
    st.subheader("전체 투입 · 월별 MM")
    current_fy = Service.fiscal_year_of(service.clock())
    years = {current_fy} | {Service.fiscal_year_of(date.fromisoformat(a[k]))
                            for a in service.assignments() for k in ("start_date", "end_date")}
    left, right = st.columns([1, 2])
    fy = left.selectbox("회계연도", sorted(years), index=sorted(years).index(current_fy),
                        format_func=lambda y: f"{y}년 4월 ~ {y + 1}년 3월", key="mm_fiscal_year")
    keyword = right.text_input("이름·업체·과제 검색", key="mm_search").strip()
    rows = [r for r in service.monthly_mm(fy)
            if not keyword or any(keyword in (r[k] or "") for k in ("name", "company", "project"))]
    if not rows:
        st.info("이 회계연도에 걸친 투입이 없습니다.")
        return
    months = Service.fiscal_months(fy)
    month_label = {m: f"{int(m[5:])}월" for m in months}

    def number(value) -> str:
        """MM 칸 글자. 값이 없으면(투입 없는 달, Part time 월별 값) 빈 문자열로 둬서 'None'이 보이지 않게 한다.

        Streamlit 표는 빈 값(None/NaN)을 회색 'None'으로 그리므로 숫자 대신 '0.750' 같은 글자로 넣는다.
        """
        if value in (None, ""):
            return ""
        return format(Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP), "f")

    # 열 순서(10/20 담당자): 이름, 업체명, 4월~3월, 투입 MM, 투입 Type, 투입 시작일, 투입 종료일
    table = [{"이름": r["name"], "업체명": r["company"],
              **{month_label[m]: number(r["months"][m]) for m in months},
              "투입 MM": number(r["mm"]), "투입 Type": r["assignment_type"],
              "투입 시작일": r["start_date"], "투입 종료일": r["end_date"]} for r in rows]
    # 월 합계도 그 달 투입이 없으면 0.000 대신 빈 칸
    totals = {m: sum(Decimal(r["months"][m]) for r in rows if r["months"][m])
              if any(r["months"][m] for r in rows) else None for m in months}
    table.append({"이름": "월 합계 (Full time)", **{month_label[m]: number(totals[m]) for m in months}})
    numbers = [month_label[m] for m in months] + ["투입 MM"]
    frame = pd.DataFrame(table, columns=list(table[0])).fillna("")   # 합계 줄의 빈 칸도 빈 문자열
    styled = frame.style.apply(lambda row: ["font-weight: 600; background-color: #F1F4F9"] * len(row)
                               if row["이름"] == "월 합계 (Full time)" else [""] * len(row), axis=1)
    try:   # 숫자 글자는 오른쪽 정렬 (alignment를 모르는 Streamlit 버전이면 기본 정렬)
        config = {c: st.column_config.TextColumn(alignment="right") for c in numbers}
    except TypeError:
        config = None
    st.dataframe(styled, hide_index=True, use_container_width=True, column_config=config)
    st.caption(f"{fy}년 4월 ~ {fy + 1}년 3월 · 월별 MM은 구매시스템 방식(그 달 투입 근무일 ÷ 그 달 전체 근무일)을 "
               "달마다 소수 셋째 자리로 반올림한 값입니다. '투입 MM'은 합계를 한 번 반올림하므로 월별 값의 합과 "
               "0.001 차이가 날 수 있습니다. Part time은 기간 전체 MM만 입력하므로 월별 칸이 비어 있습니다.")
    for r in rows:
        if r["note"].startswith("계산 실패"):
            st.warning(f"{r['name']} · {r['project'] or '과제 미입력'}: {r['note']}")


def person_form(current: dict | None) -> None:
    """신규 인력 등록(current=None: 인력 + 첫 투입) 또는 기존 인력 수정·계정 ID 보완."""
    target = current["id"] if current else None
    context = ("person", target)
    if st.session_state.get("edit_context") != context:
        st.session_state.pop("pending", None)
        st.session_state["edit_context"] = context
    st.subheader("인력 수정 / 계정 ID 보완" if current else "신규 투입 대상 등록")
    if current:
        st.caption("계정 ID를 입력해도 이 사람의 투입·계약 연결은 그대로 유지됩니다.")
    else:
        st.caption("이름·업체·분류·등급·투입 기간만으로 등록할 수 있습니다. 계정 ID는 발급 후 입력하세요. "
                   "이미 등록된 인력의 추가 투입은 '과제 투입 관리'에서 등록하세요.")
    name = st.text_input("이름", value=current["name"] if current else "", key=f"name_{target}")
    company = st.text_input("업체명", value=current["company"] if current else "", key=f"company_{target}")
    categories = ["정", "협력", "컨설팅"]
    category = st.selectbox("분류", categories, key=f"category_{target}",
                            index=categories.index(current["category"]) if current else None)
    job_role = st.text_input("직무 (선택)", value=(current.get("job_role") or "") if current else "",
                             key=f"job_{target}")
    account_id = st.text_input("계정 ID (발급 후 입력 / 선택)",
                               value=(current["account_id"] or "") if current else "", key=f"account_{target}")
    if not current:
        grade = st.selectbox("투입 등급", list(grades()), index=None, key="intake_grade",
                             placeholder="선택 (컨설팅은 비워 둘 수 있음)")
        a, b = st.columns(2)
        start = a.date_input("투입 시작일", value=None, key="intake_start",
                             min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
        end = b.date_input("투입 종료일", value=None, key="intake_end",
                           min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
        mode, manual_mm = mm_fields(start, end, None, "intake")
    person_data = dict(name=name, company=company, category=category, account_id=account_id, job_role=job_role)
    kind = "person" if current else "intake"
    input_data = dict(person_data, id=current["id"]) if current else dict(
        person=person_data, assignment=dict(grade=grade, start_date=start, end_date=end,
                                            assignment_type=mode, mm=manual_mm))
    invalidate_preview(kind, input_data)
    if st.button("미리보기", key="preview_person"):
        stage(kind, input_data, current)
    confirm()


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
        elif kind == "contract":
            clean = validate_contract(data)
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
        st.caption("계정 ID와 과제·단가는 나중에 보완할 수 있습니다.")
        similar = [p for p in service.people() if p["name"] == data["person"]["name"]
                   and p["company"] == data["person"]["company"]]
        if similar:
            st.warning("같은 이름·업체의 인력이 이미 있습니다. 기존 인력의 추가 투입이라면 '과제 투입 관리'에서 등록하세요.")
            show_table(similar, ["name", "company", "job_role", "account_id"])
    reviewed = False
    if pending["kind"] == "assignment":
        overlaps = service.overlaps(data, current["id"] if current else None)
        if overlaps:
            st.warning("다른 투입 기간과 겹칩니다. 중복 허용 기준은 미확정이므로 검토가 필요합니다.")
            show_table(overlaps, ["project", "start_date", "end_date"])
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
            elif pending["kind"] == "contract":
                result_id = service.save_contract(
                    data, contract_id=current["id"] if current else None, **args
                )
            else:
                result_id = service.save_assignment(
                    data, assignment_id=current["id"] if current else None,
                    overlap_reviewed=reviewed, **args
                )
            st.session_state.pop("pending", None)
            st.session_state["saved_message"] = "저장했습니다."
            st.rerun()
        except (ValidationError, ConflictError) as exc:
            st.error(str(exc))
        except sqlite3.Error:
            st.error("DB 저장에 실패했습니다. 입력은 유지됩니다. 다시 시도하세요.")
    if right.button("취소", key="cancel"):
        st.session_state.pop("pending", None)
        st.rerun()


if page == "홈":
    from pmo.agent.agents import AGENTS, AUTO
    from pmo.agent.llm import OllamaClient
    from pmo.agent.runner import ask
    from ui.agent_view import render_turn_results

    client = OllamaClient()
    turns = st.session_state.setdefault("chat_turns", [])
    day = service.clock()
    try:
        ending, preparing = service.contracts_ending(day.year, day.month), service.checklist_overview()
    except sqlite3.Error:
        ending, preparing = [], []
    theme.kpi_row([("미확인 계약 종료 알림", len(pending_alerts), "건", bool(pending_alerts)),
                   (f"{day.month}월 계약 종료 예정", len(ending), "명", False),
                   ("근무 환경 준비 남은 투입", len(preparing), "건", False),
                   ("등록 인력", _people_count, "명", False)])
    chat_col, result_col = st.columns(2, gap="large")

    with chat_col, st.container(key="home_chat_panel"):
        st.subheader("🤖 PMO Agent")
        st.caption(f"로컬 LLM({client.model})이 도구로 확인해 답합니다. "
                   "등록·수정·삭제는 오른쪽 확인 카드에서 '확인 후 저장'을 눌러야 반영됩니다.")
        # 업무 카테고리: 라디오 대신 이모티콘을 붙인 큰 버튼 (10/20 담당자). 고른 버튼은 파란색(primary).
        agent_key = st.session_state.setdefault("home_category", AUTO)
        st.markdown("**업무 카테고리**", help="카테고리를 고르면 그 업무 전문 Agent가 필요한 도구와 업무 지침만 "
                    "가지고 답합니다. 자동(전체)은 모든 도구를 쓰는 단일 Agent입니다.")
        with st.container(key="home_agent"):   # 3개 · 2개 두 줄 (10/20 담당자)
            keys = list(AGENTS)
            columns = list(st.columns(3)) + list(st.columns(2))
            for column, key in zip(columns, keys):
                column.button(theme.agent_label(key, AGENTS[key].label), key=f"home_agent_{key}",
                              type="primary" if key == agent_key else "secondary", width="stretch",
                              on_click=st.session_state.__setitem__, args=("home_category", key))
        agent = AGENTS[agent_key]
        with st.container(height=480, border=True):
            if not turns:
                with st.chat_message("assistant"):
                    st.markdown("무엇을 도와드릴까요? 인력·투입·구매계약·근무 환경 준비를 묻거나, "
                                "상태 변경·계약 등록·수정·삭제를 요청하세요. 저장은 오른쪽 확인 카드에서 확인 후 진행됩니다.")
            for turn in turns:
                with st.chat_message("user"):
                    st.markdown(turn["question"])
                with st.chat_message("assistant"):
                    st.caption(theme.agent_label(turn.get("agent", AUTO),
                                                 f"{turn.get('agent_label', AGENTS[AUTO].label)} Agent"))
                    if turn["ok"]:
                        st.markdown(turn["answer"])
                    else:
                        st.error(turn["error"])
                        st.caption("LLM 없이도 왼쪽 메뉴의 각 화면에서 같은 정보를 조회할 수 있습니다.")
                    for message in turn.get("done", {}).values():
                        st.success(message)
                    st.caption(f"도구 {len(turn['calls'])}개 사용 · {turn['duration_ms'] / 1000:.1f}초")
        asked = None
        if not turns:
            st.caption(f"예시 질문 · {agent.label}")
            for i, example in enumerate(agent.examples):
                if st.button(example, key=f"home_example_{i}"):
                    asked = example
        typed = st.chat_input("질문을 입력하세요", key="home_chat")
        asked = (typed or "").strip() or asked
        if asked:
            history = []
            for turn in turns:
                if turn["ok"]:
                    history += [{"role": "user", "content": turn["question"]},
                                {"role": "assistant", "content": turn["answer"]}]
            with st.spinner(f"{agent.label} Agent — '{asked}' 도구를 고르고 조회하는 중입니다…"):
                result = ask(service, asked, client, history=history, agent=agent)
            turns.append(dict(result, question=asked))
            st.rerun()
        if turns and st.button("대화 지우기", key="home_clear"):
            turns.clear()
            st.rerun()

    with result_col, st.container(key="home_result_panel"):
        st.subheader("수행 결과")
        if not turns:
            st.info("질문하면 Agent가 사용한 도구와 조회 결과가 여기에 표시됩니다.")
        else:
            selected = len(turns) - 1
            if len(turns) > 1:
                # 새 질문이 생기면 위젯을 새로 만들어 가장 최근 결과를 기본으로 보여준다.
                selected = st.selectbox(
                    "결과를 볼 질문", list(range(len(turns))), index=selected,
                    key=f"home_pick_{len(turns)}",
                    format_func=lambda i: f"{i + 1}. [{turns[i].get('agent_label', '자동(전체)')}] "
                                          f"{turns[i]['question'][:40]}")
            render_turn_results(turns[selected], service, actor, selected)

elif page == "전체 인력 조회":
    st.subheader("인력 목록")
    people = service.people()
    query = st.text_input("이름·업체·직무·계정 ID 검색", key="person_search").strip().casefold()
    latest = {a["person_id"]: a for a in service.assignments()}
    filtered = [r for r in people if query in
                " ".join(str(r.get(k) or "") for k in ("name", "company", "job_role", "account_id")).casefold()]
    overview = [dict(r, **{k: latest.get(r["id"], {}).get(k)
                          for k in ("grade", "start_date", "end_date", "assignment_type", "mm")}) for r in filtered]
    show_table(overview, ["name", "company", "category", "job_role", "account_id", "grade", "start_date", "end_date",
                          "assignment_type", "mm"])
    st.caption("등급·기간은 시작일이 가장 최근인 투입 건을 표시합니다. 투입 등록·수정은 '과제 투입 관리'에서 합니다.")
    st.divider()
    render_monthly_mm()
    st.divider()
    if people:
        by_id = {r["id"]: r for r in people}
        with st.expander("인력 정보 수정 · 계정 ID 보완", expanded=bool(st.session_state.get("person_target"))):
            target = st.selectbox("수정할 인력", [None] + list(by_id), key="person_target",
                                  format_func=lambda i: "선택하세요" if i is None else person_label(by_id[i]))
            if target:
                person_form(by_id[target])

elif page == "신규 인력 등록":
    person_form(None)

elif page == "과제 투입 관리":
    st.subheader("인력별 투입 목록")
    people = service.people()
    by_person = {r["id"]: r for r in people}
    person_id = st.selectbox(
        "인력 선택", [r["id"] for r in people],
        format_func=lambda i: person_label(by_person[i]), key="assignment_person"
    ) if people else None
    if person_id is None:
        st.info("먼저 인력을 등록하세요.")
        st.stop()
    rows = service.assignments(person_id)
    timeline = service.assignment_timeline(person_id)
    columns = ["project", "revenue_project", "revenue_team", "start_date", "end_date", "grade",
               "assignment_type", "mm", "sales_rate", "purchase_rate", "rate_unit"]
    for title, key in (("현재 투입", "current"), ("예정 투입", "upcoming"), ("과거 투입 이력", "past")):
        st.markdown(f"**{title}** ({len(timeline[key])}건)")
        show_table(timeline[key], columns)
    st.caption(f"기준일 {timeline['today']} (Asia/Seoul) · 시작·종료일을 포함해 현재 투입을 판단합니다.")
    by_id = {r["id"]: r for r in rows}
    target = st.selectbox(
        "등록 또는 수정할 투입", [None] + list(by_id),
        format_func=lambda i: "신규 등록" if i is None else
        f"{by_id[i]['project'] or '과제 미입력'} · {by_id[i]['start_date']}~{by_id[i]['end_date']}",
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
        revenue_project = st.text_input(
            "매출귀속 프로젝트 (선택)", value=(current.get("revenue_project") or "") if current else "",
            key=f"revenue_{person_id}_{target}",
            help="투입 과제와 매출이 귀속되는 프로젝트가 다를 때 입력합니다. 금액 배분 규칙은 미확정입니다.")
        revenue_team = st.text_input("매출귀속팀 (선택)", value=(current.get("revenue_team") or "") if current else "",
                                     key=f"team_{person_id}_{target}")
        a, b = st.columns(2)
        start = a.date_input("시작일", value=date.fromisoformat(current["start_date"]) if current else None,
                             min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
        end = b.date_input("종료일", value=date.fromisoformat(current["end_date"]) if current else None,
                           min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
        allowed = list(grades())
        if current and current["grade"] and current["grade"] not in allowed:
            st.warning("기존 등급이 현재 허용 목록에 없습니다. 새 등급을 선택하세요.")
        grade = st.selectbox("등급", allowed, placeholder="선택 (컨설팅은 비워 둘 수 있음)",
                             index=allowed.index(current["grade"]) if current and current["grade"] in allowed else None)
        mode, manual_mm = mm_fields(start, end, current, f"{person_id}_{target}")
        sales = st.text_input("매출단가 (선택)", value=(current["sales_rate"] or "") if current else "",
                              key=f"sales_{person_id}_{target}")
        purchase = st.text_input("매입단가 (선택)", value=(current["purchase_rate"] or "") if current else "",
                                 key=f"purchase_{person_id}_{target}")
        unit = st.text_input("단가 기준 단위 (예: 원/MM, 원/일)",
                             value=(current["rate_unit"] or "") if current else "",
                             key=f"unit_{person_id}_{target}")
        input_data = dict(person_id=person_id, project=project, revenue_project=revenue_project,
                          revenue_team=revenue_team,
                          start_date=start,
                          end_date=end, grade=grade, sales_rate=sales,
                          purchase_rate=purchase, rate_unit=unit, assignment_type=mode, mm=manual_mm)
        invalidate_preview("assignment", input_data)
        if st.button("미리보기", key="preview_assignment"):
            stage("assignment", input_data, current)
    confirm()

elif page == "근무 환경 준비":
    from pmo.checklist import ITEMS as CHECKLIST_ITEMS, STATUSES

    st.subheader("준비가 남은 투입")
    st.caption("방문예약·계정 신청·ID카드 신청·OA기기 신청 진행 상황입니다. "
               "'완료'는 사내 시스템에서 실제 신청·예약을 마친 경우에만 선택하세요.")
    include_closed = st.checkbox("준비를 마친 투입도 보기", key="ck_include_closed")
    overview = service.checklist_overview(include_closed=include_closed)

    STATUS_STYLE = {   # 준비 항목 상태 색 (10/20 담당자: 시각적으로 잘 보이게)
        "완료": "background-color: #E5F4E4; color: #1E6B35; font-weight: 600",
        "진행 중": "background-color: #E7F1FB; color: #1D4ED8; font-weight: 600",
        "미진행": "background-color: #F9ECE7; color: #B4441E",
        "해당 없음": "background-color: #F0EFEC; color: #6B7280",
    }

    def start_label(days: int) -> str:
        return "투입 중" if days < 0 else ("오늘 시작" if days == 0 else f"시작 D-{days}")

    if not overview:
        st.success("준비가 남은 투입이 없습니다.")
        st.stop()
    # 항목마다 상태를 한 칸씩, 상태별 색으로 보여준다 (항목 순서: 방문예약·계정 신청·ID카드 신청·OA기기 신청)
    frame = pd.DataFrame([{"이름": r["name"], "업체명": r["company"], "과제명": r["project"] or "미입력",
                           "시작일": r["start_date"], "투입 현황": start_label(r["days_to_start"]),
                           **r["statuses"]} for r in overview])
    st.dataframe(frame.style.map(lambda v: STATUS_STYLE.get(v, ""), subset=list(CHECKLIST_ITEMS)),
                 use_container_width=True, hide_index=True)
    st.markdown(" ".join(f'<span style="{style}; padding: 2px 10px; border-radius: 10px; font-size: 13px">'
                         f'{name}</span>' for name, style in STATUS_STYLE.items()), unsafe_allow_html=True)
    by_assignment = {r["id"]: r for r in overview}
    aid = st.selectbox(
        "확인할 투입", list(by_assignment), key="ck_assignment",
        format_func=lambda i: f"{by_assignment[i]['name']} · {by_assignment[i]['company']} · "
                              f"{by_assignment[i]['project'] or '과제 미입력'} · {by_assignment[i]['start_date']}")
    if st.session_state.get("ck_context") != aid:
        st.session_state.pop("ck_pending", None)
        st.session_state["ck_context"] = aid

    for index, row in enumerate(service.checklist(aid)):
        with st.container(border=True):
            head, state = st.columns([3, 2])
            head.markdown(f"**{row['item']}**")
            state.markdown(f"상태: **{row['status']}**")
            notes = []
            if row["reason"]:
                notes.append(f"사유·메모: {row['reason']}")
            if row["applied_at"]:
                notes.append(f"실제 신청 완료 기록: {row['applied_at'][:16].replace('T', ' ')}")
            if row["updated_by"]:
                notes.append(f"최근 변경: {row['updated_by']} · {row['updated_at'][:16].replace('T', ' ')}")
            if notes:
                st.caption(" · ".join(notes))
            a, b = st.columns([2, 3])
            new_status = a.selectbox("변경할 상태", STATUSES, index=STATUSES.index(row["status"]),
                                     key=f"ck_status_{aid}_{index}")
            reason = b.text_input("사유·메모 ('해당 없음'이면 필수)", value=row["reason"],
                                  key=f"ck_reason_{aid}_{index}")
            actual = False
            if new_status == "완료":
                actual = st.checkbox("사내 시스템에서 실제 신청·예약을 마쳤습니다.",
                                     key=f"ck_actual_{aid}_{index}")
            left, right = st.columns(2)
            if left.button("변경 내용 확인", key=f"ck_preview_{aid}_{index}"):
                from pmo.checklist import validate_change
                try:
                    validate_change(row["item"], new_status, reason, actual)
                    changed = not (new_status == row["status"] and reason.strip() == row["reason"])
                except ValidationError as exc:
                    st.error(str(exc))
                    changed = None
                if changed is False:
                    st.info("바뀐 내용이 없습니다.")
                elif changed:
                    st.session_state["ck_pending"] = dict(
                        aid=aid, index=index, item=row["item"], before=row["status"],
                        status=new_status, reason=reason, actual=actual,
                        version=row["version"], token=str(uuid4()), actor=actor.strip())
            if right.button("신청 초안 만들기", key=f"ck_draft_{aid}_{index}"):
                try:
                    service.create_checklist_draft(aid, row["item"], actor=actor)
                    st.session_state["saved_message"] = (f"{row['item']} 신청 초안을 만들었습니다. "
                                                         "상태는 바뀌지 않았습니다.")
                    st.rerun()
                except ValidationError as exc:
                    st.error(str(exc))
                except sqlite3.Error:
                    st.error("초안을 저장하지 못했습니다. 다시 시도하세요.")
            if row["draft"]:
                with st.expander(f"신청 초안 · {row['drafted_at'][:16].replace('T', ' ')} 작성"):
                    st.code(row["draft"], language=None)
                    st.caption("초안만으로는 신청되지 않습니다. 사내 시스템에 붙여 넣어 직접 신청하세요.")
            pending = st.session_state.get("ck_pending")
            if pending and pending["aid"] == aid and pending["index"] == index:
                st.warning(f"{pending['item']}: {pending['before']} → {pending['status']}"
                           + (f" (사유: {pending['reason'].strip()})" if pending["reason"].strip() else ""))
                ok, cancel = st.columns(2)
                if ok.button("확인 후 저장", type="primary", key=f"ck_confirm_{aid}_{index}"):
                    try:
                        service.update_checklist_item(
                            aid, pending["item"], pending["status"], pending["reason"],
                            actual_done=pending["actual"], token=pending["token"],
                            actor=pending["actor"], expected_version=pending["version"])
                        st.session_state.pop("ck_pending", None)
                        st.session_state["saved_message"] = f"{pending['item']} 상태를 {pending['status']}(으)로 저장했습니다."
                        st.rerun()
                    except (ValidationError, ConflictError) as exc:
                        st.error(str(exc))
                    except sqlite3.Error:
                        st.error("저장하지 못했습니다. 입력은 유지됩니다. 다시 시도하세요.")
                if cancel.button("취소", key=f"ck_cancel_{aid}_{index}"):
                    st.session_state.pop("ck_pending", None)
                    st.rerun()

elif page == "전체 계약 조회":
    all_contracts = service.contracts()
    if not all_contracts:
        st.info("등록된 구매계약이 없습니다. '계약 등록·수정'에서 등록하세요.")
        st.stop()
    soon_count = sum(c["ending_soon"] for c in all_contracts)
    f1, f2, f3, f4 = st.columns([1, 1, 1, 1.4])
    company_filter = f1.selectbox("업체", ["전체"] + sorted({c["company"] for c in all_contracts}),
                                  key="contract_company")
    status_filter = f2.selectbox("계약 상태", ["전체", "준비", "확정"], key="contract_status")
    month_filter = f3.selectbox("종료 월", ["전체"] + sorted({c["end_date"][:7] for c in all_contracts}),
                                key="contract_month",
                                format_func=lambda m: m if m == "전체" else f"{m[:4]}년 {int(m[5:])}월")
    f4.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
    only_soon = f4.toggle(f"계약 종료 예정만 보기 ({soon_count}명)", key="contract_only_soon")
    rows = [c for c in all_contracts
            if (company_filter == "전체" or c["company"] == company_filter)
            and (status_filter == "전체" or c["status"] == status_filter)
            and (month_filter == "전체" or c["end_date"].startswith(month_filter))
            and (not only_soon or c["ending_soon"])]
    if not rows:
        st.info("조건에 맞는 계약이 없습니다.")
    else:
        frame = pd.DataFrame([{"이름": r["name"], "업체명": r["company"],
                               "계약번호": r.get("contract_no") or "미발급", "계약 상태": r["status"],
                               "계약 시작일": r["start_date"], "계약 종료일": r["end_date"],
                               "만료 상태": r["label"], "연장 계약": "등록" if r["has_successor"] else ""}
                              for r in rows])
        soon = [r["ending_soon"] for r in rows]
        # 계약 종료 예정 줄은 붉은 계열 (10/25 담당자)
        styled = frame.style.apply(lambda row: ["background-color: #FBEBEB; color: #9B1C1C"] * len(row)
                                   if soon[row.name] else [""] * len(row), axis=1)
        st.dataframe(styled, hide_index=True, use_container_width=True)
    st.markdown(f'<span style="background-color: #FBEBEB; color: #9B1C1C; padding: 2px 10px; '
                f'border-radius: 10px; font-size: 13px">계약 종료 예정</span> '
                f'<span style="font-size: 13px; color: #5B6780">종료 {CONTRACT_ALERT_DAYS}일 전부터 종료일 당일까지, '
                f'연장 계약이 아직 등록되지 않은 계약 · {len(rows)}건 표시 / 전체 {len(all_contracts)}건</span>',
                unsafe_allow_html=True)

elif page == "계약 등록·수정":
    st.subheader("계약 등록 / 수정")
    eligible = [r for r in service.people() if r["category"] in ("협력", "컨설팅")]
    if not eligible:
        st.info("구매계약은 협력·컨설팅 인력만 등록할 수 있습니다. 먼저 인력을 등록하세요.")
        st.stop()
    people_by_id = {r["id"]: r for r in eligible}
    person_id = st.selectbox(
        "계약 대상 인력 (협력·컨설팅)", list(people_by_id),
        format_func=lambda i: person_label(people_by_id[i]),
        key="contract_person")
    person_contracts = service.contracts(person_id)
    by_id = {r["id"]: r for r in person_contracts}
    target = st.selectbox(
        "등록 또는 수정할 계약", [None] + list(by_id),
        format_func=lambda i: "신규 등록" if i is None else
        f"{by_id[i]['contract_no'] or '번호 미발급'} · {by_id[i]['start_date']}~{by_id[i]['end_date']}",
        key=f"contract_target_{person_id}")
    current = by_id.get(target)
    context = ("contract", person_id, target)
    if st.session_state.get("edit_context") != context:
        st.session_state.pop("pending", None)
        st.session_state["edit_context"] = context
    suffix = f"{person_id}_{target}"
    person_assignments = {r["id"]: r for r in service.assignments(person_id)}
    with st.container():
        company = st.text_input("업체명", value=current["company"] if current
                                else people_by_id[person_id]["company"], key=f"ccompany_{suffix}")
        statuses = ["준비", "확정"]
        status = st.selectbox("계약 상태", statuses,
                              index=statuses.index(current["status"]) if current else 0,
                              key=f"cstatus_{suffix}",
                              help="계약번호 발급 전에는 '준비'로 저장하고, 확정 시 계약번호가 필요합니다.")
        contract_no = st.text_input("계약번호", value=(current["contract_no"] or "") if current else "",
                                    key=f"cno_{suffix}")
        a, b = st.columns(2)
        c_start = a.date_input("계약 시작일",
                               value=date.fromisoformat(current["start_date"]) if current else None,
                               min_value=date(1900, 1, 1), max_value=date(2100, 12, 31),
                               key=f"cstart_{suffix}")
        c_end = b.date_input("계약 종료일",
                             value=date.fromisoformat(current["end_date"]) if current else None,
                             min_value=date(1900, 1, 1), max_value=date(2100, 12, 31),
                             key=f"cend_{suffix}")
        assignment_options = [None] + list(person_assignments)
        assignment_id = st.selectbox(
            "연결할 투입 (선택)", assignment_options,
            index=assignment_options.index(current["assignment_id"])
            if current and current["assignment_id"] in assignment_options else 0,
            format_func=lambda i: "연결 안 함" if i is None else
            f"{person_assignments[i]['project'] or '과제 미입력'} · "
            f"{person_assignments[i]['start_date']}~{person_assignments[i]['end_date']}",
            key=f"cassign_{suffix}")
        previous_options = [None] + [i for i in by_id if i != target]
        previous_id = st.selectbox(
            "이전 계약 (연장 계약일 때 선택)", previous_options,
            index=previous_options.index(current["previous_contract_id"])
            if current and current["previous_contract_id"] in previous_options else 0,
            format_func=lambda i: "없음 (신규 계약)" if i is None else
            f"{by_id[i]['contract_no'] or '번호 미발급'} · {by_id[i]['start_date']}~{by_id[i]['end_date']}",
            key=f"cprev_{suffix}")
        note = st.text_area("메모 (선택)", value=(current["note"] or "") if current else "",
                            key=f"cnote_{suffix}")
        input_data = dict(person_id=person_id, company=company, contract_no=contract_no,
                          status=status, start_date=c_start, end_date=c_end,
                          assignment_id=assignment_id, previous_contract_id=previous_id, note=note)
        invalidate_preview("contract", input_data)
        if st.button("미리보기", key="preview_contract"):
            stage("contract", input_data, current)
    confirm()

elif page == "검수하기":
    def won(value) -> str:
        return "계산 불가" if value is None else f"{value:,}원"

    def total_text(value) -> str:
        return "계산 불가" if value is None else f"{value:,}"

    choices = service.inspection_contracts()
    if not choices:
        st.info("계약번호가 있는 구매계약이 없습니다. '계약 등록·수정'에서 계약번호를 입력하세요.")
        st.stop()
    # 검색은 한 줄: 업체명 · 계약번호 · 월 (+ 세부 조회 토글) (10/25 담당자)
    s1, s2, s3, s4 = st.columns([1.2, 1.6, 1, 0.8], vertical_alignment="bottom")
    company = s1.selectbox("업체명", sorted({c["company"] for c in choices}), key="insp_company")
    numbers = {c["contract_no"]: c for c in choices if c["company"] == company}
    contract_no = s2.selectbox(
        "계약번호", list(numbers), key=f"insp_no_{company}",
        format_func=lambda n: f"{n} · {numbers[n]['start_date']}~{numbers[n]['end_date']} · {numbers[n]['people']}명")
    chosen = numbers[contract_no]
    months, cursor = [], date.fromisoformat(chosen["start_date"]).replace(day=1)
    while cursor.isoformat()[:7] <= chosen["end_date"][:7]:
        months.append(cursor.isoformat()[:7])
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    # 기본은 이번 달 (계약 기간 밖이면 가장 가까운 달)
    default = min(max(service.clock().isoformat()[:7], months[0]), months[-1])
    label = lambda m: f"{m[:4]}년 {int(m[5:])}월"  # noqa: E731
    key = f"{company}_{contract_no}"
    detail = s4.toggle("세부 조회", key=f"insp_detail_{key}", help="시작 월과 종료 월을 골라 여러 달을 한 번에 봅니다.")
    month = s3.selectbox("월", months, index=months.index(default), format_func=label,
                         key=f"insp_month_{key}", disabled=detail)
    start_month = end_month = month
    if detail:
        d1, d2, _ = st.columns([1.2, 1.6, 1.8])
        start_month = d1.selectbox("시작 월", months, index=months.index(month), format_func=label,
                                   key=f"insp_from_{key}")
        end_month = d2.selectbox("종료 월", months, index=months.index(month), format_func=label,
                                 key=f"insp_to_{key}")
    try:
        result = service.inspection(company, contract_no, start_month, end_month)
    except ValidationError as exc:
        st.error(str(exc))
        st.stop()
    period_label = label(start_month) if start_month == end_month else f"{label(start_month)} ~ {label(end_month)}"
    theme.kpi_row([("계약 총액 (계약 기간 전체)", total_text(result["contract"]["total"]), "원", False),
                   (f"검수 금액 · {period_label}", total_text(result["selected"]["total"]), "원", False),
                   ("계약 인원", result["people"], "명", False),
                   ("계산 불가 건", result["selected"]["incomplete"], "건", bool(result["selected"]["incomplete"]))])
    st.subheader(f"매출귀속 프로젝트별 검수 금액 · {period_label}")
    st.caption("'프로젝트 전체 금액'은 계약 기간 전체에서 그 프로젝트에 귀속되는 금액입니다. "
               "구매시스템의 계약금액과 맞춰 볼 때 쓰세요.")
    lines = result["selected"]["lines"]
    if not lines and not result["contract"]["lines"]:
        st.info("이 계약에 연결된 투입이 없습니다.")
    else:
        from pmo.inspection import report_rows
        # 구매시스템에는 프로젝트 번호가 없어, 계약 금액 중 프로젝트 귀속 총액(계약 기간)과 이번 달 검수 금액을 함께 본다 (10/25)
        amount_col, total_col = f"검수 금액 ({period_label})", "프로젝트 전체 금액 (계약 기간)"
        rows = report_rows(lines, result["contract"]["lines"])

        def amount_cell(row, key):
            if key == "amount" and not row["in_period"]:
                return ""                                   # 이 기간에 투입이 없는 인력
            return won(row[key])                            # None이면 '계산 불가' (0원 아님)

        frame = pd.DataFrame([{"인력명": r["name"], "매출귀속 프로젝트": r["revenue_project"],
                               "MM": r["mm"] or "", total_col: amount_cell(r, "project_amount"),
                               amount_col: amount_cell(r, "amount")} for r in rows])
        kinds = [r["kind"] for r in rows]
        shade = {"subtotal": "font-weight: 600; background-color: #F7F9FC",
                 "total": "font-weight: 700; background-color: #E8EEFB"}

        def style_row(row):
            base = shade.get(kinds[row.name], "")
            # 이번 달(조회 기간) 검수 금액은 굵게, 파란색으로 강조 (10/25 담당자)
            return [f"{base}; font-weight: 700; color: #1D4ED8" if col == amount_col else base for col in row.index]

        st.dataframe(frame.style.apply(style_row, axis=1), hide_index=True, use_container_width=True,
                     column_config={c: st.column_config.TextColumn(alignment="right")
                                    for c in ("MM", total_col, amount_col)})
    incomplete = [l for l in lines + result["contract"]["lines"] if l["amount"] is None]
    incomplete = list({(l["name"], l["note"]): l for l in incomplete}.values())
    if incomplete:
        st.warning(f"계산할 수 없는 {len(incomplete)}건은 합계에서 뺐습니다(0원으로 계산하지 않음). "
                   + " · ".join(f"{l['name']}: {l['note']}" for l in incomplete))
    st.caption(f"계산 규칙: {result['rule']}. 이 금액은 구매시스템의 실제 검수·승인과 별개이며, "
               "구매시스템 계약금액과 대조해 규칙을 확정할 예정입니다.")

elif page == "계약 종료 알림":
    st.subheader("계약 종료 알림")
    st.caption("계약 종료 14일 전부터 표시하며, 앱을 실행할 때마다 미처리 대상을 다시 확인합니다. "
               "'확인'은 알림을 봤다는 기록이고 계약 갱신 완료와는 별개입니다. "
               "연장 계약은 '계약 등록·수정'에서 이전 계약을 지정해 등록하세요.")
    if not pending_alerts:
        st.success("미확인 계약 종료 알림이 없습니다.")
    for alert in pending_alerts:
        with st.container(border=True):
            left, middle, right, action = st.columns([3, 2, 2, 1])
            left.markdown(f"**{alert['name']}** · {alert['company']} · {alert['category']}")
            left.caption(f"계약번호 {alert['contract_no'] or '미발급(준비)'}")
            middle.markdown(f"**{alert['label']}**")
            middle.caption(f"종료일 {alert['end_date']} · 알림 시작 {alert['notify_from']}")
            right.write("연장 계약 등록됨" if alert["has_successor"] else "연장 계약 없음")
            if action.button("확인", key=f"ack_{alert['id']}"):
                try:
                    service.acknowledge_alert(alert["id"], actor=actor)
                    st.session_state["saved_message"] = f"{alert['name']}의 계약 종료 알림을 확인 처리했습니다."
                    st.rerun()
                except (ValidationError, ConflictError) as exc:
                    st.error(str(exc))
                except sqlite3.Error:
                    st.error("확인 처리를 저장하지 못했습니다. 다시 시도하세요.")

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
    st.subheader("실행 기록")
    st.caption("저장·알림 확인·도구 호출의 성공 여부와 소요 시간 (최근 100건)")
    log = service.run_log(100)
    if log:
        st.dataframe([{"시각": r["started_at"], "작업": r["operation"], "주체": r["actor"],
                       "성공": "성공" if r["ok"] else "실패", "소요(ms)": r["duration_ms"],
                       "결과": r["result"] or "", "오류": r["error"] or ""} for r in log],
                     use_container_width=True, hide_index=True)
    else:
        st.info("실행 기록이 없습니다.")
