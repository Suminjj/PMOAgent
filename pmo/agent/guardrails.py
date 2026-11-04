"""v3 Agent 가드레일: 허용된 범위에서만 답한다.

프롬프트에만 맡기지 않고 코드로 막는다(작은 로컬 모델은 지시를 늘 따르지는 않음, v2 평가 11/04).

- 사전 검사(precheck): LLM을 부르기 전에 정해진 방식으로 답한다.
  지시 무시 시도 · 관리하지 않는 정보(연봉·연락처 등) · 업무 범위 밖 · 다른 카테고리 업무 · 검수 금액(도구 연결 전)
- 도구 입력 정리: 자리표시 값('미입력' 등) 제거, 사용자가 말하지 않은 인력·날짜 차단, '12월' 같은 달 보완
- 답변 검사: 도구 결과·사용자 문장에 없는 인력 이름이 답변에 있으면 답변 보류, 카테고리 안내 바로잡기

모든 판단은 로그인 사용자 권한으로 거른 service(people() 등) 위에서 한다. 과제 리더에게 다른 과제 인력 이름을 제안하지 않는다.
"""
import difflib
import json
import re
from datetime import date

# --- 업무 카테고리 판정(키워드 점수) ------------------------------------------------------------
# 질문이 어느 전문 Agent의 업무인지 정한다. 점수가 가장 높은 카테고리 하나(동점이면 판정 안 함).
OWNER_KEYWORDS: dict[str, dict[str, int]] = {
    "staff": {"투입": 1, "과제": 1, "매출귀속": 2, "귀속": 1, "등급": 1, "mm": 1, "계정 id": 2, "계정id": 2,
              "계정 아이디": 2, "교체": 1, "옮겨": 1, "이동": 1, "직무": 1, "단가": 1},
    "environment": {"근무 환경": 2, "방문예약": 2, "방문 예약": 2, "id카드": 2, "id 카드": 2, "oa": 1, "노트북": 1,
                    "모니터": 1, "계정 신청": 2, "준비": 1},
    "contract": {"계약": 1, "알림": 1, "만료": 1, "연장": 1, "종료": 1},
    "inspection": {"검수": 3, "총액": 2, "금액": 2},
}
# 카테고리는 정할 수 없지만 업무 질문인 말(인력 목록·변경 요청 등)
BUSINESS_WORDS = ("인력", "사람", "명단", "목록", "리스트", "몇 명", "업체", "협력", "컨설팅", "정직원", "등록", "삭제",
                  "수정", "변경", "바꿔", "기간", "언제", "현황", "누구")


def scores(question: str) -> dict[str, int]:
    q = question.casefold()
    return {key: sum(w for kw, w in words.items() if kw in q) for key, words in OWNER_KEYWORDS.items()}


def owner_of(question: str) -> str | None:
    s = scores(question)
    best = max(s.values())
    top = [k for k, v in s.items() if v == best]
    return top[0] if best > 0 and len(top) == 1 else None


# --- 사전 검사 ----------------------------------------------------------------------------------
INJECTION = re.compile(r"(지시|규칙|프롬프트|명령|설정)\S{0,3}\s*(은|는|을|를)?\s*(다\s*)?(무시|잊어|따르지)"
                       r"|시스템\s*프롬프트|system\s*prompt|ignore\s+(all|previous|the)", re.I)
UNSUPPORTED = re.compile(r"연봉|급여|월급|보너스|성과급|연락처|전화번호|휴대폰|핸드폰|주소|주민등록|생년월일|인사\s*평가|평가\s*결과")

ANSWERS = {
    "scope": "'{project}' 과제는 조회할 수 있는 과제가 아닙니다. 담당 과제({mine})만 조회할 수 있습니다.",
    "redirect_failed": "이 요청은 {here} 업무인데 처리하지 못했습니다. 인력 이름과 기간 등을 넣어 다시 요청해 주세요.",
    "injection": "이전 지시를 무시하거나 바꾸라는 요청은 따를 수 없습니다. 인력·투입·구매계약·근무 환경 준비 업무를 요청해 주세요.",
    "unsupported": "이 서비스는 인력의 {what} 정보를 관리하지 않아 답할 수 없습니다. "
                   "투입 과제·기간, 구매계약, 근무 환경 준비 정보만 조회할 수 있습니다.",
    "out_of_scope": "이 요청은 도와드릴 수 없습니다. 인력·투입, 구매계약, 근무 환경 준비, 검수 업무만 도와드립니다. "
                    "예: '김정직 지금 무슨 과제에 투입되어 있어?'",
    "redirect": "이 요청은 {here} 카테고리에서 처리할 수 없습니다. 홈 화면에서 '{there}'를 골라 다시 요청해 주세요.",
    "hold": "조회 결과로 확인되지 않은 인력({names})이 답변에 들어가 답변을 보류합니다. 대상 인력의 이름을 확인해 다시 요청해 주세요.",
}


