"""Agent가 제안한 등록·수정·삭제를 화면 양식으로 보여주고, 사용자 확인 후 정해진 업무 함수로 저장한다.

- Agent(LLM)는 값만 제안한다(prepare_* 도구). 여기서 그 값으로 양식을 미리 채운다.
- 사용자는 화면에서 값을 고르고 고칠 수 있다. 선택지는 DB에서 읽어 온 값만 쓴다.
- '변경 내용 확인'으로 전후 값을 본 뒤 '확인 후 저장'을 눌러야 저장된다. 삭제는 한 번 더 확인한다.
- 미리보기 뒤에 값을 바꾸면 미리보기는 무효가 된다. 같은 요청을 다시 눌러도 한 번만 저장된다(요청 토큰).
"""
import sqlite3
from datetime import date
from uuid import uuid4

import streamlit as st

from pmo.checklist import ITEMS, STATUSES, validate_change
from pmo.contracts import CONTRACT_CATEGORIES, CONTRACT_STATUSES, validate_contract
from pmo.errors import ConflictError, ValidationError

ACTION_TITLES = {
    "checklist_update": "근무 환경 준비 상태 변경",
    "contract_save": "구매계약 등록·수정",
    "contract_delete": "구매계약 삭제",
    "account_id": "계정 ID 입력",
    "alert_ack": "계약 종료 알림 확인",
}


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _shown(value) -> str:
    return "미입력" if value in (None, "") else str(value)


def _index(options: list, value, default: int | None = 0) -> int | None:
    return options.index(value) if value in options else default


def _person(service, key: str, values: dict, categories=None):
    people = [p for p in service.people() if not categories or p["category"] in categories]
    if not people:
        st.info("선택할 수 있는 인력이 없습니다.")
        return None
    by_id = {p["id"]: p for p in people}
    preset = values.get("person_id")
    if preset and preset not in by_id:
        from pmo.tools import resolve_person
        resolved, _ = resolve_person(service, preset)   # 이름이 들어온 경우 보완
        preset = resolved if resolved in by_id else preset
    if preset and preset not in by_id:
        st.warning(f"Agent가 제안한 인력({preset})을 목록에서 찾지 못했습니다. 직접 선택하세요.")
    pid = st.selectbox("대상 인력", list(by_id), index=_index(list(by_id), preset, None),
                       placeholder="인력을 선택하세요", key=f"{key}_person",
                       format_func=lambda i: f"{by_id[i]['name']} · {by_id[i]['company']} · {i}")
    return by_id.get(pid)


# --- 액션별 양식: (params, 미리보기 행, 실행 함수)를 돌려준다. 값이 모자라면 None ----------------------

def _checklist_form(service, key, values, actor):
    person = _person(service, key, values)
    if not person:
        return None
    assignments = sorted(service.assignments(person["id"]), key=lambda a: a["start_date"], reverse=True)
    if not assignments:
        st.info("이 인력에 등록된 투입이 없습니다.")
        return None
    open_ids = [r["id"] for r in service.checklist_overview() if r["person_id"] == person["id"]]
    by_id = {a["id"]: a for a in assignments}
    ids = list(by_id)
    aid = st.selectbox("투입", ids, index=_index(ids, open_ids[0] if open_ids else None), key=f"{key}_assignment",
                       format_func=lambda i: f"{by_id[i]['project'] or '과제 미입력'} · "
                                             f"{by_id[i]['start_date']}~{by_id[i]['end_date']}")
    item = st.selectbox("항목", list(ITEMS), index=_index(list(ITEMS), values.get("item"), None),
                        placeholder="항목을 선택하세요", key=f"{key}_item")
    if not item:
        return None
    row = next(r for r in service.checklist(aid) if r["item"] == item)
    status = st.selectbox("변경할 상태", list(STATUSES),
                          index=_index(list(STATUSES), values.get("status") or row["status"]), key=f"{key}_{aid}_{item}_status")
    reason = st.text_input("사유·메모 ('해당 없음'이면 필수)", value=values.get("reason") or row["reason"],
                           key=f"{key}_{aid}_{item}_reason")
    actual = False
    if status == "완료":
        actual = st.checkbox("사내 시스템에서 실제 신청·예약을 마쳤습니다.", key=f"{key}_{aid}_{item}_actual")

    def preview():
        validate_change(item, status, reason, actual)
        return [("인력", person["name"], person["name"]), ("항목", item, item),
                ("상태", row["status"], status), ("사유·메모", _shown(row["reason"]), _shown(reason.strip()))]

    def run(token):
        service.update_checklist_item(aid, item, status, reason, actual_done=actual, token=token,
                                      actor=actor, expected_version=row["version"])
        return f"{person['name']}의 {item} 상태를 '{status}'(으)로 저장했습니다."

    return (aid, item, status, reason, actual, row["version"]), preview, run


