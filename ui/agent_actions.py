"""Agent가 준비한 변경 계획을 '현재 → 변경' 확인 카드로 보여주고, 사용자가 확인하면 저장한다.

- 대상과 값은 Agent 요청을 바탕으로 코드가 DB에서 확정했다(pmo/agent/actions.py). 사용자는 값을 고르지 않는다.
- 사용자는 '확인 후 저장' 또는 '취소'만 누른다. '완료'(실제 신청 여부)와 삭제는 확인란을 한 번 더 선택한다.
- 값을 바꾸려면 채팅으로 다시 요청하거나 메뉴 화면을 쓴다.
- 같은 버튼을 다시 눌러도 한 번만 저장되고(요청 토큰), 카드를 만든 뒤 데이터가 바뀌었으면 저장을 막는다(버전 검사).
"""
import sqlite3
from uuid import uuid4

import streamlit as st

from pmo.agent.actions import execute_plan
from pmo.agent.llm import LLMError
from pmo.errors import ConflictError, ValidationError


def render_plan(service, plan: dict, key: str, actor: str, turn: dict, call_index: int) -> None:
    """변경 계획 하나를 확인 카드로 보여준다. 카드 모양은 ui/theme.py (st-key-…_card)."""
    with st.container(key=f"{key}_card"):
        _render_plan(service, plan, key, actor, turn, call_index)


def _render_plan(service, plan: dict, key: str, actor: str, turn: dict, call_index: int) -> None:
    done = turn.setdefault("done", {})
    verb = "삭제" if plan["action"] == "contract_delete" else "저장"
    st.markdown(f"**확인 후 {verb} · {plan['title']}**  \n아래 내용으로 바꿀까요? 아직 {verb}되지 않았습니다.")
    st.table(plan["rows"])
    if call_index in done:
        (st.info if done[call_index].startswith("취소") else st.success)(done[call_index])
        return
    token = turn.setdefault("tokens", {}).setdefault(call_index, str(uuid4()))
    checked = True
    if plan.get("check"):
        checked = st.checkbox(plan["check"], key=f"{key}_check")
    deleting = plan["action"] == "contract_delete"
    ok, cancel = st.columns(2)
    if ok.button("확인 후 삭제" if deleting else "확인 후 저장", type="primary", key=f"{key}_confirm",
                 disabled=not checked):
        if not checked:   # 버튼 비활성화와 별개로 코드에서도 확인한다
            st.error("확인란을 먼저 선택하세요.")
            return
        if not actor.strip():
            st.error("왼쪽 위 '변경 주체'를 입력하세요.")
            return
        try:
            message = execute_plan(service, plan, token=token, actor=actor.strip(), checked=checked)
            done[call_index] = message
            st.session_state["saved_message"] = message
            st.rerun()
        except ConflictError as exc:
            st.error(f"{exc} 같은 요청을 채팅에 다시 입력해 새 확인 카드를 만드세요.")
        except (ValidationError, LLMError) as exc:
            st.error(str(exc))
        except sqlite3.Error:
            st.error("저장하지 못했습니다. 다시 시도하세요.")
    if cancel.button("취소", key=f"{key}_cancel"):
        done[call_index] = "취소했습니다. 저장하지 않았습니다."
        st.rerun()
    st.caption("값을 바꾸려면 채팅으로 다시 요청하거나 왼쪽 메뉴 화면을 이용하세요.")
