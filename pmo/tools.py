"""향후 Agent가 호출할 도구 계층.

- 도구는 화면과 같은 업무 함수(Service)를 호출한다. 계산·저장 로직을 여기 새로 만들지 않는다.
- 입력은 스키마로 검증하고, 결과는 항상 {"ok", "tool", "data" | "error", "duration_ms"} 형식이다.
- 조회: 정해진 조회 도구 + 그 밖의 조회는 query_database(LlamaIndex Text-to-SQL, SELECT만·읽기 전용).
- 등록·수정·삭제: prepare_* 도구는 LLM이 알아낸 값으로 변경 계획(현재 → 변경)을 만들 뿐 저장하지 않는다.
  대상이 애매하면 계획 대신 되물을 내용을 돌려준다. 화면이 계획을 확인 카드로 보여주고,
  사용자가 확인해야 정해진 업무 함수가 저장한다.
- LLM 제공자·모델·프롬프트와 무관하다. 어떤 LLM이든 `tool_specs()`로 도구 목록을 받고
  `call_tool()`로 실행하면 된다.
"""
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from pmo.agent.actions import _person, build_plan
from pmo.agent.llm import LLMError
from pmo.agent.text_to_sql import query_database
from pmo.checklist import ITEMS as CHECKLIST_ITEMS, STATUSES as CHECKLIST_STATUSES
from pmo.config import TIMEZONE
from pmo.contracts import CONTRACT_STATUSES
from pmo.errors import ConflictError, ValidationError
from pmo.service import Service

IMPLEMENTED, PLANNED = "구현", "구현 예정"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    status: str = IMPLEMENTED
    handler: Callable[[Service, dict], Any] | None = field(default=None, repr=False)


def _obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


STR = {"type": "string"}
DATE = {"type": "string", "description": "YYYY-MM-DD"}
PERSON = {"type": "string", "description": "대상 인력. find_people 결과의 id(예: DEMO-002) 또는 사용자가 말한 이름. 비우지 않는다."}


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


PROPOSAL_NOTE = ("저장하지 않았습니다. 화면 오른쪽에 '현재 → 변경' 확인 카드를 띄웠습니다. "
                 "사용자가 '확인 후 저장'을 눌러야 반영됩니다.")


def _plan(action: str):
    """LLM이 넣은 값으로 변경 계획(현재 → 변경)을 만든다. 저장하지 않는다(pmo/agent/actions.py)."""
    def handler(service: Service, args: dict) -> dict:
        return {"plan": build_plan(service, action, args), "message": PROPOSAL_NOTE}
    return handler
