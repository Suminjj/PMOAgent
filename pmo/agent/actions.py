"""Agent 저장 요청 → 변경 계획(현재 → 변경) 만들기와, 사용자 확인 후 실행.

흐름 (AGENTS.md 5.5)
1. LLM이 prepare_* 도구에 알아낸 값을 넣는다. LLM은 저장하지 못한다.
2. build_plan이 DB를 읽어 대상(인력·투입·계약)을 확정하고, 현재 값과 바뀔 값, 실행할 정해진 업무 함수의 인자를 만든다.
   - 대상이 애매하거나(동명이인, 투입·계약 여러 건) 필요한 값이 없으면 계획을 만들지 않고 ValidationError로
     무엇을 물어봐야 하는지 돌려준다. LLM은 그 내용을 사용자에게 되묻는다.
3. 화면은 계획을 확인 카드로 보여준다. 사용자는 값을 고르지 않고 '확인 후 저장' 또는 '취소'만 누른다.
4. execute_plan이 정해진 업무 함수로 저장한다. 계획을 만든 뒤 데이터가 바뀌었으면 버전 검사로 막힌다.
"""
import difflib
from datetime import date, timedelta
from uuid import uuid4

from pmo.contracts import CONTRACT_CATEGORIES, validate_contract
from pmo.errors import ValidationError
from pmo.service import validate_assignment

