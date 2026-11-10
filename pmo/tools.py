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
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from pmo.agent.actions import _person, build_plan, typo_message
from pmo.agent.guardrails import similar_names
from pmo.agent.llm import LLMError
from pmo.agent.text_to_sql import query_database
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
    # 구현 예정 도구가 실패와 함께 돌려줄 참고 정보(판정은 하지 않음). 결과의 data에 담긴다.
    fallback: Callable[[Service, dict], Any] | None = field(default=None, repr=False)


def _obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


STR = {"type": "string"}
DATE = {"type": "string", "description": "YYYY-MM-DD"}
PERSON = {"type": "string", "description": "대상 인력. find_people 결과의 id(예: DEMO-002) 또는 사용자가 말한 이름. 비우지 않는다."}


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


PROPOSAL_NOTE = ("저장하지 않았습니다. 답변 아래에 '현재 → 변경' 확인 카드를 띄웠습니다. "
                 "사용자가 '확인 후 저장'을 눌러야 반영됩니다.")


REQUEST_NOTE = ("저장하지 않았습니다. 과제 리더 권한이라 바로 바꿀 수 없어, 답변 아래 카드에 '수정 요청 보내기'를 띄웠습니다. "
                "요청을 보내면 PM·PMO가 승인해야 반영됩니다.")


def _plan(action: str):
    """LLM이 넣은 값으로 변경 계획(현재 → 변경)을 만든다. 저장하지 않는다(pmo/agent/actions.py).

    11/08 담당자(수정 요청 승인): 과제 리더도 담당 과제 인력의 변경 계획까지는 만든다. 카드는 '확인 후 저장' 대신
    '수정 요청 보내기'이고, PM·PMO가 수정 요청 화면에서 승인해야 반영된다(이전 v2는 리더에게 카드를 만들지 않음).
    """
    def handler(service: Service, args: dict) -> dict:
        plan = build_plan(service, action, args)
        if service.access is not None and not service.access.can_write:
            from pmo.agent.actions import request_target
            project, person_id = request_target(service, plan, args)
            plan = dict(plan, request={"project": project, "person_id": person_id,
                                       "action": {"action": action, "values": args}})
            return {"plan": plan, "message": REQUEST_NOTE}
        return {"plan": plan, "message": PROPOSAL_NOTE}
    return handler
