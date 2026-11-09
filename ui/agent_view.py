"""홈 화면 채팅의 수행 결과: Agent가 호출한 도구와 조회 결과를 답변 아래에 보여준다(11/08 한 화면 채팅)."""
import streamlit as st

from pmo.agent.agents import AGENTS, AUTO
from pmo.tools import TOOL_LABELS  # 도구 한국어 이름 (Agent 답변 정리에도 씀)

EXAMPLES = AGENTS[AUTO].examples   # 카테고리별 예시 질문은 pmo/agent/agents.py

COLUMNS = {
    "id": "관리번호", "name": "이름", "company": "업체명", "category": "분류", "account_id": "계정 ID",
    "project": "과제명", "revenue_project": "매출귀속 프로젝트", "start_date": "시작일", "end_date": "종료일",
    "grade": "등급", "assignment_type": "투입 Type", "mm": "투입 MM",
    "contract_no": "계약번호", "status": "상태", "label": "만료 상태", "has_successor": "연장 계약",
    "notify_from": "알림 시작일", "item": "항목", "reason": "사유·메모", "drafted_at": "초안 작성",
    "applied_at": "실제 신청 완료", "done": "처리", "total": "전체", "open_items": "남은 항목",
    "open_labels": "남은 항목(상태)",
}

_TABLE_FIELDS = {
    "find_people": ["name", "company", "category", "account_id"],
    "list_contracts": ["contract_no", "start_date", "end_date", "label", "has_successor"],
    "contracts_ending": ["name", "company", "contract_no", "end_date", "label", "has_successor"],
    "pending_contract_alerts": ["name", "company", "contract_no", "end_date", "label", "notify_from"],
}
_ASSIGNMENT_FIELDS = ["project", "revenue_project", "start_date", "end_date", "grade"]


def _cell(value):
    if value is None or value == "":
        return "미입력"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, list):
        return ", ".join(map(str, value)) or "-"
    if isinstance(value, str) and "T" in value and len(value) >= 16 and value[4] == "-":
        return value[:16].replace("T", " ")
    return value


HIDDEN_KEYS = {"id", "person_id", "assignment_id", "contract_id", "previous_contract_id", "version"}


def table(rows: list[dict], fields: list[str] | None = None) -> None:
    if not rows:
        st.caption("결과 없음")
        return
    # 내부 관리번호(id 계열)는 화면에 보이지 않는다(10/20 담당자 요청)
    fields = [k for k in (fields or [k for k in rows[0] if not isinstance(rows[0][k], (dict, list))
                                    or k in ("open_items", "open_labels")]) if k not in HIDDEN_KEYS]
    st.dataframe([{COLUMNS.get(k, k): _cell(r.get(k)) for k in fields} for r in rows],
                 use_container_width=True, hide_index=True)


_LABELS = {**COLUMNS, "period": "기간", "basis": "기준", "month": "월", "assigned_days": "투입 근무일",
           "working_days": "전체 근무일", "note": "비고", "rule": "계산 규칙", "today": "기준일", "members": "인력",
           "include_past": "과거 포함"}


def _render_mm(data: dict) -> None:
    """기간 MM 계산 결과(calculate_mm)를 사람이 읽는 모양으로."""
    who = f"{data['name']} · " if data.get("name") else ""
    st.markdown(f"**{who}{data['period']} · {data['mm']} MM**")
    if data.get("basis"):
        st.caption(data["basis"])
    if data.get("months"):
        table(data["months"], ["month", "assigned_days", "working_days"])
    if data.get("assignments"):
        table(data["assignments"], ["project", "revenue_project", "assignment_type", "period", "mm", "note"])
    if data.get("note"):
        st.caption(data["note"])
    if data.get("rule"):
        st.caption(data["rule"])


def _render_dict(data: dict) -> None:
    """전용 화면이 없는 도구 결과: JSON 대신 값 목록·표로 보여준다(11/08 담당자). 원본은 실행 기록에 남는다."""
    plain = {k: v for k, v in data.items() if not isinstance(v, (dict, list)) and k not in HIDDEN_KEYS}
    if plain:
        st.dataframe([{"항목": _LABELS.get(k, k), "값": _cell(v)} for k, v in plain.items()],
                     width="stretch", hide_index=True)
    for key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            st.markdown(f"**{_LABELS.get(key, key)}** ({len(value)}건)")
            table(value)


