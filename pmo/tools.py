"""향후 Agent가 호출할 도구 계층.

- 도구는 화면과 같은 업무 함수(Service)를 호출한다. 계산·저장 로직을 여기 새로 만들지 않는다.
- 입력은 스키마로 검증하고, 결과는 항상 {"ok", "tool", "data" | "error", "duration_ms"} 형식이다.
- 조회: 정해진 조회 도구 + 그 밖의 조회는 query_database(LlamaIndex Text-to-SQL, SELECT만·읽기 전용).
- 등록·수정·삭제: prepare_* 도구는 LLM이 알아낸 값을 '제안'으로 돌려줄 뿐 저장하지 않는다.
  화면이 그 값으로 양식을 채우고, 사용자가 값을 고르고 확인해야 정해진 업무 함수가 저장한다.
- LLM 제공자·모델·프롬프트와 무관하다. 어떤 LLM이든 `tool_specs()`로 도구 목록을 받고
  `call_tool()`로 실행하면 된다.
"""
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

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


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


PROPOSAL_NOTE = ("저장하지 않았습니다. 화면 오른쪽 양식에 값을 채워 두었으니, "
                 "사용자가 값을 확인·선택하고 '확인 후 저장'을 눌러야 반영됩니다.")


def _proposal(action: str):
    def handler(service: Service, args: dict) -> dict:
        values = {k: v for k, v in args.items() if v not in (None, "")}
        return {"proposal": {"action": action, "values": values}, "message": PROPOSAL_NOTE}
    return handler
INT = {"type": "integer"}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("find_people", "이름 일부로 인력 후보를 찾는다. 동명이인이 있으면 여러 명이 나온다. "
         "이후 도구는 반드시 결과의 내부 관리번호(id)로 호출한다.",
         _obj({"name": STR}, ("name",)),
         handler=lambda s, a: s.find_people(a["name"])),
    Tool("get_assignment_timeline", "인력의 현재 투입·과거 투입·예정 투입 과제와 기간을 조회한다.",
         _obj({"person_id": STR}, ("person_id",)),
         handler=lambda s, a: s.assignment_timeline(a["person_id"])),
    Tool("list_contracts", "인력의 구매계약(계약번호·기간·상태·연장 여부)을 조회한다.",
         _obj({"person_id": STR}, ("person_id",)),
         handler=lambda s, a: s.contracts(a["person_id"])),
    Tool("contracts_ending", "특정 연·월(선택: 업체)에 계약이 종료되는 인력 목록을 조회한다.",
         _obj({"year": INT, "month": INT, "company": STR}, ("year", "month")),
         handler=lambda s, a: s.contracts_ending(a["year"], a["month"], a.get("company"))),
    Tool("checklist_status", "근무 환경 준비(좌석 배정·ID카드 신청·OA기기 신청·방문예약) 진행 상황을 조회한다. "
         "person_id를 주면 그 인력의 투입별 항목 상태, 없으면 준비가 남은 투입 목록을 돌려준다.",
         _obj({"person_id": STR}),
         handler=lambda s, a: _checklist_status(s, a.get("person_id"))),
    Tool("pending_contract_alerts", "계약 종료 14일 전부터 표시되는 미확인 만료 알림 목록을 조회한다.",
         _obj({}),
         handler=lambda s, a: s.pending_alerts()),
    Tool("query_database", "정해진 조회 도구로 답할 수 없는 조회 질문(조건 검색, 건수 집계, 여러 표를 묶은 조회 등)을 "
         "LlamaIndex Text-to-SQL로 조회한다. question에 조회할 내용을 한국어 문장으로 넣는다. "
         "읽기 전용 SELECT만 실행되며 등록·수정·삭제에는 쓸 수 없다.",
         _obj({"question": STR}, ("question",)),
         handler=lambda s, a: query_database(s.path, s.clock(), a["question"])),
    # --- 등록·수정·삭제 제안: 값만 채우고 저장하지 않는다 ---
    Tool("prepare_checklist_update", "근무 환경 준비 항목의 상태 변경을 제안한다(저장 안 함). "
         "알아낸 값만 넣고 모르는 값은 비운다. 사용자가 화면에서 확인한 뒤 저장한다.",
         _obj({"person_id": STR, "item": _enum(CHECKLIST_ITEMS), "status": _enum(CHECKLIST_STATUSES),
               "reason": STR}), handler=_proposal("checklist_update")),
    Tool("prepare_contract_save", "구매계약 신규 등록 또는 수정(기간 변경·연장·계약번호 입력 등)을 제안한다(저장 안 함). "
         "수정이면 list_contracts로 찾은 contract_id를 넣는다.",
         _obj({"person_id": STR, "contract_id": STR, "company": STR, "contract_no": STR,
               "start_date": DATE, "end_date": DATE, "status": _enum(CONTRACT_STATUSES)}),
         handler=_proposal("contract_save")),
    Tool("prepare_contract_delete", "구매계약 삭제를 제안한다(삭제 안 함). 사용자가 화면에서 대상을 확인해야 삭제된다.",
         _obj({"person_id": STR, "contract_id": STR}), handler=_proposal("contract_delete")),
    Tool("prepare_account_id", "발급된 회사 계정 ID를 인력 정보에 입력하도록 제안한다(저장 안 함).",
         _obj({"person_id": STR, "account_id": STR}), handler=_proposal("account_id")),
    Tool("prepare_alert_ack", "계약 종료 알림 확인 처리를 제안한다(저장 안 함). 알림 확인은 계약 갱신 완료가 아니다.",
         _obj({"person_id": STR}), handler=_proposal("alert_ack")),
    # --- 구현 예정: 규칙 미확정 ---
    Tool("monthly_purchase_needs", "투입 계획과 계약 기간을 비교해 특정 월 구매계약이 필요한 인력을 조회한다. "
         "구매 필요 판정 기준 미확정(TODO).",
         _obj({"year": INT, "month": INT}, ("year", "month")), status=PLANNED),
    Tool("inspection_amount_by_revenue_project", "업체의 월 검수 금액을 매출귀속 프로젝트별로 계산한다. "
         "배분·반올림·부가세 규칙 미확정(TODO).",
         _obj({"company": STR, "year": INT, "month": INT}, ("company", "year", "month")), status=PLANNED),
]}


def _checklist_status(service: Service, person_id: str | None) -> list[dict]:
    if not person_id:
        return [{k: r[k] for k in ("id", "name", "company", "project", "start_date", "done", "total",
                                   "open_items")} for r in service.checklist_overview()]
    return [{"assignment_id": a["id"], "project": a["project"], "start_date": a["start_date"],
             "end_date": a["end_date"],
             "items": [{k: c[k] for k in ("item", "status", "reason", "drafted_at", "applied_at")}
                       for c in service.checklist(a["id"])]}
            for a in service.assignments(person_id)]


def tool_specs(include_planned: bool = False) -> list[dict]:
    """LLM에 넘길 도구 목록(이름·설명·입력 스키마)."""
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in TOOLS.values() if include_planned or t.status == IMPLEMENTED]


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