INT = {"type": "integer"}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("find_people", "이름 일부로 인력 후보를 찾는다. 동명이인이 있으면 여러 명이 나온다. "
         "1명이면 summary에 그 사람의 현재·예정·과거 투입(과제·기간·매출귀속 프로젝트·투입 Type·MM)과 구매계약 요약이 함께 온다. "
         "summary로 답할 수 있으면 바로 답하고, 이후 도구는 결과의 내부 관리번호(id)로 호출한다.",
         _obj({"name": {"type": "string", "description": "사람 이름(예: 이가상). 직함은 빼도 된다"},
               "company": {"type": "string", "description": "질문에 업체명이 따로 있을 때만(예: '가상솔루션 이가상'의 가상솔루션). 이름을 넣지 않는다"}}),
         # name을 스키마 필수로 두지 않는다: 이름을 company에 넣는 실수를 코드에서 바로잡기 위함(빈 이름은 _find_people이 거절)
         handler=lambda s, a: _find_people(s, a.get("name"), a.get("company"))),
    Tool("get_assignment_timeline", "인력의 현재·과거·예정 투입을 조회한다. 투입마다 과제, 매출귀속 프로젝트, 기간, 등급, "
         "투입 Type(Full time·Part time), 투입 MM이 있다. 한 사람의 매출귀속 프로젝트(월별 포함)·등급·MM을 묻는 질문도 이 도구로 답한다.",
         _obj({"person_id": PERSON}, ("person_id",)),
         handler=lambda s, a: _timeline(s, _pid(s, a["person_id"]))),
    Tool("calculate_mm", "기간(시작일~종료일)의 MM을 구매시스템 방식으로 계산한다(시작·종료일 포함, 주말·공휴일 제외, "
         "월별 근무일 비율 합). '11월 1일부터 18일까지 MM', '이가상 10/1~10/10 MM'처럼 날짜 구간의 MM을 물을 때 쓴다. "
         "인력을 말하면 person_id에 넣는다(그 사람 투입과 겹치는 날만 계산), 인력이 없으면 비운다(Full time 기준 MM). "
         "결과의 mm을 그대로 답하고 직접 계산하지 않는다.",
         _obj({"start_date": DATE, "end_date": DATE,
               "person_id": {"type": "string", "description": "인력 id 또는 이름. 말하지 않았으면 비운다"}},
              ("start_date", "end_date")),
         handler=lambda s, a: _calculate_mm(s, **a)),
    Tool("get_project_members", "과제(투입 과제, 예: APC Analysis)의 인력 목록을 조회한다. 사람마다 매출귀속 프로젝트·기간·"
         "투입 Type·MM과, 매출귀속 프로젝트별로 묶은 이름(by_revenue_project)이 있다. 기본은 현재·예정 투입, "
         "과거까지 보려면 include_past=true. 과제명은 사람 이름이 아니니 find_people에 넣지 않는다.",
         _obj({"project": {"type": "string", "description": "과제명(사용자가 말한 그대로)"},
               "include_past": {"type": "boolean"}}, ("project",)),
         handler=lambda s, a: s.project_members(a["project"], include_past=bool(a.get("include_past")))),
    Tool("list_contracts", "인력의 구매계약(계약번호·기간·상태·연장 여부)을 조회한다.",
         _obj({"person_id": PERSON}, ("person_id",)),
         handler=lambda s, a: s.contracts(_pid(s, a["person_id"]))),
    Tool("search_contracts", "조건으로 구매계약과 그 인력을 찾는다: 계약번호(예: 450012201에 묶인 인력), 업체, "
         "계약번호 미발급(without_number=true). 계약번호를 사람 이름 칸에 넣지 않고 이 도구를 쓴다.",
         _obj({"contract_no": STR, "company": STR,
               "without_number": {"type": "boolean", "description": "계약번호가 아직 없는 계약만"}}),
         handler=lambda s, a: _company_is_not_a_person(s, a) or [
             {k: c[k] for k in ("name", "company", "category", "contract_no", "start_date", "end_date", "label")}
             | {"contract_no": c["contract_no"] or "미발급"}
             for c in s.search_contracts(a.get("contract_no"), a.get("company"), None, bool(a.get("without_number")))]),
    Tool("search_people", "조건으로 인력을 찾는다: 업체, 분류(정·협력·컨설팅), 직무, 계정 ID 미발급(without_account=true), "
         "현재·예정 투입의 매출귀속 프로젝트 미정(without_revenue_project=true). 이름을 모를 때 쓴다. 이름이 있으면 find_people.",
         _obj({"company": STR, "category": _enum(("정", "협력", "컨설팅")), "job_role": STR,
               "without_account": {"type": "boolean", "description": "계정 ID가 아직 없는 사람만"},
               "without_revenue_project": {"type": "boolean",
                                           "description": "현재·예정 투입 중 매출귀속 프로젝트가 정해지지 않은 사람만"}}),
         handler=lambda s, a: _company_is_not_a_person(s, a) or [
             {k: p[k] for k in ("name", "company", "category", "job_role")} | {"account_id": p["account_id"] or "미발급"}
             | ({"projects_without_revenue_project": p["projects_without_revenue_project"]}
                if p.get("projects_without_revenue_project") else {})
             for p in s.search_people(a.get("company"), a.get("category"), a.get("job_role"),
                                      bool(a.get("without_account")), bool(a.get("without_revenue_project")))]),
    Tool("contracts_ending", "특정 연·월(선택: 업체)에 계약이 종료되는 인력 목록을 조회한다.",
         _obj({"year": INT, "month": INT, "company": STR}, ("year", "month")),
         handler=lambda s, a: s.contracts_ending(a["year"], a["month"], a.get("company"))),
    Tool("pending_contract_alerts", "계약 종료 14일 전부터 표시되는 미확인 만료 알림 목록을 조회한다. "
         "'연장이 필요한 인력'을 물으면 needs_extension=true(종료 14일 전~당일이면서 연장 계약 미등록만). "
         "결과의 extension_registered가 true면 이미 연장 계약이 등록된 사람이다.",
         _obj({"needs_extension": {"type": "boolean",
                                   "description": "연장이 필요한 계약만(종료 14일 전~당일, 연장 계약 미등록). 만료 지난 계약은 빠진다"}}),
         handler=lambda s, a: [dict(r, extension_registered=bool(r.get("has_successor"))) for r in s.pending_alerts()
                               if not a.get("needs_extension") or r.get("ending_soon")]),
    Tool("query_database", "정해진 조회 도구로 답할 수 없는 조회 질문(조건 검색, 건수 집계, 여러 표를 묶은 조회 등)을 "
         "LlamaIndex Text-to-SQL로 조회한다. question에 조회할 내용을 한국어 문장으로 넣는다. "
         "읽기 전용 SELECT만 실행되며 등록·수정·삭제에는 쓸 수 없다.",
         _obj({"question": STR}, ("question",)),
         handler=lambda s, a: _query_database(s, a["question"])),
    # --- 등록·수정·삭제 제안: 값만 채우고 저장하지 않는다 ---
    Tool("prepare_contract_save", "구매계약 신규 등록 또는 수정(기간 변경·연장·계약번호 입력 등)을 준비한다(저장 안 함). "
         "수정이면 contract_id에 계약 id나 계약번호를 넣고, 새 계약이면 mode='신규'로 한다. 바꿀 값만 넣는다.",
         _obj({"person_id": PERSON, "mode": _enum(("신규", "수정")), "contract_id": STR, "company": STR,
               "contract_no": STR, "start_date": DATE, "end_date": DATE}),
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
         "구매 필요 판정 기준 미확정(TODO). 실패와 함께 그 달 계약 종료 예정 인력을 참고 정보로 돌려준다.",
         _obj({"year": INT, "month": INT}, ("year", "month")), status=PLANNED,
         fallback=lambda s, a: _purchase_reference(s, **a)),
    Tool("inspection_amount_by_revenue_project", "업체·계약번호의 검수 금액(원)을 매출귀속 프로젝트별로 계산한다(화면 '검수하기'와 같은 계산). "
         "검수 금액·계약 총액을 물을 때만 쓴다. 인력의 매출귀속 프로젝트가 무엇인지 묻는 질문은 get_assignment_timeline으로 답한다. "
         "계약번호를 모르면 비운다(업체의 계약이 여러 건이면 결과가 되물을 내용을 알려 준다). "
         "월을 말하지 않으면 비운다(이번 달). 금액은 결과의 문장을 그대로 옮기고 직접 계산하지 않는다.",
         _obj({"company": {"type": "string", "description": "업체명"},
               "contract_no": {"type": "string", "description": "계약번호(예: 450012201). 모르면 비운다"},
               "year": INT, "month": {"type": "integer", "description": "검수 월(1~12). 말하지 않으면 비운다"}},
              ("company",)),
         handler=lambda s, a: _inspection(s, **a)),
]}