def _render_data(tool: str, data) -> None:
    if tool == "get_assignment_timeline":
        st.caption(f"기준일 {data.get('today')}")
        for title, key in (("현재 투입", "current"), ("예정 투입", "upcoming"), ("과거 투입", "past")):
            st.markdown(f"**{title}** ({len(data.get(key, []))}건)")
            table(data.get(key, []), _ASSIGNMENT_FIELDS)
    elif tool == "calculate_mm":
        _render_mm(data)
    elif tool == "inspection_amount_by_revenue_project":
        st.caption(f"{data['company']} · 계약번호 {data['contract_no']} · 계약 기간 {data['contract_period']} · "
                   f"검수 기간 {data['inspection_period']}")
        st.markdown(f"**검수 금액** {data['inspection_total']} · **계약 총액(계약 기간 전체)** {data['contract_total']}")
        st.dataframe([{"매출귀속 프로젝트": p["revenue_project"], "인원": p["people"], "MM": p["mm"],
                       "검수 금액": p["amount"], "프로젝트 전체 금액(계약 기간)": p["contract_amount"]}
                      for p in data["projects"]], width="stretch", hide_index=True)
        if data["lines"]:
            st.dataframe([{"인력": l["name"], "매출귀속 프로젝트": l["revenue_project"], "기간": l["period"],
                           "MM": l["mm"], "검수 금액": l["amount"], "비고": l.get("note", "")}
                          for l in data["lines"]], width="stretch", hide_index=True)
        if data["not_calculated"]:
            st.warning("계산 불가 " + str(len(data["not_calculated"])) + "건(합계에서 뺌): " + ", ".join(data["not_calculated"]))
        st.caption(data["rule"])
    elif tool == "query_database":
        # 만든 SQL은 화면에 보이지 않고 실행 기록에 남긴다(11/08 담당자: 사람이 보는 화면에는 개발용 표시를 두지 않음)
        st.caption("자유 조회(읽기 전용)로 찾은 결과입니다."
                   + (" 결과가 많아 일부만 표시합니다." if data.get("truncated") else ""))
        table(data["rows"], data["columns"] or None)
    elif isinstance(data, list):
        table(data, _TABLE_FIELDS.get(tool))
    elif isinstance(data, dict):
        _render_dict(data)
    else:
        st.write(data)


def render_turn_inline(turn: dict, service=None, actor: str = "", turn_index: int = 0, latest: bool = False) -> None:
    """채팅 답변 아래에 붙는 수행 결과(11/08 담당자: 질문·답변을 한 화면에).

    확인 카드(prepare_*)는 바로 보이게 답변 아래에 두고, 조회 결과 표는 '조회 결과' 접기 상자에 넣는다
    (가장 최근 답변만 펼침). 저장은 이전과 같이 카드의 '확인 후 저장'을 눌러야 한다.
    """
    from ui.agent_actions import render_plan
    calls = turn["calls"]
    plans = [(n, c) for n, c in enumerate(calls, 1) if c["tool"].startswith("prepare_")]
    reads = [(n, c) for n, c in enumerate(calls, 1) if not c["tool"].startswith("prepare_")]
    for number, call in plans:
        if call["ok"] and service is not None:
            with st.container(border=True):
                st.markdown(f"**{TOOL_LABELS.get(call['tool'], call['tool'])}**")
                render_plan(service, call["data"]["plan"], f"act{turn_index}_{number}", actor, turn, number)
        elif not call["ok"]:
            st.info(f"확인 카드를 만들지 않았습니다: {call.get('error')}")
    seconds = f"{turn['duration_ms'] / 1000:.1f}초"
    if not reads:
        st.caption(f"도구 {len(calls)}개 사용 · {seconds}")
        return
    with st.expander(f"🔎 조회 결과 · 도구 {len(calls)}개 · {seconds}", expanded=latest):
        for number, call in reads:
            # 도구 이름·입력값(개발용 표시)은 화면에 두지 않고 실행 기록(run_log)에 남긴다(11/08 담당자)
            st.markdown(f"**{number}. {TOOL_LABELS.get(call['tool'], call['tool'])}**")
            if call["ok"]:
                _render_data(call["tool"], call.get("data"))
            else:
                st.warning(call.get("error") or "도구 실행에 실패했습니다.")