TITLES = {
    "contract_save": "구매계약 등록·수정",
    "contract_delete": "구매계약 삭제",
    "account_id": "계정 ID 입력",
    "alert_ack": "계약 종료 알림 확인",
    "assignment_update": "투입 정보 변경",
    "assignment_transfer": "다른 과제로 이동",
    "assignment_replace": "인력 교체",
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


def typo_message(service, given: str) -> str:
    """없는 이름이면 비슷한 이름을 되묻게 한다(v3, v2 평가 T01 김졍직·T03 박헙력). 볼 수 있는 인력 중에서만 제안."""
    from pmo.agent.guardrails import similar_names
    close = similar_names(service, given)
    if close:
        # 작은 모델이 오류 문장을 그대로 옮기므로 사용자에게 바로 보여줄 수 있는 문장으로 쓴다(v3 평가 T01·T03)
        return (f"'{given}'에 해당하는 인력이 없습니다. " + " 또는 ".join(f"'{n}'" for n in close)
                + "을(를) 말씀하신 건가요?")
    return f"'{given}'에 해당하는 인력이 없습니다. 이름을 확인하세요."


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
        raise ValidationError(typo_message(service, given))
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


# --- 투입 변경·이동·교체 (11/03 담당자 요청) ------------------------------------------------
# 투입은 기간이 있는 행이다. '12월만 매출귀속 프로젝트 변경'처럼 기간 일부만 바뀌면 투입을 나눈다.
# 계약이 원래 투입에만 연결돼 있으면
# 연결을 풀어 계약 기간 안의 그 인력 투입 전체를 덮게 한다(검수에서 뒤쪽 투입이 빠지지 않게). [제안]

_ASSIGNMENT_KEYS = ("person_id", "project", "revenue_project", "revenue_team", "start_date", "end_date", "grade",
                    "assignment_type", "mm", "sales_rate", "purchase_rate", "rate_unit")


def _day(value, label: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}을(를) YYYY-MM-DD로 사용자에게 확인하세요.") from exc


def _month(value) -> tuple[date, date]:
    text = str(value).strip()
    try:
        first = date.fromisoformat(f"{text[:7]}-01")
    except ValueError as exc:
        raise ValidationError("어느 달인지 YYYY-MM으로 사용자에게 확인하세요.") from exc
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first, last


def _won(value) -> str:
    if value in (None, ""):
        return "미입력"
    try:
        return f"{int(float(value)):,}원"
    except (TypeError, ValueError):
        return str(value)


def _added(label, after) -> dict:
    """새로 생기는 값(현재 값 없음)."""
    return {"항목": label, "현재": "—", "변경": _shown(after)}


def _period(a: dict) -> str:
    return f"{a['start_date']}~{a['end_date']}"


def _asg_label(a: dict) -> str:
    return f"{a['project'] or '과제 미입력'} {_period(a)}"


def _segment(a: dict, start: date, end: date, **changes) -> dict:
    """기존 투입 값을 이어받은 새 기간의 투입 값(검증 전)."""
    data = {k: a.get(k) for k in _ASSIGNMENT_KEYS}
    data.update(start_date=start.isoformat(), end_date=end.isoformat(), **changes)
    if data.get("purchase_rate") is not None or data.get("sales_rate") is not None:
        data["rate_unit"] = data.get("rate_unit") or "원/MM"
    return data


def _check_split(a: dict) -> None:
    if a["assignment_type"] == "Part time":
        raise ValidationError(f"{_asg_label(a)}은(는) Part time이라 MM을 나눌 기준이 없습니다. "
                              "'과제 투입 관리' 화면에서 기간별 MM을 직접 입력하도록 안내하세요.")


def _pick_assignment(service, person: dict, *, project=None, on: date | None = None,
                     month: tuple[date, date] | None = None) -> dict:
    rows = sorted(service.assignments(person["id"]), key=lambda r: r["start_date"])
    if not rows:
        raise ValidationError(f"{person['name']}에게 등록된 투입이 없습니다.")
    found = rows
    hint = str(project or "").strip()
    if hint:
        found = [a for a in found if hint in (a["project"] or "")] or found
    if on:
        found = [a for a in found if a["start_date"] <= on.isoformat() <= a["end_date"]]
    if month:
        found = [a for a in found if a["start_date"] <= month[1].isoformat() and a["end_date"] >= month[0].isoformat()]
    if not on and not month and not hint:
        today = service.clock().isoformat()
        found = [a for a in found if a["end_date"] >= today] or found
    if len(found) == 1:
        return found[0]
    if not found:
        when = on.isoformat() if on else (f"{month[0]:%Y-%m}" if month else "")
        raise ValidationError(f"{person['name']}의 투입 중 {when} 기간에 해당하는 투입이 없습니다. 투입: "
                              + ", ".join(_asg_label(a) for a in rows) + ". 사용자에게 확인하세요.")
    raise ValidationError(f"{person['name']}의 투입이 {len(found)}건입니다: "
                          + ", ".join(_asg_label(a) for a in found)
                          + ". 어느 투입(과제·기간)인지 사용자에게 확인하세요.")


def _known_project(service, name: str) -> tuple[str, str | None]:
    """기존 과제명과 맞춰 본다. 오타로 보이면 비슷한 과제명을 제안하며 되묻는다. (과제명, 경고)"""
    name = str(name or "").strip()
    if not name:
        raise ValidationError("옮길 과제명을 사용자에게 확인하세요.")
    known = sorted({a["project"] for a in service.assignments() if a["project"]})
    if name in known:
        return name, None
    folded = {k.casefold().replace(" ", ""): k for k in known}
    if name.casefold().replace(" ", "") in folded:
        return folded[name.casefold().replace(" ", "")], None
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
    if close:
        raise ValidationError(f"'{name}' 과제는 없습니다. '{close[0]}' 과제가 맞는지 사용자에게 확인하세요.")
    return name, "처음 보는 과제명입니다(기존 투입에 없음). 맞는지 확인하세요."


def _linked_contracts(service, person: dict, assignment: dict) -> list[dict]:
    return [c for c in service.contracts(person["id"]) if c["assignment_id"] == assignment["id"]]


def _unlink(contracts: list[dict], rows: list[dict], changes: list[dict], after: date) -> None:
    """원래 투입에만 연결된 계약(after 이후까지 이어지는 것)의 연결을 풀어 그 인력의 투입 전체를 덮게 한다."""
    for c in contracts:
        if c["end_date"] > after.isoformat():
            data = {k: c[k] for k in ("id", "person_id", "company", "contract_no", "start_date", "end_date",
                                      "status", "previous_contract_id", "note")}
            changes.append({"table": "contracts", "data": dict(data, assignment_id=None),
                            "expected_version": c["version"]})
            rows.append(_row(f"계약 {_contract_label(c)} 연결 투입", "이 투입 한 건",
                             "연결 없음 (계약 기간 안의 이 인력 투입 전체)"))


def _rate(values: dict, current=None):
    value = values.get("purchase_rate")
    return current if value in (None, "") else str(value).replace(",", "").replace("원", "").strip()


def _plan_assignment_update(service, values: dict) -> dict:
    """투입 값 변경. month(그 달만)·effective_date(그날부터)가 기간 일부면 투입을 나눈다."""
    person = _person(service, values)
    month = _month(values["month"]) if values.get("month") else None
    effective = _day(values["effective_date"], "적용 시작일") if values.get("effective_date") else None
    if month and effective:
        raise ValidationError("'그 달만'인지 '그날부터'인지 사용자에게 확인하세요.")
    a = _pick_assignment(service, person, project=values.get("project"), on=effective, month=month)
    changes = {}
    if "revenue_project" in values:
        changes["revenue_project"] = str(values["revenue_project"]).strip()
    if "grade" in values:
        changes["grade"] = str(values["grade"]).strip()
    if "purchase_rate" in values:
        changes["purchase_rate"] = _rate(values)
    dates = {k: _day(values[k], label).isoformat() for k, label in (("start_date", "시작일"), ("end_date", "종료일"))
             if k in values}
    if dates and (month or effective):
        raise ValidationError("기간을 바꾸는 요청과 특정 달만 바꾸는 요청을 함께 처리할 수 없습니다. 하나씩 요청하도록 안내하세요.")
    if not changes and not dates:
        raise ValidationError(f"{person['name']}의 {_asg_label(a)}에서 무엇을 바꿀지(매출귀속 프로젝트·등급·매입단가·기간) "
                              "사용자에게 확인하세요.")
    labels = {"revenue_project": "매출귀속 프로젝트", "grade": "등급", "purchase_rate": "매입단가(원/MM)",
              "start_date": "시작일", "end_date": "종료일"}
    differs = {k: v for k, v in {**changes, **dates}.items()
               if str(v) != str(a.get(k) if k != "purchase_rate" or a.get(k) is None else int(float(a[k])))}
    if not differs:
        raise ValidationError(f"{person['name']}의 {_asg_label(a)}은(는) 이미 그 값입니다. 바뀌는 값이 없습니다.")
    start, end = date.fromisoformat(a["start_date"]), date.fromisoformat(a["end_date"])
    seg_start = max(start, month[0]) if month else max(start, effective) if effective else start
    seg_end = min(end, month[1]) if month else end
    rows = [_row("인력", _person_label(person), _person_label(person)), _row("대상 투입", _asg_label(a), _asg_label(a))]
    rows += [_row(labels[k], _won(a.get(k)), _won(v)) if k == "purchase_rate" else _row(labels[k], a.get(k), v)
             for k, v in differs.items()]
    plan_changes = []
    if dates or (seg_start == start and seg_end == end):
        data = validate_assignment(_segment(a, date.fromisoformat(dates.get("start_date", a["start_date"])),
                                            date.fromisoformat(dates.get("end_date", a["end_date"])), **changes))
        plan_changes.append({"table": "assignments", "data": dict(data, id=a["id"]), "expected_version": a["version"]})
        if dates:
            rows.append(_row("투입 MM", a["mm"], data["mm"]))
        summary = f"{person['name']}의 {_asg_label(a)} 변경"
    else:
        _check_split(a)
        parts = []
        if seg_start > start:
            parts.append((start, seg_start - timedelta(days=1), {}))
        parts.append((seg_start, seg_end, changes))
        if seg_end < end:
            parts.append((seg_end + timedelta(days=1), end, {}))
        shown = []
        for i, (s, e, ch) in enumerate(parts):
            data = validate_assignment(_segment(a, s, e, **ch))
            if i == 0:
                plan_changes.append({"table": "assignments", "data": dict(data, id=a["id"]),
                                     "expected_version": a["version"]})
            else:
                plan_changes.append({"table": "assignments", "data": dict(data, id=str(uuid4()))})
            shown.append(f"{s}~{e} {data['mm']}MM" + (" (변경)" if ch else " (기존 값)"))
        rows.append(_row("투입 나누기", f"{_period(a)} 한 건 {a['mm']}MM", " / ".join(shown)))
        _unlink(_linked_contracts(service, person, a), rows, plan_changes, seg_start - timedelta(days=1))
        summary = f"{person['name']}의 {seg_start}~{seg_end} 투입 변경(투입 {len(parts)}건으로 나눔)"
    return {"rows": rows, "check": None, "params": {"changes": plan_changes}, "summary": summary}


def _plan_assignment_transfer(service, values: dict) -> dict:
    """같은 인력이 기존 과제에서 빠지고 다른 과제로 옮긴다: 기존 투입은 이동 전날 종료, 새 과제 투입 등록."""
    person = _person(service, values)
    move = _day(values.get("start_date"), "이동일(새 과제 시작일)")
    to_project, warning = _known_project(service, values.get("to_project"))
    a = _pick_assignment(service, person, project=values.get("from_project"), on=move)
    if a["project"] == to_project:
        raise ValidationError(f"{person['name']}은(는) {move}에 이미 {to_project} 과제에 투입되어 있습니다.")
    if move.isoformat() <= a["start_date"]:
        raise ValidationError(f"이동일 {move}이(가) 기존 투입 시작일({a['start_date']})보다 늦어야 합니다. "
                              "투입 과제 자체를 바꾸려면 '과제 투입 관리' 화면을 안내하세요.")
    _check_split(a)
    end = _day(values["end_date"], "새 과제 종료일") if values.get("end_date") else date.fromisoformat(a["end_date"])
    old_end = move - timedelta(days=1)
    revenue = str(values.get("revenue_project") or "").strip() or None
    old = validate_assignment(_segment(a, date.fromisoformat(a["start_date"]), old_end))
    new_id = str(uuid4())
    new = validate_assignment(_segment(a, move, end, project=to_project, revenue_project=revenue, revenue_team=None,
                                       purchase_rate=_rate(values, a.get("purchase_rate"))))
    others = [o for o in service.assignments(person["id"]) if o["id"] != a["id"]
              and o["start_date"] <= new["end_date"] and o["end_date"] >= new["start_date"]]
    if others:
        raise ValidationError(f"{person['name']}의 새 투입 기간이 다른 투입과 겹칩니다: "
                              + ", ".join(_asg_label(o) for o in others) + ". 종료일을 사용자에게 확인하세요.")
    rows = [_row("인력", _person_label(person), _person_label(person)),
            _row("기존 투입", _asg_label(a) + f" {a['mm']}MM",
                 f"{a['project']} {a['start_date']}~{old_end} {old['mm']}MM"),
            _added("새 투입 과제", to_project),
            _added("새 투입 기간", f"{move}~{end} {new['mm']}MM ({new['assignment_type']})"),
            _added("새 투입 매출귀속 프로젝트", revenue or "미정 (나중에 보완)"),
            _added("새 투입 등급·매입단가", f"{new['grade'] or '미입력'} · {_won(new['purchase_rate'])} (기존 투입과 같게)")]
    if warning:
        rows.append(_added("확인", warning))
    changes = [{"table": "assignments", "data": dict(old, id=a["id"]), "expected_version": a["version"]},
               {"table": "assignments", "data": dict(new, id=new_id)}]
    _unlink(_linked_contracts(service, person, a), rows, changes, old_end)
    return {"rows": rows, "check": None, "params": {"changes": changes},
            "summary": f"{person['name']} {move}부터 {a['project']} → {to_project} 이동"}


def _plan_assignment_replace(service, values: dict) -> dict:
    """과제 인력 교체: 기존 인력 투입은 교체 전날 종료, 교체 인력이 같은 과제에 새로 투입. 계약도 함께 맞춘다."""
    old_person = _person(service, values)
    if not values.get("new_person_id"):
        raise ValidationError("누구로 교체할지(교체 인력 이름) 사용자에게 확인하세요.")
    new_person = _person(service, {"person_id": values["new_person_id"]})
    if new_person["id"] == old_person["id"]:
        raise ValidationError("기존 인력과 교체 인력이 같습니다. 사용자에게 확인하세요.")
    if not service.assignments(old_person["id"]) and service.assignments(new_person["id"]):
        # 모델이 기존·교체 인력을 바꿔 넣음(v3 평가 X01 전문): 투입이 없는 사람을 교체할 수는 없으니 자리를 바로잡는다.
        # 결과는 확인 카드로 사용자가 보고 확정한다.
        old_person, new_person = new_person, old_person
    start = _day(values.get("start_date"), "교체일(교체 인력 투입 시작일)")
    a = _pick_assignment(service, old_person, project=values.get("project"), on=start)
    if start.isoformat() <= a["start_date"]:
        raise ValidationError(f"교체일 {start}이(가) {old_person['name']}의 투입 시작일({a['start_date']})보다 늦어야 합니다.")
    grade = str(values.get("grade") or "").strip() or None
    if grade is None and new_person["category"] != "컨설팅":
        raise ValidationError(f"교체 인력 {new_person['name']}의 등급(L7·L6·L5 등)을 사용자에게 확인하세요.")
    kind = a["assignment_type"]
    if kind == "Part time":
        raise ValidationError(f"{_asg_label(a)}은(는) Part time입니다. 교체 인력의 MM을 '과제 투입 관리' 화면에서 입력하도록 안내하세요.")
    end = _day(values["end_date"], "교체 인력 종료일") if values.get("end_date") else date.fromisoformat(a["end_date"])
    old_end = start - timedelta(days=1)
    revenue = str(values.get("revenue_project") or "").strip() or a["revenue_project"]
    old = validate_assignment(_segment(a, date.fromisoformat(a["start_date"]), old_end))
    new_id = str(uuid4())
    rate = _rate(values)
    new = validate_assignment({"person_id": new_person["id"], "project": a["project"], "revenue_project": revenue,
                               "revenue_team": a.get("revenue_team"), "start_date": start.isoformat(),
                               "end_date": end.isoformat(), "grade": grade, "assignment_type": kind,
                               "purchase_rate": rate, "rate_unit": "원/MM" if rate else None})
    clash = [o for o in service.assignments(new_person["id"])
             if o["start_date"] <= new["end_date"] and o["end_date"] >= new["start_date"]]
    if clash:
        raise ValidationError(f"{new_person['name']}은(는) 그 기간에 이미 투입이 있습니다: "
                              + ", ".join(_asg_label(o) for o in clash) + ". 사용자에게 확인하세요.")
    rows = [_row("과제", a["project"], a["project"]),
            _row("기존 인력", f"{_person_label(old_person)} · {_period(a)} {a['mm']}MM",
                 f"{a['start_date']}~{old_end} {old['mm']}MM (투입 종료)"),
            _added("교체 인력", f"{_person_label(new_person)} · {start}~{end} {new['mm']}MM ({kind})"),
            _added("교체 인력 매출귀속 프로젝트", (revenue or "미정")
                 + (" (기존 인력과 같게)" if revenue == a["revenue_project"] else "")),
            _added("교체 인력 등급·매입단가", f"{grade or '없음(컨설팅)'} · {_won(rate)}")]
    changes = [{"table": "assignments", "data": dict(old, id=a["id"]), "expected_version": a["version"]},
               {"table": "assignments", "data": dict(new, id=new_id)}]
    for c in service.contracts(old_person["id"]):
        covers = c["assignment_id"] == a["id"] or (not c["assignment_id"] and c["start_date"] <= old_end.isoformat())
        if covers and c["end_date"] > old_end.isoformat() and c["start_date"] <= old_end.isoformat():
            data = {k: c[k] for k in ("id", "person_id", "assignment_id", "company", "contract_no", "start_date",
                                      "status", "previous_contract_id", "note")}
            changes.append({"table": "contracts", "data": dict(data, end_date=old_end.isoformat()),
                            "expected_version": c["version"]})
            rows.append(_row(f"기존 인력 계약 {c['contract_no'] or '번호 미발급'} 종료일", c["end_date"], old_end.isoformat()))
    if new_person["category"] in CONTRACT_CATEGORIES:
        changes.append({"table": "contracts", "data": {
            "person_id": new_person["id"], "assignment_id": new_id, "company": new_person["company"],
            "contract_no": None, "start_date": start.isoformat(), "end_date": end.isoformat(), "status": "준비"}})
        rows.append(_added("교체 인력 구매계약", f"{new_person['company']} {start}~{end} 준비 (계약번호 발급 후 입력)"))
    return {"rows": rows, "check": "교체 인력과 교체일이 맞는지 확인했습니다.", "params": {"changes": changes},
            "summary": f"{a['project']} {old_person['name']} → {new_person['name']} 교체 ({start}부터)"}


_BUILDERS = {"contract_save": _plan_contract_save,
             "contract_delete": _plan_contract_delete, "account_id": _plan_account, "alert_ack": _plan_alert,
             "assignment_update": _plan_assignment_update, "assignment_transfer": _plan_assignment_transfer,
             "assignment_replace": _plan_assignment_replace}


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
    if action == "contract_save":
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
    elif action in ("assignment_update", "assignment_transfer", "assignment_replace"):
        service.apply_assignment_changes(p["changes"], token=token, actor=actor)
    else:
        raise ValidationError("지원하지 않는 작업입니다.")
    return f"{plan['summary']} — 저장했습니다."


def request_target(service, plan: dict, values: dict) -> tuple[str, str | None]:
    """과제 리더의 수정 요청이 걸리는 과제와 인력(11/08 수정 요청 승인). 리더가 볼 수 있는 투입 중 담당 과제에서 고른다."""
    person_id, _ = resolve_person(service, values.get("person_id") or values.get("name"))
    projects = [a["project"] for a in service.assignments(person_id) if a.get("project")] if person_id else []
    project = next((p for p in projects if service.access is None or service.access.sees_project(p)), None)
    if project is None:
        mine = sorted(getattr(service.access, "projects", ()) or ())
        project = mine[0] if mine else None
    if project is None:
        raise ValidationError("요청할 담당 과제를 찾지 못했습니다. 왼쪽 메뉴 '수정 요청'에서 직접 남겨 주세요.")
    return project, person_id


def request_text(plan: dict) -> str:
    """수정 요청 목록에 보일 내용: 카드 제목과 '현재 → 변경' 줄."""
    lines = [plan["title"]] + [f"- {r.get('항목', '')}: {r.get('현재', '')} → {r.get('변경', '')}" for r in plan["rows"]]
    return "\n".join(lines)


def approve_request(service, request: dict, *, actor: str, checked: bool = False) -> str:
    """PM·PMO가 수정 요청을 승인한다: 승인하는 사람 권한으로 계획을 다시 만들어 실행하고 요청을 '처리 완료'로 닫는다.

    리더가 보낸 계획은 리더 권한으로 만든 것이라(단가 칸 비움 등) 그대로 쓰지 않는다. 요청 뒤 데이터가 바뀌어 계획을
    만들 수 없으면 오류(되묻기 문장)를 그대로 보여 준다. 요청 토큰으로 같은 승인을 두 번 저장하지 않는다.
    """
    action = request.get("action") or {}
    plan = build_plan(service, action["action"], action.get("values") or {})
    message = execute_plan(service, plan, token=f"change-request:{request['id']}", actor=actor, checked=checked)
    service.handle_change_request(request["id"], "처리 완료", f"승인 후 반영 · {message}")
    return message