# 화면과 Agent 답변에서 쓰는 도구의 한국어 이름
TOOL_LABELS = {
    "find_people": "인력 찾기",
    "get_assignment_timeline": "투입 이력 조회",
    "calculate_mm": "기간 MM 계산",
    "get_project_members": "과제 인력 조회",
    "search_contracts": "조건으로 계약 찾기",
    "search_people": "조건으로 인력 찾기",
    "list_contracts": "구매계약 조회",
    "contracts_ending": "월·업체별 계약 종료 예정",
    "pending_contract_alerts": "미확인 계약 종료 알림",
    "monthly_purchase_needs": "월별 구매 필요 인력 (구현 예정)",
    "inspection_amount_by_revenue_project": "매출귀속 프로젝트별 검수 금액",
    "query_database": "자유 조회 (Text-to-SQL, 읽기 전용)",
    "prepare_contract_save": "구매계약 등록·수정",
    "prepare_contract_delete": "구매계약 삭제",
    "prepare_account_id": "계정 ID 입력",
    "prepare_alert_ack": "계약 종료 알림 확인",
    "prepare_assignment_update": "투입 정보 변경",
    "prepare_assignment_transfer": "다른 과제로 이동",
    "prepare_assignment_replace": "인력 교체",
}


# 계약번호: 예전 형식(C-2026-202)과 구매시스템 형식(4500 + 숫자 5개, 예: 450012201)
_CONTRACT_NO = re.compile(r"^(?:[A-Za-z]{1,3}-?\d{2,4}-?\d{1,4}|\d{6,12})$")


