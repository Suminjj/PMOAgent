"""향후 Agent가 호출할 도구 계층.

- 도구는 화면과 같은 업무 함수(Service)를 호출한다. 계산·저장 로직을 여기 새로 만들지 않는다.
- 입력은 스키마로 검증하고, 결과는 항상 {"ok", "tool", "data" | "error", "duration_ms"} 형식이다.
- 현재는 조회 도구만 구현했다. 등록·변경 도구는 '미리보기 → 사용자 확인 → 저장' 흐름이 필요하므로
  구현 예정으로 두고 Agent가 직접 저장하지 못하게 한다.
- LLM 제공자·모델·프롬프트와 무관하다. 어떤 LLM이든 `tool_specs()`로 도구 목록을 받고
  `call_tool()`로 실행하면 된다.
"""
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from pmo.config import TIMEZONE
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
    # --- 구현 예정: 규칙 미확정 또는 저장 확인 흐름 필요 ---
    Tool("monthly_purchase_needs", "투입 계획과 계약 기간을 비교해 특정 월 구매계약이 필요한 인력을 조회한다. "
         "구매 필요 판정 기준 미확정(TODO).",
         _obj({"year": INT, "month": INT}, ("year", "month")), status=PLANNED),
    Tool("inspection_amount_by_revenue_project", "업체의 월 검수 금액을 매출귀속 프로젝트별로 계산한다. "
         "배분·반올림·부가세 규칙 미확정(TODO).",
         _obj({"company": STR, "year": INT, "month": INT}, ("company", "year", "month")), status=PLANNED),
    Tool("prepare_contract_change", "계약 등록·변경 미리보기를 만든다. 저장은 사용자가 화면에서 확인해야 한다.",
         _obj({"person_id": STR, "changes": {"type": "object"}}, ("person_id", "changes")), status=PLANNED),
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
    except (ValidationError, ConflictError) as exc:
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