def _vocabulary(service) -> set[str]:
    """질문에 나오면 업무 질문으로 보는 말: 볼 수 있는 인력 이름·업체·과제명."""
    words = set(service.project_names())
    for p in service.people():
        words.update(w for w in (p.get("name"), p.get("company")) if w)
    return {w for w in words if len(w) >= 2}


def precheck(service, question: str, agent_key: str, agent_labels: dict[str, str],
             history: list[dict] | None = None, inspection_ready: bool = False) -> dict | None:
    """LLM을 부르기 전에 답이 정해진 요청이면 {"kind", "answer"}를 돌려준다. 아니면 None."""
    q = question.strip()
    access = getattr(service, "access", None)
    if access is not None and not access.all_projects:
        # 과제 리더가 담당 밖 과제를 물으면(질문 속 '나는 ○○ 리더야'와 무관하게) 정해진 문장으로 답한다(v3 평가 P01:
        # 모델이 질문 속 담당 과제 이름을 대신 조회해 엉뚱한 목록을 보여줌). 문장은 없는 과제와 같아 존재를 알리지 않는다.
        from pmo.service import Service
        everyone = Service(service.path, clock=service.clock).project_names()
        asked = [p for p in everyone if _fold(p) in _fold(q) and not access.sees_project(p)]
        if asked:
            return {"kind": "scope", "answer": ANSWERS["scope"].format(project=asked[0],
                                                                       mine=", ".join(service.project_names()) or "없음")}
    if INJECTION.search(q):
        return {"kind": "injection", "answer": ANSWERS["injection"]}
    m = UNSUPPORTED.search(q)
    if m:
        return {"kind": "unsupported", "answer": ANSWERS["unsupported"].format(what=m.group(0))}
    owner = owner_of(q)
    follow_up = bool(history) and len(q) <= 20          # 되물음에 대한 짧은 대답("감마테크 김협력", "1번")
    known = any(w in q for w in _vocabulary(service))
    if owner is None and not follow_up and not known and not any(w in q for w in BUSINESS_WORDS):
        return {"kind": "out_of_scope", "answer": ANSWERS["out_of_scope"]}
    if owner == "inspection" and not inspection_ready and agent_key == "auto":
        return {"kind": "inspection", "answer": None}      # 검수 Agent의 정해진 안내(agents.py planned_notice)로 답한다
    if agent_key != "auto" and owner and owner != agent_key and scores(q)[agent_key] == 0 and not follow_up:
        return {"kind": "redirect", "answer": ANSWERS["redirect"].format(here=agent_labels[agent_key],
                                                                         there=agent_labels[owner])}
    return None


REDIRECT = re.compile(r"카테고리에서 처리할 수 없습니다")
_NAMED = re.compile(r"['\"‘“]([^'\"’”]{1,15})['\"’”]\s*(?:를|을)?\s*골라")


def is_redirect(answer: str) -> bool:
    return bool(REDIRECT.search(answer or ""))


def fix_redirect(answer: str, question: str, agent_key: str, agent_labels: dict[str, str]) -> str:
    """모델이 안내한 카테고리가 없거나 틀리면 바로잡는다(v2 평가 A01 '계정 관리', A02 '구매계약')."""
    owner = owner_of(question)
    if owner and owner != agent_key:
        return ANSWERS["redirect"].format(here=agent_labels[agent_key], there=agent_labels[owner])
    named = _NAMED.search(answer or "")
    if owner is None and (not named or named.group(1) not in agent_labels.values()):
        return ANSWERS["out_of_scope"]
    return answer


# --- 인력 이름 ----------------------------------------------------------------------------------
def similar_names(service, value: str, n: int = 3) -> list[str]:
    """오타로 보이는 이름의 후보(볼 수 있는 인력만). 예: 김졍직 → 김정직."""
    names = sorted({p["name"] for p in service.people() if p.get("name")})
    return difflib.get_close_matches(str(value or "").strip(), names, n=n, cutoff=0.6)


def _fold(text: str) -> str:
    return str(text or "").casefold().replace(" ", "")


PERSON_FIELDS = ("person_id", "new_person_id", "name")


def ungrounded_person(service, tool: str, args: dict, texts: list[str], seen: list[str]) -> str | None:
    """사용자가 말하지 않은 인력을 도구 입력에 넣었으면 되물을 문장(v2 평가 G02·C08 '김협력').

    texts: 이번 질문·이전 대화, seen: 이번 턴에 앞서 받은 도구 결과(JSON 문자열).
    """
    if tool == "search_people" or tool == "search_contracts":
        return None
    people = service.people()
    by_id = {p["id"]: p for p in people}
    names = {p["name"] for p in people if p.get("name")}
    pool = _fold(" ".join(texts + seen))
    for field in PERSON_FIELDS:
        value = str(args.get(field) or "").strip()
        if not value:
            continue
        if value in by_id:
            if _fold(value) in pool or _fold(by_id[value]["name"]) in pool:
                continue
            name = by_id[value]["name"]
        else:
            name = next((n for n in sorted(names, key=len, reverse=True) if n in value), None)
            if name is None or _fold(name) in pool:
                continue                     # 등록되지 않은 이름(오타 등)은 도구가 되묻는다
        return (f"'{name}'은(는) 사용자가 말하지 않은 인력입니다. 대상을 지어내지 말고, 누구의 일인지 사용자에게 물어보세요.")
    return None