def _company_is_not_a_person(service: Service, args: dict):
    """조건 조회의 업체 칸에 사람 이름을 넣으면 그 사람의 인력 찾기 결과를 대신 돌려준다.

    v3 평가 V01: '이협력' 업체로 검색. 처음엔 오류로 알렸더니 모델이 다시 부르지 않고 '인력 찾기를 호출하세요'를 사용자에게 전함.
    """
    company = str(args.get("company") or "").strip()
    if company and not any(company in (p["company"] or "") for p in service.people()) and service.find_people(company):
        return {"note": f"'{company}'은(는) 업체가 아니라 인력 이름이라 인력 찾기 결과를 돌려줍니다.",
                "people": _find_people(service, company)}
    return None


def _not_a_name(value) -> str | None:
    """사람 이름 자리에 계약번호·조건 문장이 들어왔으면 맞는 도구를 알려 준다(v2 평가 C09·C10)."""
    text = str(value or "").strip()
    if _CONTRACT_NO.match(text.replace(" ", "")):
        return f"'{text}'은(는) 사람이 아니라 계약번호입니다. 계약번호로 인력을 찾으려면 search_contracts(contract_no)를 쓰세요."
    if len(text) > 8 or " " in text:
        return (f"'{text}'은(는) 사람 이름이 아닙니다. 조건으로 찾으려면 계약은 search_contracts"
                "(계약번호·업체·상태·계약번호 미발급), 인력은 search_people(업체·분류·직무·계정 ID 미발급)을 쓰세요.")
    return None


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
        hint = _not_a_name(value)
        if hint:
            raise ValidationError(hint)
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


RATE_HIDDEN = "권한 없음(단가는 PM·PMO만 조회)"


def _rate(row: dict) -> str:
    if row.get("rate_hidden"):
        return RATE_HIDDEN
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


def _calculate_mm(service: Service, start_date: str, end_date: str, person_id: str = "") -> dict:
    """기간 MM 계산 도구(11/08 담당자: '11월 1일부터 18일까지 MM'에 '미입력'이라 답하던 문제).

    인력이 없으면 Full time 기준 기간 MM(홈 'MM 계산기'와 같음). 인력이 있으면 그 사람 투입과 겹치는 날로 투입마다 계산한다.
    Full time은 겹치는 기간으로 다시 계산하고, Part time은 기간 전체 MM만 있어 조회 기간이 투입 전체를 덮을 때만 그 값을 쓴다.
    """
    from pmo.inspection import _overlap
    from pmo.mm import calculate_full_time
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValidationError("날짜는 YYYY-MM-DD 형식으로 넣으세요.") from exc
    if end < start:
        raise ValidationError("종료일이 시작일보다 빠릅니다.")
    period = f"{start}~{end}"
    if not person_id:
        r = calculate_full_time(start, end)
        return {"period": period, "basis": "Full time(그 기간 내내 투입) 기준", "mm": r["mm"],
                "months": [{"month": m["month"], "assigned_days": m["assigned_days"],
                            "working_days": m["working_days"]} for m in r["months"]],
                "rule": "시작·종료일 포함, 주말·공휴일 제외, 월별 (투입 근무일 ÷ 그 달 근무일) 합, 합계 소수 셋째 자리 반올림"}
    pid = _pid(service, person_id)
    person = next((p for p in service.people() if p["id"] == pid), {})
    rows, total, skipped = [], Decimal(0), []
    for a in service.assignments(pid):
        window = _overlap((a["start_date"], a["end_date"]), (start.isoformat(), end.isoformat()))
        if window is None:
            continue
        row = {"project": a["project"], "revenue_project": a.get("revenue_project") or "미정",
               "assignment_type": a.get("assignment_type") or "미입력", "period": f"{window[0]}~{window[1]}"}
        if a.get("assignment_type") == "Full time":
            row["mm"] = calculate_full_time(*window)["mm"]
        elif a.get("assignment_type") == "Part time" and window == (date.fromisoformat(a["start_date"]),
                                                                    date.fromisoformat(a["end_date"])) \
                and a.get("mm") not in (None, ""):
            row["mm"] = str(a["mm"])
        else:
            row["mm"] = "계산 불가"
            row["note"] = ("Part time은 투입 기간 전체 MM만 있어 일부 기간으로 나눌 수 없음"
                           if a.get("assignment_type") == "Part time" else "투입 Type 미입력")
            skipped.append(row["project"])
        if row["mm"] != "계산 불가":
            total += Decimal(row["mm"])
        rows.append(row)
    if not rows:
        return {"name": person.get("name"), "period": period, "mm": "0",
                "note": f"{period} 기간에 이 인력의 투입이 없습니다."}
    for row in rows:
        if row["assignment_type"] == "Part time":
            row["work_pattern"] = "Part time: 투입 기간 내내 다른 업무와 병행(예: 0.5MM = 기간 내내 절반)"
        elif row["assignment_type"] == "Full time":
            row["work_pattern"] = "Full time: 그 기간에만 전일 근무"
    # 작은 모델이 과제별 값을 빼고 합계만 답하던 문제(v3 평가 S08) → 답에 그대로 옮길 문장
    parts = ", ".join(f"{r['project']} {r['mm']}MM" for r in rows)
    summary = (f"{person.get('name')} {period}: {parts}" + (f" / 합계 {format(total, 'f')}MM" if len(rows) > 1 else ""))
    return {"name": person.get("name"), "period": period, "summary": summary, "assignments": rows,
            "mm": format(total, "f") + (" (계산 불가 건 제외)" if skipped else ""),
            "rule": "투입과 겹치는 날만 투입마다 구매시스템 방식으로 계산해 더함(Full time). 계산 불가는 0으로 보지 않고 합계에서 뺌"}


