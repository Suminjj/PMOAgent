"""Agent 저장 요청 → 변경 계획(현재 → 변경) 만들기와, 사용자 확인 후 실행.

흐름 (AGENTS.md 5.5)
1. LLM이 prepare_* 도구에 알아낸 값을 넣는다. LLM은 저장하지 못한다.
2. build_plan이 DB를 읽어 대상(인력·투입·계약)을 확정하고, 현재 값과 바뀔 값, 실행할 정해진 업무 함수의 인자를 만든다.
   - 대상이 애매하거나(동명이인, 투입·계약 여러 건) 필요한 값이 없으면 계획을 만들지 않고 ValidationError로
     무엇을 물어봐야 하는지 돌려준다. LLM은 그 내용을 사용자에게 되묻는다.
3. 화면은 계획을 확인 카드로 보여준다. 사용자는 값을 고르지 않고 '확인 후 저장' 또는 '취소'만 누른다.
4. execute_plan이 정해진 업무 함수로 저장한다. 계획을 만든 뒤 데이터가 바뀌었으면 버전 검사로 막힌다.
"""
from datetime import date

from pmo.checklist import ITEMS, validate_change
from pmo.contracts import CONTRACT_CATEGORIES, validate_contract
from pmo.errors import ValidationError

TITLES = {
    "checklist_update": "근무 환경 준비 상태 변경",
    "contract_save": "구매계약 등록·수정",
    "contract_delete": "구매계약 삭제",
    "account_id": "계정 ID 입력",
    "alert_ack": "계약 종료 알림 확인",
}
_TITLE_WORDS = ("차장", "과장", "대리", "부장", "사원", "주임", "책임", "선임", "수석", "님", "씨")


def _shown(value) -> str:
    return "미입력" if value in (None, "") else str(value)


def _person_label(p: dict) -> str:
    return f"{p['name']} · {p['company'] or '업체 미입력'} · {p['id']}"


def resolve_person(service, value: str | None) -> tuple[str | None, list[dict]]:
    """관리번호가 아니라 이름(직함 포함)이 들어와도 사람을 찾는다. (확정 id 또는 None, 후보 목록)

    작은 로컬 모델은 find_people을 건너뛰고 person_id에 이름을 넣는 일이 있어 코드에서 보완한다.
    """
    value = str(value or "").strip()
    if not value:
        return None, []
    people = service.people()
    if any(p["id"] == value for p in people):
        return value, []
    for word in _TITLE_WORDS:
        value = value.removesuffix(word).strip()
    exact = [p for p in people if p["name"] == value]
    candidates = exact or (service.find_people(value) if value else [])
    if len(candidates) == 1:
        return candidates[0]["id"], []
    return None, [{k: c[k] for k in ("id", "name", "company", "category")} for c in candidates]


def _person(service, values: dict, categories=None) -> dict:
    given = values.get("person_id")
    if not given:
        raise ValidationError("누구의 정보를 바꿀지 알 수 없습니다. 사용자에게 대상 인력의 이름을 물어보세요.")
    pid, candidates = resolve_person(service, given)
    if not pid and candidates:
        raise ValidationError(f"'{given}'에 해당하는 인력이 {len(candidates)}명입니다: "
                              + "; ".join(_person_label(c) for c in candidates)
                              + ". 어느 분인지 사용자에게 확인하세요.")
    if not pid:
        raise ValidationError(f"'{given}'에 해당하는 인력이 없습니다. 이름을 확인하세요.")
    person = next(p for p in service.people() if p["id"] == pid)
    if categories and person["category"] not in categories:
        raise ValidationError(f"{person['name']}은(는) '{person['category']}' 분류라 구매계약 대상이 아닙니다.")
    return person


def _contract_label(c: dict) -> str:
    return f"{c['contract_no'] or '번호 미발급'}({c['start_date']}~{c['end_date']}, {c['status']})"


def _pick_contract(service, person: dict, given: str | None, *, allow_single: bool) -> dict | None:
    """계약 id 또는 계약번호로 그 인력의 계약을 찾는다. 없으면 한 건뿐일 때만 그 계약을 쓴다."""
    contracts = service.contracts(person["id"])
    given = str(given or "").strip()
    if given:
        found = [c for c in contracts if given in (c["id"], c["contract_no"])]
        if len(found) == 1:
            return found[0]
        raise ValidationError(f"{person['name']}의 계약 중 '{given}'을(를) 찾지 못했습니다. 계약: "
                              + ", ".join(_contract_label(c) for c in contracts) + ". 어느 계약인지 확인하세요.")
    if allow_single and len(contracts) == 1:
        return contracts[0]
    if len(contracts) > 1:
        raise ValidationError(f"{person['name']}의 계약이 {len(contracts)}건입니다: "
                              + ", ".join(_contract_label(c) for c in contracts)
                              + ". 어느 계약인지(계약번호) 사용자에게 확인하세요.")
    return None