INT = {"type": "integer"}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("find_people", "이름 일부로 인력 후보를 찾는다. 동명이인이 있으면 여러 명이 나온다. "
         "1명이면 summary에 그 사람의 현재·예정·과거 투입(과제·기간·매출귀속 프로젝트·투입 Type·MM)과 구매계약 요약이 함께 온다. "
         "summary로 답할 수 있으면 바로 답하고, 이후 도구는 결과의 내부 관리번호(id)로 호출한다.",
         _obj({"name": {"type": "string", "description": "사람 이름(예: 조협력). 직함은 빼도 된다"},
               "company": {"type": "string", "description": "질문에 업체명이 따로 있을 때만(예: '감마테크 김협력'의 감마테크). 이름을 넣지 않는다"}}),
         # name을 스키마 필수로 두지 않는다: 이름을 company에 넣는 실수를 코드에서 바로잡기 위함(빈 이름은 _find_people이 거절)
         handler=lambda s, a: _find_people(s, a.get("name"), a.get("company"))),
    Tool("get_assignment_timeline", "인력의 현재·과거·예정 투입을 조회한다. 투입마다 과제, 매출귀속 프로젝트, 기간, 등급, "
         "투입 Type(Full time·Part time), 투입 MM이 있다. 한 사람의 매출귀속 프로젝트(월별 포함)·등급·MM을 묻는 질문도 이 도구로 답한다.",
         _obj({"person_id": PERSON}, ("person_id",)),
         handler=lambda s, a: _timeline(s, _pid(s, a["person_id"]))),
    Tool("get_project_members", "과제(투입 과제, 예: APC Analysis)의 인력 목록을 조회한다. 사람마다 매출귀속 프로젝트·기간·"
         "투입 Type·MM과, 매출귀속 프로젝트별로 묶은 이름(by_revenue_project)이 있다. 기본은 현재·예정 투입, "
         "과거까지 보려면 include_past=true. 과제명은 사람 이름이 아니니 find_people에 넣지 않는다.",
         _obj({"project": {"type": "string", "description": "과제명(사용자가 말한 그대로)"},
               "include_past": {"type": "boolean"}}, ("project",)),
         handler=lambda s, a: s.project_members(a["project"], include_past=bool(a.get("include_past")))),
    Tool("list_contracts", "인력의 구매계약(계약번호·기간·상태·연장 여부)을 조회한다.",
         _obj({"person_id": PERSON}, ("person_id",)),
         handler=lambda s, a: s.contracts(_pid(s, a["person_id"]))),
    Tool("contracts_ending", "특정 연·월(선택: 업체)에 계약이 종료되는 인력 목록을 조회한다.",
         _obj({"year": INT, "month": INT, "company": STR}, ("year", "month")),
         handler=lambda s, a: s.contracts_ending(a["year"], a["month"], a.get("company"))),
    Tool("checklist_status", "근무 환경 준비(방문예약·계정 신청·ID카드 신청·OA기기 신청) 진행 상황을 조회한다. "
         "person_id를 주면 그 인력의 투입별 항목 상태, 없으면 준비가 남은 투입 목록을 돌려준다.",
         _obj({"person_id": {"type": "string", "description": "대상 인력의 id 또는 이름. 비우면 준비가 남은 전체 투입"}}),
         handler=lambda s, a: _checklist_status(s, _pid(s, a["person_id"]) if a.get("person_id") else None)),
    Tool("pending_contract_alerts", "계약 종료 14일 전부터 표시되는 미확인 만료 알림 목록을 조회한다.",
         _obj({}),
         handler=lambda s, a: s.pending_alerts()),
    Tool("query_database", "정해진 조회 도구로 답할 수 없는 조회 질문(조건 검색, 건수 집계, 여러 표를 묶은 조회 등)을 "
         "LlamaIndex Text-to-SQL로 조회한다. question에 조회할 내용을 한국어 문장으로 넣는다. "
         "읽기 전용 SELECT만 실행되며 등록·수정·삭제에는 쓸 수 없다.",
         _obj({"question": STR}, ("question",)),
         handler=lambda s, a: query_database(s.path, s.clock(), a["question"])),
    # --- 등록·수정·삭제 제안: 값만 채우고 저장하지 않는다 ---
    Tool("prepare_checklist_update", "근무 환경 준비 항목의 상태 변경을 준비한다(저장 안 함). "
         "투입이 여러 건이면 project에 과제명을 넣는다. 사용자가 화면에서 확인해야 저장된다.",
         _obj({"person_id": PERSON, "item": _enum(CHECKLIST_ITEMS), "status": _enum(CHECKLIST_STATUSES),
               "reason": STR, "project": STR}), handler=_plan("checklist_update")),
    Tool("prepare_contract_save", "구매계약 신규 등록 또는 수정(기간 변경·연장·계약번호 입력 등)을 준비한다(저장 안 함). "
         "수정이면 contract_id에 계약 id나 계약번호를 넣고, 새 계약이면 mode='신규'로 한다. 바꿀 값만 넣는다.",
         _obj({"person_id": PERSON, "mode": _enum(("신규", "수정")), "contract_id": STR, "company": STR,
               "contract_no": STR, "start_date": DATE, "end_date": DATE, "status": _enum(CONTRACT_STATUSES)}),
         handler=_plan("contract_save")),
    Tool("prepare_contract_delete", "구매계약 삭제를 준비한다(삭제 안 함). 사용자가 화면에서 확인해야 삭제된다.",
         _obj({"person_id": PERSON, "contract_id": STR}), handler=_plan("contract_delete")),
    Tool("prepare_account_id", "발급된 회사 계정 ID 입력을 준비한다(저장 안 함).",
         _obj({"person_id": PERSON, "account_id": STR}), handler=_plan("account_id")),
    Tool("prepare_alert_ack", "계약 종료 알림 확인 처리를 준비한다(저장 안 함). 알림 확인은 계약 갱신 완료가 아니다.",
         _obj({"person_id": PERSON}), handler=_plan("alert_ack")),
    Tool("prepare_assignment_update", "투입 정보(매출귀속 프로젝트·등급·매입단가·시작일·종료일) 변경을 준비한다(저장 안 함). "
         "특정 달만 바꾸면 month(YYYY-MM), 어느 날부터 바꾸면 effective_date를 넣는다. 기간 일부만 바뀌면 코드가 투입을 나눈다. "
         "바꿀 값만 넣는다. 과제를 옮기면 prepare_assignment_transfer, 사람을 바꾸면 prepare_assignment_replace를 쓴다.",
         _obj({"person_id": PERSON, "project": {"type": "string", "description": "투입이 여러 건일 때 대상 과제명"},
               "month": {"type": "string", "description": "그 달만 바꿀 때 YYYY-MM"},
               "effective_date": {"type": "string", "description": "그날부터 바꿀 때 YYYY-MM-DD"},
               "revenue_project": STR, "grade": STR, "purchase_rate": {"type": "string", "description": "원/MM 금액"},
               "start_date": DATE, "end_date": DATE}),
         handler=_plan("assignment_update")),
    Tool("prepare_assignment_transfer", "인력이 기존 과제에서 빠지고 다른 과제로 옮기는 것을 준비한다(저장 안 함). "
         "기존 투입은 이동 전날 종료하고 새 과제 투입을 만든다.",
         _obj({"person_id": PERSON, "to_project": {"type": "string", "description": "옮길 과제명"},
               "start_date": {"type": "string", "description": "새 과제 시작일(이동일) YYYY-MM-DD"},
               "from_project": {"type": "string", "description": "기존 과제명(투입이 여러 건일 때)"},
               "end_date": {"type": "string", "description": "새 과제 종료일. 사용자가 말하지 않으면 비운다"},
               "revenue_project": {"type": "string", "description": "새 과제의 매출귀속 프로젝트. 말하지 않으면 비운다"},
               "purchase_rate": STR}, ("person_id", "to_project", "start_date")),
         handler=_plan("assignment_transfer")),
    Tool("prepare_assignment_replace", "과제의 인력 교체를 준비한다(저장 안 함). 기존 인력 투입은 교체 전날 종료하고 "
         "교체 인력을 같은 과제에 투입하며, 기존 인력 계약 종료일과 교체 인력 계약(준비)도 함께 맞춘다.",
         _obj({"person_id": {"type": "string", "description": "빠지는 기존 인력의 id 또는 이름"},
               "new_person_id": {"type": "string", "description": "새로 들어오는 교체 인력의 id 또는 이름"},
               "start_date": {"type": "string", "description": "교체 인력 투입 시작일 YYYY-MM-DD"},
               "project": {"type": "string", "description": "과제명(기존 인력 투입이 여러 건일 때)"},
               "grade": {"type": "string", "description": "교체 인력 등급(L7·L6·L5 등). 사용자가 말하지 않으면 비운다"},
               "purchase_rate": STR, "revenue_project": STR, "end_date": DATE},
              ("person_id", "new_person_id", "start_date")),
         handler=_plan("assignment_replace")),
    # --- 구현 예정: 규칙 미확정 ---
    Tool("monthly_purchase_needs", "투입 계획과 계약 기간을 비교해 특정 월 구매계약이 필요한 인력을 조회한다. "
         "구매 필요 판정 기준 미확정(TODO).",
         _obj({"year": INT, "month": INT}, ("year", "month")), status=PLANNED),
    Tool("inspection_amount_by_revenue_project", "업체의 월 검수 금액(원)을 매출귀속 프로젝트별로 계산한다. 검수 금액을 물을 때만 쓴다. "
         "인력의 매출귀속 프로젝트가 무엇인지 묻는 질문은 get_assignment_timeline으로 답한다. "
         "배분·반올림·부가세 규칙 미확정(TODO).",
         _obj({"company": STR, "year": INT, "month": INT}, ("company", "year", "month")), status=PLANNED),
]}


