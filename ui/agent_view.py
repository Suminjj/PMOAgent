"""홈 화면 오른쪽 '수행 결과' 패널: Agent가 호출한 도구와 조회 결과를 표로 보여준다."""
import streamlit as st

EXAMPLES = (
    "처리 안 한 계약 만료 알림 있어?",
    "베타컨설팅 이번 달 계약 종료 예정 인력 알려줘",
    "이가상 지금 무슨 과제에 투입되어 있어?",
    "근무 환경 준비가 남은 투입 알려줘",
)

TOOL_LABELS = {
    "find_people": "인력 찾기",
    "get_assignment_timeline": "투입 이력 조회",
    "list_contracts": "구매계약 조회",
    "contracts_ending": "월·업체별 계약 종료 예정",
    "pending_contract_alerts": "미확인 계약 종료 알림",
    "checklist_status": "근무 환경 준비 현황",
    "monthly_purchase_needs": "월별 구매 필요 인력 (구현 예정)",
    "inspection_amount_by_revenue_project": "매출귀속 프로젝트별 검수 금액 (구현 예정)",
    "prepare_contract_change": "계약 변경 미리보기 (구현 예정)",
}

COLUMNS = {
    "id": "관리번호", "name": "이름", "company": "업체명", "category": "분류", "account_id": "계정 ID",
    "project": "과제명", "revenue_project": "매출귀속 프로젝트", "start_date": "시작일", "end_date": "종료일",
    "grade": "등급", "assignment_type": "투입 Type", "mm": "투입 MM",
    "contract_no": "계약번호", "status": "상태", "label": "만료 상태", "has_successor": "연장 계약",
    "notify_from": "알림 시작일", "item": "항목", "reason": "사유·메모", "drafted_at": "초안 작성",
    "applied_at": "실제 신청 완료", "done": "처리", "total": "전체", "open_items": "남은 항목",
}

_TABLE_FIELDS = {
    "find_people": ["name", "company", "category", "account_id", "id"],
    "list_contracts": ["contract_no", "status", "start_date", "end_date", "label", "has_successor"],
    "contracts_ending": ["name", "company", "contract_no", "status", "end_date", "label", "has_successor"],
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


def table(rows: list[dict], fields: list[str] | None = None) -> None:
    if not rows:
        st.caption("결과 없음")
        return
    fields = fields or [k for k in rows[0] if not isinstance(rows[0][k], (dict, list)) or k == "open_items"]
    st.dataframe([{COLUMNS.get(k, k): _cell(r.get(k)) for k in fields} for r in rows],
                 use_container_width=True, hide_index=True)


def _render_data(tool: str, data) -> None:
    if tool == "get_assignment_timeline":
        st.caption(f"기준일 {data.get('today')}")
        for title, key in (("현재 투입", "current"), ("예정 투입", "upcoming"), ("과거 투입", "past")):
            st.markdown(f"**{title}** ({len(data.get(key, []))}건)")
            table(data.get(key, []), _ASSIGNMENT_FIELDS)
    elif tool == "checklist_status" and data and "items" in data[0]:
        for a in data:
            st.markdown(f"**{a.get('project') or '과제 미입력'}** · {a['start_date']} ~ {a['end_date']}")
            table(a["items"], ["item", "status", "reason", "drafted_at", "applied_at"])
    elif tool == "checklist_status":
        table(data, ["name", "company", "project", "start_date", "done", "total", "open_items"])
    elif isinstance(data, list):
        table(data, _TABLE_FIELDS.get(tool))
    else:
        st.json(data)


def render_turn_results(turn: dict) -> None:
    """질문 한 번에 Agent가 사용한 도구와 결과를 차례로 보여준다."""
    st.caption(f"질문: {turn['question']}")
    if not turn["calls"]:
        if turn["ok"]:
            st.info("도구를 사용하지 않은 답변입니다. 조회 결과가 필요하면 질문을 구체적으로 바꿔 보세요.")
        else:
            st.error(turn["error"])
        return
    for number, call in enumerate(turn["calls"], 1):
        with st.container(border=True):
            args = ", ".join(f"{k}={v}" for k, v in call["arguments"].items()) or "입력 없음"
            st.markdown(f"**{number}. {TOOL_LABELS.get(call['tool'], call['tool'])}**")
            st.caption(f"{call['tool']}({args})")
            if call["ok"]:
                _render_data(call["tool"], call.get("data"))
            else:
                st.warning(call.get("error") or "도구 실행에 실패했습니다.")