def _row(label, before, after) -> dict:
    return {"항목": label, "현재": _shown(before), "변경": _shown(after)}


# --- 작업별 계획 ------------------------------------------------------------------------

def _plan_checklist(service, values: dict) -> dict:
    person = _person(service, values)
    item, status = values.get("item"), values.get("status")
    if item not in ITEMS:
        raise ValidationError("어느 항목(방문예약·계정 신청·ID카드 신청·OA기기 신청)인지 사용자에게 확인하세요.")
    if not status:
        raise ValidationError("어떤 상태(미진행·진행 중·완료·해당 없음)로 바꿀지 사용자에게 확인하세요.")
    today = service.clock().isoformat()
    live = [a for a in service.assignments(person["id"]) if a["end_date"] >= today]
    hint = str(values.get("project") or "").strip()
    if hint:
        live = [a for a in live if hint in (a["project"] or "")] or live
    if not live:
        raise ValidationError(f"{person['name']}의 진행 중이거나 예정된 투입이 없습니다.")
    if len(live) > 1:
        raise ValidationError(f"{person['name']}의 투입이 {len(live)}건입니다: "
                              + ", ".join(f"{a['project'] or '과제 미입력'}({a['start_date']}~{a['end_date']})"
                                          for a in live) + ". 어느 과제의 준비 항목인지 확인하세요.")
    assignment = live[0]
    row = next(r for r in service.checklist(assignment["id"]) if r["item"] == item)
    reason = values.get("reason")
    reason = row["reason"] if reason is None else str(reason).strip()
    validate_change(item, status, reason, actual_done=True)   # '완료'의 실제 신청 확인은 화면 확인란에서 받는다
    if status == row["status"] and reason == row["reason"]:
        raise ValidationError(f"{person['name']}의 {item}은(는) 이미 '{status}' 상태입니다.")
    return {
        "rows": [_row("인력", _person_label(person), _person_label(person)),
                 _row("투입", f"{assignment['project'] or '과제 미입력'} ({assignment['start_date']}~)", "변경 없음"),
                 _row("항목", item, item), _row("상태", row["status"], status),
                 _row("사유·메모", row["reason"], reason)],
        "check": "사내 시스템에서 실제 신청·예약을 마쳤습니다." if status == "완료" else None,
        "params": {"assignment_id": assignment["id"], "item": item, "status": status, "reason": reason,
                   "expected_version": row["version"]},
        "summary": f"{person['name']}의 {item}: {row['status']} → {status}",
    }


def _plan_contract_save(service, values: dict) -> dict:
    person = _person(service, values, CONTRACT_CATEGORIES)
    new = values.get("mode") == "신규"
    current = None if new else _pick_contract(service, person, values.get("contract_id"), allow_single=True)
    cur = current or {}
    number = values.get("contract_no") or cur.get("contract_no")
    data = {
        "person_id": person["id"], "company": values.get("company") or cur.get("company") or person["company"],
        "contract_no": number,
        "status": values.get("status") or cur.get("status") or ("확정" if number else "준비"),
        "start_date": values.get("start_date") or cur.get("start_date"),
        "end_date": values.get("end_date") or cur.get("end_date"),
        "assignment_id": cur.get("assignment_id"), "previous_contract_id": cur.get("previous_contract_id"),
        "note": cur.get("note"),
    }
    if not current and not (data["start_date"] and data["end_date"]):
        raise ValidationError(f"{person['name']}의 새 계약을 등록하려면 계약 시작일과 종료일이 필요합니다. "
                              "사용자에게 확인하세요.")
    clean = validate_contract(data)
    fields = (("업체명", "company"), ("계약번호", "contract_no"), ("계약 상태", "status"),
              ("계약 시작일", "start_date"), ("계약 종료일", "end_date"))
    if current and all(clean[k] == cur.get(k) for _, k in fields):
        raise ValidationError(f"{person['name']}의 계약 {_contract_label(cur)}에서 바뀌는 값이 없습니다.")
    rows = [_row("인력", _person_label(person), _person_label(person)),
            _row("대상 계약", _contract_label(cur) if current else "(없음)", "수정" if current else "신규 등록")]
    rows += [_row(label, cur.get(k) if current else None, clean[k]) for label, k in fields]
    return {
        "rows": rows, "check": None,
        "params": {"data": clean, "contract_id": cur.get("id"), "expected_version": cur.get("version")},
        "summary": f"{person['name']}의 구매계약 {'수정' if current else '신규 등록'}",
    }