def _contract_form(service, key, values, actor):
    person = _person(service, key, values, CONTRACT_CATEGORIES)
    if not person:
        return None
    contracts = {c["id"]: c for c in service.contracts(person["id"])}
    options = [None] + list(contracts)
    target = st.selectbox("대상 계약", options, index=_index(options, values.get("contract_id")),
                          key=f"{key}_target",
                          format_func=lambda i: "신규 등록" if i is None else
                          f"{contracts[i]['contract_no'] or '번호 미발급'} · "
                          f"{contracts[i]['start_date']}~{contracts[i]['end_date']}")
    current = contracts.get(target) or {}
    company = st.text_input("업체명", value=values.get("company") or current.get("company") or person["company"],
                            key=f"{key}_{target}_company")
    status = st.selectbox("계약 상태", list(CONTRACT_STATUSES),
                          index=_index(list(CONTRACT_STATUSES), values.get("status") or current.get("status")),
                          key=f"{key}_{target}_cstatus")
    number = st.text_input("계약번호", value=values.get("contract_no") or current.get("contract_no") or "",
                           key=f"{key}_{target}_number")
    a, b = st.columns(2)
    start = a.date_input("계약 시작일", value=_iso(values.get("start_date")) or _iso(current.get("start_date")),
                         min_value=date(1900, 1, 1), max_value=date(2100, 12, 31), key=f"{key}_{target}_start")
    end = b.date_input("계약 종료일", value=_iso(values.get("end_date")) or _iso(current.get("end_date")),
                       min_value=date(1900, 1, 1), max_value=date(2100, 12, 31), key=f"{key}_{target}_end")
    data = dict(person_id=person["id"], company=company, contract_no=number, status=status,
                start_date=start, end_date=end, assignment_id=current.get("assignment_id"),
                previous_contract_id=current.get("previous_contract_id"), note=current.get("note"))

    def preview():
        clean = validate_contract(data)
        fields = (("업체명", "company"), ("계약번호", "contract_no"), ("계약 상태", "status"),
                  ("계약 시작일", "start_date"), ("계약 종료일", "end_date"))
        return [("인력", person["name"], person["name"])] + [
            (label, _shown(current.get(k)) if current else "(신규)", _shown(clean[k])) for label, k in fields]

    def run(token):
        service.save_contract(data, token=token, actor=actor, contract_id=current.get("id"),
                              expected_version=current.get("version"))
        return f"{person['name']}의 구매계약을 {'수정' if current else '등록'}했습니다."

    return (tuple(sorted((k, str(v)) for k, v in data.items())), target), preview, run


def _contract_delete_form(service, key, values, actor):
    person = _person(service, key, values, CONTRACT_CATEGORIES)
    if not person:
        return None
    contracts = {c["id"]: c for c in service.contracts(person["id"])}
    if not contracts:
        st.info("이 인력에 등록된 구매계약이 없습니다.")
        return None
    ids = list(contracts)
    target = st.selectbox("삭제할 계약", ids, index=_index(ids, values.get("contract_id"), None),
                          placeholder="계약을 선택하세요", key=f"{key}_target",
                          format_func=lambda i: f"{contracts[i]['contract_no'] or '번호 미발급'} · "
                                                f"{contracts[i]['start_date']}~{contracts[i]['end_date']}")
    if not target:
        return None
    c = contracts[target]

    def preview():
        return [("인력", person["name"], person["name"]), ("계약번호", _shown(c["contract_no"]), "삭제"),
                ("계약 기간", f"{c['start_date']}~{c['end_date']}", "삭제"), ("상태", c["status"], "삭제")]

    def run(token):
        service.delete_contract(target, token=token, actor=actor, expected_version=c["version"])
        return f"{person['name']}의 계약({c['contract_no'] or '번호 미발급'})을 삭제했습니다."

    return (target, c["version"]), preview, run