def ungrounded_names_in_answer(service, answer: str, texts: list[str], seen: list[str]) -> list[str]:
    """답변에 나온 인력 이름 중 사용자 문장·도구 결과 어디에도 없는 이름."""
    pool = " ".join(texts + seen)
    names = sorted({p["name"] for p in service.people() if p.get("name") and len(p["name"]) >= 2}, key=len, reverse=True)
    found = []
    for n in names:
        if n in answer and n not in pool and not any(n != m and n in m and m in answer for m in names):
            found.append(n)
    return found


# --- 도구 입력 정리 --------------------------------------------------------------------------------
PLACEHOLDERS = {"미입력", "없음", "미정", "-", "null", "none", "n/a", "na", "모름", "알 수 없음", "알수없음", "미상",
                "unknown", "tbd", "?"}


def drop_placeholders(args: dict) -> dict:
    """모델이 모르는 값 자리에 '미입력'·'없음' 같은 말을 넣으면 값을 안 준 것으로 본다(v2 평가 X02 매입단가 '미입력')."""
    return {k: v for k, v in args.items()
            if not (isinstance(v, str) and v.strip().casefold() in PLACEHOLDERS)}


def _date_forms(d: date) -> set[str]:
    return {d.isoformat(), f"{d.month}월{d.day}일", f"{d.month}/{d.day}", f"{d.month:02d}-{d.day:02d}",
            f"{d.year}.{d.month}.{d.day}", f"{d.month}.{d.day}"}


def date_said(value: str, texts: list[str]) -> bool:
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return False
    pool = _fold(" ".join(texts))
    return any(_fold(f) in pool for f in _date_forms(d))


# 사용자가 말해야 쓰는 선택 날짜(말하지 않으면 도구가 기존 종료일을 쓴다). v2 평가 X01: 모델이 종료일을 지어내 '시작일보다 빠름' 오류
OPTIONAL_DATES = {"prepare_assignment_update": ("end_date",), "prepare_assignment_transfer": ("end_date",),
                  "prepare_assignment_replace": ("end_date",)}
_DAY = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_MONTH = re.compile(r"(\d{1,2})\s*월")


def _year_for(month: int, today: date) -> int:
    """'12월'의 연도: 오늘 기준 가까운 쪽(지난 달이 6개월 넘게 지났으면 내년)."""
    return today.year + 1 if month < today.month - 6 else today.year - 1 if month > today.month + 6 else today.year


def tidy_args(tool: str, args: dict, question: str, texts: list[str], today: date) -> dict:
    """도구 입력을 가드레일 규칙으로 정리한다."""
    args = drop_placeholders(args)
    for field in OPTIONAL_DATES.get(tool, ()):
        if args.get(field) and not date_said(args[field], texts):
            args = {k: v for k, v in args.items() if k != field}
    if tool == "prepare_assignment_update" and not args.get("month") and not args.get("effective_date"):
        # 사용자가 '12월'(그 달만)·'12월 1일부터'(그날부터)를 말했는데 모델이 빠뜨리면 채운다(v2 평가 R02 투입 2건 되물음)
        day = _DAY.search(question)
        if day and "부터" in question:
            m, d = int(day.group(1)), int(day.group(2))
            args = dict(args, effective_date=date(_year_for(m, today), m, d).isoformat())
        elif not day:
            month = _MONTH.search(question)
            if month and 1 <= int(month.group(1)) <= 12:
                m = int(month.group(1))
                args = dict(args, month=f"{_year_for(m, today)}-{m:02d}")
    return args


def seen_text(calls: list[dict]) -> list[str]:
    """이번 턴 도구 결과(성공 데이터와 오류 문장). 오류의 '김정직을 말씀하신 건지' 같은 제안도 근거로 본다(v3 평가 T01·T03)."""
    return [json.dumps(c.get("data") if c.get("ok") else c.get("error"), ensure_ascii=False, default=str)
            for c in calls if not c.get("blocked")]


def mentioned_people(service, text: str) -> list[str]:
    """문장에 나온 등록 인력 이름(볼 수 있는 인력만, 긴 이름 우선)."""
    names = sorted({p["name"] for p in service.people() if p.get("name") and len(p["name"]) >= 2}, key=len, reverse=True)
    found = [n for n in names if n in text]
    return [n for n in found if not any(n != m and n in m for m in found)]


# 대상(인력·계약)이 있어야 하는 저장 준비 도구. 모델이 대상 없이 부르면 실행하지 않고 되묻게 한다(v3 평가 G02).
TARGET_FIELDS = ("person_id", "contract_id", "contract_no")


def missing_target(tool: str, args: dict) -> str | None:
    if tool.startswith("prepare_") and not any(str(args.get(f) or "").strip() for f in TARGET_FIELDS):
        return "누구의 일인지 알 수 없습니다. 도구를 부르지 말고 대상 인력의 이름을 사용자에게 물어보세요."
    return None