def _plan_contract_delete(service, values: dict) -> dict:
    person = _person(service, values, CONTRACT_CATEGORIES)
    contract = _pick_contract(service, person, values.get("contract_id"), allow_single=True)
    if not contract:
        raise ValidationError(f"{person['name']}에게 등록된 구매계약이 없습니다.")
    if contract["has_successor"]:
        raise ValidationError(f"{_contract_label(contract)}은(는) 연장 계약이 이전 계약으로 지정하고 있어 삭제할 수 없습니다.")
    return {
        "rows": [_row("인력", _person_label(person), _person_label(person)),
                 _row("계약", _contract_label(contract), "삭제"),
                 _row("계약 종료 알림", "함께 삭제", "삭제")],
        "check": "삭제할 계약이 맞는지 확인했습니다. (삭제 전 값은 변경 이력에 남습니다)",
        "params": {"contract_id": contract["id"], "expected_version": contract["version"]},
        "summary": f"{person['name']}의 계약 {_contract_label(contract)} 삭제",
    }


def _plan_account(service, values: dict) -> dict:
    person = _person(service, values)
    account = str(values.get("account_id") or "").strip()
    if not account:
        raise ValidationError(f"{person['name']}의 계정 ID를 사용자에게 확인하세요.")
    if account == person["account_id"]:
        raise ValidationError(f"{person['name']}의 계정 ID는 이미 {account}입니다.")
    return {
        "rows": [_row("인력", _person_label(person), _person_label(person)),
                 _row("계정 ID", person["account_id"], account)],
        "check": None,
        "params": {"person": {k: person[k] for k in ("id", "name", "company", "category")},
                   "account_id": account, "expected_version": person["version"]},
        "summary": f"{person['name']}의 계정 ID 입력",
    }


def _plan_alert(service, values: dict) -> dict:
    alerts = service.pending_alerts()
    if values.get("person_id"):
        person = _person(service, values)
        alerts = [a for a in alerts if a["person_id"] == person["id"]]
    if not alerts:
        raise ValidationError("확인할 미확인 계약 종료 알림이 없습니다.")
    if len(alerts) > 1:
        raise ValidationError(f"미확인 알림이 {len(alerts)}건입니다: "
                              + ", ".join(f"{a['name']}({a['company']}, 종료 {a['end_date']})" for a in alerts)
                              + ". 누구의 알림인지 확인하세요.")
    a = alerts[0]
    return {
        "rows": [_row("인력", f"{a['name']} · {a['company']}", f"{a['name']} · {a['company']}"),
                 _row("계약 종료일", f"{a['end_date']} ({a['label']})", "변경 없음"),
                 _row("알림 상태", "미확인", "확인")],
        "check": None,
        "params": {"alert_id": a["id"]},
        "summary": f"{a['name']}의 계약 종료 알림 확인 (계약 갱신 완료는 아님)",
    }


_BUILDERS = {"checklist_update": _plan_checklist, "contract_save": _plan_contract_save,
             "contract_delete": _plan_contract_delete, "account_id": _plan_account, "alert_ack": _plan_alert}


def build_plan(service, action: str, values: dict) -> dict:
    """LLM이 넣은 값으로 변경 계획을 만든다. 저장하지 않는다."""
    values = {k: v for k, v in (values or {}).items() if v not in (None, "")}
    plan = _BUILDERS[action](service, values)
    return dict(plan, action=action, title=TITLES[action], built_on=date.today().isoformat())


def execute_plan(service, plan: dict, *, token: str, actor: str, checked: bool = False) -> str:
    """사용자가 확인한 계획을 정해진 업무 함수로 실행한다."""
    if plan.get("check") and not checked:
        raise ValidationError("확인란을 먼저 선택하세요.")
    p, action = plan["params"], plan["action"]
    if action == "checklist_update":
        service.update_checklist_item(p["assignment_id"], p["item"], p["status"], p["reason"],
                                      actual_done=checked, token=token, actor=actor,
                                      expected_version=p["expected_version"])
    elif action == "contract_save":
        service.save_contract(p["data"], token=token, actor=actor, contract_id=p["contract_id"],
                              expected_version=p["expected_version"])
    elif action == "contract_delete":
        service.delete_contract(p["contract_id"], token=token, actor=actor,
                                expected_version=p["expected_version"])
    elif action == "account_id":
        service.save_person(dict(p["person"], account_id=p["account_id"]), token=token, actor=actor,
                            expected_version=p["expected_version"])
    elif action == "alert_ack":
        service.acknowledge_alert(p["alert_id"], actor=actor)
    else:
        raise ValidationError("지원하지 않는 작업입니다.")
    return f"{plan['summary']} — 저장했습니다."