def _timeline(service: Service, person_id: str) -> dict:
    t = service.assignment_timeline(person_id)
    result = {k: ([_work(r) for r in v] if isinstance(v, list) else v) for k, v in t.items()}
    shared = _months_with_several(t["current"] + t["upcoming"] + t["past"])
    if shared:
        result["months_with_several_assignments"] = shared      # v3 평가 S08: 투입 이력 조회 후에도 달별 합계를 보게
    return result


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
    if not found:
        hint = _not_a_name(value)
        if hint:
            raise ValidationError(hint)
        if similar_names(service, value):          # v3: 오타면 비슷한 이름을 되묻게 한다(빈 결과로 '없다'고 끝내지 않음)
            raise ValidationError(typo_message(service, value))
    if len(found) != 1:
        return found
    person = found[0]
    t = service.assignment_timeline(person["id"])
    contracts = [{"contract_no": c["contract_no"] or "미발급", "company": c["company"], "start_date": c["start_date"],
                  "end_date": c["end_date"], "state": c["label"],
                  "extension_registered": bool(c["has_successor"])} for c in service.contracts(person["id"])]
    summary = {
        "today": t["today"],
        "current": [_brief(a) for a in t["current"]] or "현재 투입 없음",
        "upcoming": [_brief(a) for a in t["upcoming"]],
        "past": [_brief(a) for a in t["past"][:3]],
        "contracts": contracts or ("구매계약 대상 아님(정 분류)" if person["category"] == "정" else "등록된 구매계약 없음"),
    }
    shared = _months_with_several(t["current"] + t["upcoming"] + t["past"])
    if shared:
        summary["months_with_several_assignments"] = shared
    return [dict(person, summary=summary)]


def _months_with_several(rows: list[dict]) -> dict:
    """한 달에 Full time 투입이 2건 이상이면 과제별 MM과 합계(v3 평가 S08: 8월 0.25+0.75 중 하나만 답함)."""
    months: dict[str, list[tuple[str, Decimal]]] = {}
    for a in rows:
        values = _monthly_mm(a)
        if isinstance(values, dict):
            for month, mm in values.items():
                months.setdefault(month, []).append((a["project"] or "과제 미입력", Decimal(mm)))
    return {m: " + ".join(f"{p} {format(v.normalize(), 'f')}" for p, v in parts)
               + f" = 합계 {format(sum(v for _, v in parts).normalize(), 'f')}MM"
            for m, parts in sorted(months.items()) if len(parts) > 1}




def _won(amount) -> str:
    return "계산 불가" if amount is None else f"{amount:,}원"


def _fold_no(text: str) -> str:
    return str(text or "").casefold().replace(" ", "")