# 화면과 Agent 답변에서 쓰는 도구의 한국어 이름
TOOL_LABELS = {
    "find_people": "인력 찾기",
    "get_assignment_timeline": "투입 이력 조회",
    "get_project_members": "과제 인력 조회",
    "list_contracts": "구매계약 조회",
    "contracts_ending": "월·업체별 계약 종료 예정",
    "pending_contract_alerts": "미확인 계약 종료 알림",
    "checklist_status": "근무 환경 준비 현황",
    "monthly_purchase_needs": "월별 구매 필요 인력 (구현 예정)",
    "inspection_amount_by_revenue_project": "매출귀속 프로젝트별 검수 금액 (구현 예정)",
    "query_database": "자유 조회 (Text-to-SQL, 읽기 전용)",
    "prepare_checklist_update": "근무 환경 준비 상태 변경",
    "prepare_contract_save": "구매계약 등록·수정",
    "prepare_contract_delete": "구매계약 삭제",
    "prepare_account_id": "계정 ID 입력",
    "prepare_alert_ack": "계약 종료 알림 확인",
    "prepare_assignment_update": "투입 정보 변경",
    "prepare_assignment_transfer": "다른 과제로 이동",
    "prepare_assignment_replace": "인력 교체",
}


def _pid(service: Service, value: str) -> str:
    """조회 도구에 이름이 들어와도 관리번호로 바꾼다. 없거나 동명이인이면 되물을 오류를 낸다.

    작은 모델이 find_people 없이 이름을 넣으면 빈 결과가 나오고, 모델이 '모두 완료'처럼 지어내 답하던 문제를 막는다.
    """
    try:
        return _person(service, {"person_id": value})["id"]
    except ValidationError:
        given = str(value or "").strip().casefold().replace(" ", "")
        if given and any(given == p.casefold().replace(" ", "") for p in service.project_names()):
            raise ValidationError(f"'{value}'은(는) 사람이 아니라 과제명입니다. 과제 인력은 get_project_members로 조회하세요.")
        raise