def _account_form(service, key, values, actor):
    person = _person(service, key, values)
    if not person:
        return None
    account = st.text_input("계정 ID", value=values.get("account_id") or person["account_id"] or "",
                            key=f"{key}_{person['id']}_account")

    def preview():
        if not account.strip():
            raise ValidationError("계정 ID를 입력하세요.")
        return [("인력", person["name"], person["name"]),
                ("계정 ID", _shown(person["account_id"]), account.strip())]

    def run(token):
        service.save_person(dict(id=person["id"], name=person["name"], company=person["company"],
                                 category=person["category"], account_id=account),
                            token=token, actor=actor, expected_version=person["version"])
        return f"{person['name']}의 계정 ID를 저장했습니다."

    return (person["id"], account, person["version"]), preview, run


def _alert_form(service, key, values, actor):
    alerts = [a for a in service.pending_alerts()
              if not values.get("person_id") or a["person_id"] == values["person_id"]]
    if not alerts:
        st.info("확인할 미확인 계약 종료 알림이 없습니다.")
        return None
    by_id = {a["id"]: a for a in alerts}
    ids = list(by_id)
    alert_id = st.selectbox("확인할 알림", ids, index=0 if len(ids) == 1 else None,
                            placeholder="알림을 선택하세요", key=f"{key}_alert",
                            format_func=lambda i: f"{by_id[i]['name']} · {by_id[i]['company']} · "
                                                  f"종료 {by_id[i]['end_date']} ({by_id[i]['label']})")
    if alert_id is None:
        return None
    a = by_id[alert_id]

    def preview():
        return [("인력", a["name"], a["name"]), ("계약 종료일", a["end_date"], a["end_date"]),
                ("알림 상태", "미확인", "확인")]

    def run(token):
        service.acknowledge_alert(alert_id, actor=actor)
        return f"{a['name']}의 계약 종료 알림을 확인 처리했습니다. (계약 갱신은 별도로 진행하세요.)"

    return (alert_id,), preview, run


FORMS = {"checklist_update": _checklist_form, "contract_save": _contract_form,
         "contract_delete": _contract_delete_form, "account_id": _account_form, "alert_ack": _alert_form}


def render_proposal(service, proposal: dict, key: str, actor: str, turn: dict, call_index: int) -> None:
    """제안 하나를 양식으로 보여주고, 확인을 거쳐 저장한다."""
    action = proposal.get("action")
    done = turn.setdefault("done", {})
    st.markdown(f"**{ACTION_TITLES.get(action, action)}** — Agent가 채운 값입니다. 확인하고 고친 뒤 저장하세요.")
    if call_index in done:
        st.success(done[call_index])
        return
    form = FORMS.get(action)
    if form is None:
        st.warning("지원하지 않는 작업입니다.")
        return
    built = form(service, key, proposal.get("values") or {}, actor)
    if built is None:
        return
    signature, preview, run = built
    state_key = f"{key}_pending"
    pending = st.session_state.get(state_key)
    if pending and pending["signature"] != repr(signature):
        st.session_state.pop(state_key, None)   # 미리보기 뒤 값을 바꾸면 다시 확인
        pending = None
    if st.button("변경 내용 확인", key=f"{key}_preview"):
        try:
            rows = preview()
            if not actor.strip():
                raise ValidationError("왼쪽 위 '변경 주체'를 입력하세요.")
            st.session_state[state_key] = pending = {
                "signature": repr(signature), "rows": rows, "token": str(uuid4())}
        except ValidationError as exc:
            st.error(str(exc))
    if not pending:
        return
    st.table([{"항목": r[0], "현재": r[1], "변경": r[2]} for r in pending["rows"]])
    confirmed = True
    if action == "contract_delete":
        st.warning("계약과 그 알림이 삭제됩니다. 삭제 전 값은 변경 이력에 남습니다.")
        confirmed = st.checkbox("삭제할 계약이 맞는지 확인했습니다.", key=f"{key}_delete_ok")
    ok, cancel = st.columns(2)
    label = "확인 후 삭제" if action == "contract_delete" else "확인 후 저장"
    if ok.button(label, type="primary", key=f"{key}_confirm", disabled=not confirmed):
        if not confirmed:   # 버튼 비활성화와 별개로 코드에서도 확인한다
            st.error("삭제할 계약이 맞는지 확인란을 먼저 선택하세요.")
            return
        try:
            message = run(pending["token"])
            done[call_index] = message
            st.session_state.pop(state_key, None)
            st.session_state["saved_message"] = message
            st.rerun()
        except (ValidationError, ConflictError) as exc:
            st.error(str(exc))
        except sqlite3.Error:
            st.error("저장하지 못했습니다. 입력은 유지됩니다. 다시 시도하세요.")
    if cancel.button("취소", key=f"{key}_cancel"):
        st.session_state.pop(state_key, None)
        st.rerun()