def _inspection(service: Service, company: str, contract_no: str = "", year: int | None = None,
                month: int | None = None) -> dict:
    """검수 Agent 도구(11/07 담당자: 검수 Agent가 실제로 답하게). 화면 '검수하기'와 같은 Service.inspection을 부른다.

    업체 → 계약번호(모르면 업체의 계약이 1건일 때 그 계약, 여러 건이면 되물을 문장) → 조회 월(기본 이번 달).
    금액은 작은 모델이 다시 계산하지 않도록 원 단위 문장으로 돌려준다. 과제 리더는 Service가 막는다(단가 권한).
    """
    from pmo.inspection import RULE
    options = service.inspection_contracts()
    if contract_no:
        found = [c for c in options if _fold_no(c["contract_no"]) == _fold_no(contract_no)]
        if len(found) > 1 and company:
            found = [c for c in found if _fold_no(company) in _fold_no(c["company"])] or found
        if not found:
            raise ValidationError(f"계약번호 {contract_no} 계약이 없습니다. 계약번호가 발급된 계약만 검수 금액을 계산할 수 있습니다.")
    else:
        found = [c for c in options if _fold_no(company) and _fold_no(company) in _fold_no(c["company"])]
        if not found:
            numbered = [c for c in options if _fold_no(c["contract_no"]) == _fold_no(company)]
            if numbered:                                  # 업체 칸에 계약번호를 넣은 경우
                found = numbered
            else:
                raise ValidationError(f"'{company}' 업체의 계약번호가 발급된 계약이 없습니다. 업체명을 확인해 주세요.")
        if len(found) > 1:
            listed = ", ".join(f"{c['contract_no']}({c['start_date']}~{c['end_date']}, {c['people']}명)" for c in found)
            raise ValidationError(f"{found[0]['company']}의 계약이 {len(found)}건 있습니다: {listed}. "
                                  "어느 계약번호의 검수 금액을 볼까요?")
    target = found[0]
    today = service.clock()
    year = year or today.year
    month = month or today.month
    if not 1 <= month <= 12:
        raise ValidationError("월은 1~12로 입력하세요.")
    ym = f"{year:04d}-{month:02d}"
    r = service.inspection(target["company"], target["contract_no"], ym, ym)
    contract_by = {p["revenue_project"]: p for p in r["contract"]["projects"]}
    selected = r["selected"]
    projects = [{"revenue_project": p["revenue_project"], "people": p["people"], "mm": p["mm"] or "계산 불가",
                 "amount": _won(p["amount"]),
                 "contract_amount": _won(contract_by.get(p["revenue_project"], {}).get("amount"))}
                for p in selected["projects"]]
    for name, p in contract_by.items():                   # 조회 월에 투입이 없는 프로젝트도 계약 총액에는 들어간다
        if all(x["revenue_project"] != name for x in projects):
            projects.append({"revenue_project": name, "people": p["people"], "mm": "조회 월 투입 없음",
                             "amount": "해당 없음(조회 월 투입 없음)", "contract_amount": _won(p["amount"])})
    lines = [{"name": l["name"], "revenue_project": l["revenue_project"], "period": f"{l['start']}~{l['end']}",
              "mm": l["mm"] or "계산 불가", "amount": _won(l["amount"]), **({"note": l["note"]} if l["note"] else {})}
             for l in selected["lines"]]
    incomplete = [f"{l['name']}: {l['note']}" for l in selected["lines"] if l["amount"] is None]
    if not lines:                                         # 계산 실패가 아니라 조회 월에 검수할 투입이 없음
        none = f"해당 없음({ym}은 계약 기간 {r['contract_period'][0]}~{r['contract_period'][1]} 밖이거나 투입이 없음)"
        total_text, mm_text = none, none
    else:
        total_text, mm_text = _won(selected["total"]), selected["mm"] or "계산 불가"
    return {"company": r["company"], "contract_no": r["contract_no"],
            "contract_period": f"{r['contract_period'][0]}~{r['contract_period'][1]}",
            "inspection_period": f"{r['period'][0]}~{r['period'][1]}", "people": r["people"],
            "inspection_total": total_text, "inspection_mm": mm_text,
            "contract_total": _won(r["contract"]["total"]),
            "projects": sorted(projects, key=lambda p: p["revenue_project"]), "lines": lines,
            "not_calculated": incomplete,
            "not_calculated_note": ("계산 불가 건은 합계에서 뺐습니다(0원으로 보지 않음)." if incomplete else ""),
            "rule": RULE + ". 기본안 규칙이며 구매시스템 계약금액과 대조 전입니다.",
            "inspection_total_won": selected["total"], "contract_total_won": r["contract"]["total"]}