# 투입 Type의 뜻. 0.5MM를 '반 달'로 잘못 해석하던 문제(11/03 v1 평가 H01·H02)로 도구 결과에 함께 담는다.
WORK_PATTERN = {
    "Full time": "Full time: 이 기간의 근무일에 전부 근무(MM은 기간 길이로 계산, 0.5MM면 한 달 중 절반 기간만 근무)",
    "Part time": "Part time: 이 기간 내내 다른 업무와 나눠 근무(MM은 기간 전체 투입량, 0.5MM면 한 달 내내 절반씩)",
}
_TITLES = ("차장", "과장", "대리", "부장", "사원", "주임", "책임", "선임", "수석", "님", "씨")


def _monthly_mm(row: dict) -> dict | str:
    """Full time 투입의 월별 MM(계산 근거에 저장된 값, 소수 셋째 자리). '8월 MM' 질문에 합계를 답하던 문제(v1.2 평가 S08·S09)."""
    if row.get("assignment_type") != "Full time":
        return "Part time은 월별로 나누지 않음(기간 전체 MM만 있음)"
    try:
        months = json.loads(row.get("mm_details") or "{}").get("months") or []
        return {m["month"]: format(Decimal(str(m["mm"])).quantize(Decimal("0.001")).normalize(), "f") for m in months}
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return "계산 근거 없음"


def _rate(row: dict) -> str:
    rate = row.get("purchase_rate")
    if rate in (None, ""):
        return "미입력"
    return f"{int(Decimal(str(rate))):,} {row.get('rate_unit') or ''}".strip()


def _work(row: dict) -> dict:
    return dict(row, work_pattern=WORK_PATTERN.get(row.get("assignment_type"), "투입 Type 미입력"),
                monthly_mm=_monthly_mm(row))


def _brief(a: dict) -> dict:
    return {"project": a["project"] or "미입력", "revenue_project": a["revenue_project"] or "미정(미입력)",
            "start_date": a["start_date"], "end_date": a["end_date"], "assignment_type": a["assignment_type"],
            "mm": a["mm"], "monthly_mm": _monthly_mm(a), "grade": a["grade"], "purchase_rate": _rate(a),
            "work_pattern": WORK_PATTERN.get(a["assignment_type"], "투입 Type 미입력")}


def _timeline(service: Service, person_id: str) -> dict:
    t = service.assignment_timeline(person_id)
    return {k: ([_work(r) for r in v] if isinstance(v, list) else v) for k, v in t.items()}


def _find_people(service: Service, name: str, company: str | None = None) -> list[dict]:
    """인력 후보. 1명이면 투입·계약 요약(summary)을 붙인다.

    작은 모델이 인력 찾기 뒤 투입·계약 조회를 부르지 않고 멈추거나 지어내던 문제(11/03 v1 평가 S02·S04·C02·N01·R01·R03)를
    막기 위해, 찾은 결과만으로도 답할 수 있게 한다. 동명이인(2명 이상)이면 요약 없이 목록만 준다(되물어야 하므로).
    """
    value, company = str(name or "").strip(), str(company or "").strip()
    if company and (not value or not service.find_people(value)) and service.find_people(company):
        value, company = company, ""          # 모델이 이름을 업체 칸에 넣은 경우(v1.2 평가 C02·C03)
    if not value:
        raise ValidationError("찾을 인력 이름을 name에 넣으세요.")
    found = service.find_people(value)
    if not found:
        for word in _TITLES:                      # "이협력 차장"처럼 직함이 붙어 오면 떼고 다시 찾는다
            value = value.removesuffix(word).strip()
        found = service.find_people(value) if value else []
    if company and len(found) > 1:                # "감마테크 김협력"처럼 업체로 동명이인을 가린다
        found = [p for p in found if company in (p["company"] or "")] or found
    if len(found) != 1:
        return found
    person = found[0]
    t = service.assignment_timeline(person["id"])
    contracts = [{"contract_no": c["contract_no"] or "미발급", "company": c["company"], "start_date": c["start_date"],
                  "end_date": c["end_date"], "status": c["status"], "state": c["label"],
                  "extension_registered": bool(c["has_successor"])} for c in service.contracts(person["id"])]
    summary = {
        "today": t["today"],
        "current": [_brief(a) for a in t["current"]] or "현재 투입 없음",
        "upcoming": [_brief(a) for a in t["upcoming"]],
        "past": [_brief(a) for a in t["past"][:3]],
        "contracts": contracts or ("구매계약 대상 아님(정 분류)" if person["category"] == "정" else "등록된 구매계약 없음"),
    }
    return [dict(person, summary=summary)]


def _checklist_status(service: Service, person_id: str | None) -> list[dict]:
    if not person_id:
        return [{k: r[k] for k in ("id", "name", "company", "project", "start_date", "done", "total",
                                   "open_labels")} for r in service.checklist_overview()]
    return [{"assignment_id": a["id"], "project": a["project"], "start_date": a["start_date"],
             "end_date": a["end_date"],
             "items": [{k: c[k] for k in ("item", "status", "reason", "drafted_at", "applied_at")}
                       for c in service.checklist(a["id"])]}
            for a in service.assignments(person_id)]


def tool_specs(include_planned: bool = False, names=None) -> list[dict]:
    """LLM에 넘길 도구 목록(이름·설명·입력 스키마). names를 주면 그 도구만(전문 Agent용)."""
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in TOOLS.values()
            if (include_planned or t.status == IMPLEMENTED) and (names is None or t.name in names)]


def _check_args(tool: Tool, args: dict) -> dict:
    if not isinstance(args, dict):
        raise ValidationError("도구 입력은 객체(dict)여야 합니다.")
    schema = tool.parameters
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise ValidationError(f"알 수 없는 입력: {', '.join(sorted(unknown))}")
    missing = [k for k in schema["required"] if args.get(k) in (None, "")]
    if missing:
        raise ValidationError(f"필수 입력 누락: {', '.join(missing)}")
    clean = {}
    for key, value in args.items():
        kind = schema["properties"][key].get("type")
        if kind == "integer":
            if isinstance(value, bool) or not str(value).strip().lstrip("-").isdigit():
                raise ValidationError(f"{key}은(는) 정수여야 합니다.")
            value = int(value)
        elif kind == "string":
            value = str(value).strip()
            allowed = schema["properties"][key].get("enum")
            if allowed and value and value not in allowed:
                raise ValidationError(f"{key}은(는) {', '.join(allowed)} 중 하나여야 합니다.")
        clean[key] = value
    return clean


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def call_tool(service: Service, name: str, args: dict | None = None) -> dict:
    """도구 실행. 예외를 던지지 않고 결과 객체로 성공·실패를 돌려준다(Agent 루프용)."""
    started, t0 = datetime.now(TIMEZONE).isoformat(), time.perf_counter()
    args = args or {}
    try:
        tool = TOOLS.get(name)
        if tool is None:
            raise ValidationError(f"없는 도구입니다: {name}")
        if tool.status != IMPLEMENTED:
            raise ValidationError(f"{name} 도구는 아직 구현 예정입니다.")
        data = _jsonable(tool.handler(service, _check_args(tool, args)))
        result = {"ok": True, "tool": name, "data": data}
    except (ValidationError, ConflictError, LLMError) as exc:
        result = {"ok": False, "tool": name, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - Agent에는 원인만 전달하고 실행 기록에 남긴다
        result = {"ok": False, "tool": name, "error": f"처리 중 오류: {type(exc).__name__}"}
    result["duration_ms"] = round((time.perf_counter() - t0) * 1000)
    service.record_run(f"tool:{name}", (), dict(args), result["ok"],
                    None if not result["ok"] else _summary_of(result["data"]),
                    result.get("error"), t0, started)
    return result


def _summary_of(data: Any) -> str:
    if isinstance(data, list):
        return f"{len(data)}건"
    if isinstance(data, dict):
        return ", ".join(f"{k}:{len(v) if isinstance(v, list) else v}" for k, v in data.items())[:300]
    return str(data)[:300]