def _query_database(service: Service, question: str) -> dict:
    """v2 권한: 과제 리더는 볼 수 있는 행만 담은 임시 DB에서 Text-to-SQL을 실행한다."""
    with service.read_snapshot() as path:
        return query_database(path, service.clock(), question)


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


PURCHASE_NOTE = ("월별 구매 필요 인력 판정 기준(투입 계획과 계약 기간 비교)이 아직 정해지지 않아 구매 필요 여부는 판정하지 않습니다. "
                 "참고로 그 달에 계약이 끝나는 인력을 붙였습니다. 답변에서 기준 미확정임을 먼저 밝히고, 이 목록을 "
                 "'구매 필요 인력'이 아니라 '계약 종료 예정 인력(참고)'으로 보여 주세요.")


def _purchase_reference(service: Service, year: int, month: int) -> dict:
    """구매 필요 판정 대신 보여 줄 참고 정보: 그 달 계약 종료 예정 인력(11/09 담당자). 구매 필요 여부는 판정하지 않는다."""
    rows = service.contracts_ending(year, month)
    return {"month": f"{int(year)}-{int(month):02d}", "basis": "계약 종료 예정(참고, 구매 필요 판정 아님)",
            "ending_contracts": [{"name": r.get("name"), "company": r.get("company"), "contract_no": r.get("contract_no"),
                                  "end_date": r.get("end_date"), "extension_registered": r.get("has_successor")}
                                 for r in rows]}


def call_tool(service: Service, name: str, args: dict | None = None) -> dict:
    """도구 실행. 예외를 던지지 않고 결과 객체로 성공·실패를 돌려준다(Agent 루프용)."""
    started, t0 = datetime.now(TIMEZONE).isoformat(), time.perf_counter()
    args = args or {}
    try:
        tool = TOOLS.get(name)
        if tool is None:
            raise ValidationError(f"없는 도구입니다: {name}")
        if tool.status != IMPLEMENTED:
            if tool.fallback is not None:
                # 판정은 구현 예정 그대로(실패), 참고 정보만 함께 준다
                reference = _jsonable(tool.fallback(service, _check_args(tool, args)))
                result = {"ok": False, "tool": name, "error": f"{name} 도구는 아직 구현 예정입니다. {PURCHASE_NOTE}",
                          "data": reference}
                result["duration_ms"] = round((time.perf_counter() - t0) * 1000)
                service.record_run(f"tool:{name}", (), dict(args), False, _log_result(reference), result["error"],
                                   t0, started)
                return result
            raise ValidationError(f"{name} 도구는 아직 구현 예정입니다.")
        data = _jsonable(tool.handler(service, _check_args(tool, args)))
        result = {"ok": True, "tool": name, "data": data}
    except (ValidationError, ConflictError, LLMError) as exc:
        result = {"ok": False, "tool": name, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - Agent에는 원인만 전달하고 실행 기록에 남긴다
        result = {"ok": False, "tool": name, "error": f"처리 중 오류: {type(exc).__name__}"}
    result["duration_ms"] = round((time.perf_counter() - t0) * 1000)
    service.record_run(f"tool:{name}", (), dict(args), result["ok"],
                    None if not result["ok"] else _log_result(result["data"]),
                    result.get("error"), t0, started)
    return result


LOG_LIMIT = 20000


def _log_result(data: Any) -> str:
    """실행 기록(run_log)에 남길 도구 결과 전체(JSON). 11/08 담당자: 화면에는 JSON을 보이지 않고 기록에만 남긴다.

    실행 기록은 PM·PMO만 보고 Text-to-SQL에서도 제외된다(SAFE_TABLES). 너무 길면 앞부분만 남긴다.
    """
    text = json.dumps(data, ensure_ascii=False, default=str)
    return text if len(text) <= LOG_LIMIT else text[:LOG_LIMIT] + f" …(총 {len(text):,}자, 앞 {LOG_LIMIT:,}자만 기록)"


def _summary_of(data: Any) -> str:
    if isinstance(data, list):
        return f"{len(data)}건"
    if isinstance(data, dict):
        return ", ".join(f"{k}:{len(v) if isinstance(v, list) else v}" for k, v in data.items())[:300]
    return str(data)[:300]
