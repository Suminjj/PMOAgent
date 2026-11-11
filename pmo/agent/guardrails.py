"""v3 Agent 가드레일: 허용된 범위에서만 답한다.

프롬프트에만 맡기지 않고 코드로 막는다(작은 로컬 모델은 지시를 늘 따르지는 않음, v2 평가 11/04).

- 사전 검사(precheck): LLM을 부르기 전에 정해진 방식으로 답한다.
  지시 무시 시도 · 관리하지 않는 정보(연봉·연락처 등) · 업무 범위 밖 · 다른 카테고리 업무
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
# 11/10 담당자 경계: 인력에 대한 정보(기본 정보·투입·MM·단가·변경)는 인력관리, 구매계약(계약번호·계약 기간·연장·종료 예정·
# 만료 알림·구매 필요)은 구매계약, 돈(검수 금액·계약 총액)은 검수. '종료'·'끝나'·'언제까지'는 투입과 계약 양쪽에 쓰는 말이라
# 점수를 주지 않는다(투입·과제가 있으면 인력관리, 계약·구매·연장이 있으면 구매계약, 둘 다 없으면 되묻기 → router).
OWNER_KEYWORDS: dict[str, dict[str, int]] = {
    "staff": {"투입": 1, "과제": 1, "매출귀속": 2, "귀속": 1, "등급": 1, "mm": 1, "계정 id": 2, "계정id": 2,
              "계정 아이디": 2, "아이디": 2, "교체": 1, "옮겨": 1, "이동": 1, "직무": 1, "단가": 1, "소속": 1},
    "contract": {"계약": 1, "구매": 1, "알림": 1, "만료": 1, "연장": 1, "종료 예정": 1, "챙겨": 1},
    "inspection": {"검수": 3, "총액": 3, "금액": 3, "얼마 나가": 3, "얼마 줘": 3, "지급": 3},
}
# 검수 Agent가 맡는 돈 말. 이 말 없이 '검수'만 나오고 MM을 물으면 인력관리(MM)다('검수 전에 확인하려는데 정협력 11월 MM').
MONEY_WORDS = ("금액", "총액", "얼마 나가", "얼마 줘", "지급")
# 끝나는 날을 묻는 말. 투입·과제(인력관리)나 계약·구매(구매계약) 없이 나오면 어느 쪽인지 되묻는다(11/10 담당자).
END_WORDS = re.compile(r"종료|끝나|끝난|끝내|끝날|언제까지|마감")
# 카테고리는 정할 수 없지만 업무 질문인 말(인력 목록·변경 요청 등)
BUSINESS_WORDS = ("인력", "사람", "명단", "목록", "리스트", "몇 명", "업체", "협력", "컨설팅", "정직원", "등록", "삭제",
                  "수정", "변경", "바꿔", "기간", "언제", "현황", "누구")


def scores(question: str) -> dict[str, int]:
    q = question.casefold()
    s = {key: sum(w for kw, w in words.items() if kw in q) for key, words in OWNER_KEYWORDS.items()}
    if "mm" in q:
        if any(w in q for w in MONEY_WORDS):
            s["staff"] = max(s["staff"] - OWNER_KEYWORDS["staff"]["mm"], 0)   # 검수 금액 결과에 인력별 MM이 함께 나온다
        else:
            s["inspection"] = 0                                               # MM 조회는 인력관리
    return s


def end_date_unclear(question: str) -> bool:
    """끝나는 날을 묻는데 투입인지 구매계약인지 알 수 없는 질문('박협력 언제 끝나?', '이협력 종료일 12월 31일로 바꿔줘')."""
    return bool(END_WORDS.search(question)) and not any(scores(question).values())


def owner_of(question: str) -> str | None:
    s = scores(question)
    best = max(s.values())
    top = [k for k, v in s.items() if v == best]
    return top[0] if best > 0 and len(top) == 1 else None


# --- 사전 검사 ----------------------------------------------------------------------------------
INJECTION = re.compile(r"(지시|규칙|프롬프트|명령|설정)\S{0,3}\s*(은|는|을|를)?\s*(다\s*)?(무시|잊어|따르지)"
                       r"|시스템\s*프롬프트|system\s*prompt|ignore\s+(all|previous|the)", re.I)
# 제공하지 않는 업무(출입·장비 신청 등). 등록 인력 이름이 있어도 LLM이 지어내지 않게 범위 밖으로 답한다.
NOT_OFFERED = re.compile(r"근무\s*환경|방문\s*예약|ID\s*카드|OA\s*기기|노트북|모니터|계정\s*신청|출입증", re.I)
UNSUPPORTED = re.compile(r"연봉|급여|월급|보너스|성과급|연락처|전화번호|휴대폰|핸드폰|주소|주민등록|생년월일|인사\s*평가|평가\s*결과")

ANSWERS = {
    "scope": "'{project}' 과제는 조회할 수 있는 과제가 아닙니다. 담당 과제({mine})만 조회할 수 있습니다.",
    "person_scope": "'{name}'은(는) 조회할 수 있는 인력이 아닙니다. {mine} 과제 인력만 조회가 가능합니다.",
    "redirect_failed": "이 요청은 {here} 업무인데 처리하지 못했습니다. 인력 이름과 기간 등을 넣어 다시 요청해 주세요.",
    "injection": "이전 지시를 무시하거나 바꾸라는 요청은 따를 수 없습니다. 인력·투입·구매계약 업무를 요청해 주세요.",
    "unsupported": "이 서비스는 인력의 {what} 정보를 관리하지 않아 답할 수 없습니다. "
                   "투입 과제·기간, 구매계약 정보만 조회할 수 있습니다.",
    "out_of_scope": "이 요청은 도와드릴 수 없습니다. 인력·투입, 구매계약, 검수 업무만 도와드립니다. "
                    "예: '김정직 지금 무슨 과제에 투입되어 있어?'",
    "redirect": "이 요청은 {here} 카테고리에서 처리할 수 없습니다. 홈 화면에서 '{there}'를 골라 다시 요청해 주세요.",
    "hold": "조회 결과로 확인되지 않은 인력({names})이 답변에 들어가 답변을 보류합니다. 대상 인력의 이름을 확인해 다시 요청해 주세요.",
    "ask_target": "누구의 일인지 알 수 없습니다. 대상 인력의 이름을 알려 주세요.",
}


def _vocabulary(service) -> set[str]:
    """질문에 나오면 업무 질문으로 보는 말: 볼 수 있는 인력 이름·업체·과제명."""
    words = set(service.project_names())
    for p in service.people():
        words.update(w for w in (p.get("name"), p.get("company")) if w)
    return {w for w in words if len(w) >= 2}


PERSON_SCOPE = re.compile(r"과제 인력만 조회가 가능합니다")


def person_scope_message(service, name: str) -> str:
    """11/11 담당자: 과제 리더가 볼 수 없는 인력을 물으면 '등록되어 있지 않습니다' 대신 담당 과제 인력만 조회된다고 안내한다.

    다른 과제에 있는 인력이든 아예 없는 이름이든 같은 문장이라 다른 과제 인력의 존재는 알리지 않는다.
    """
    mine = ", ".join(service.project_names()) or "담당"
    return ANSWERS["person_scope"].format(name=str(name or "").strip(), mine=mine)


def hidden_people(service, question: str) -> list[str]:
    """질문 속 등록 인력 중 이 사용자(과제 리더)가 볼 수 없는 이름. 전체 접근이면 빈 목록."""
    access = getattr(service, "access", None)
    if access is None or access.all_projects:
        return []
    from pmo.service import Service
    visible = {p["name"] for p in service.people() if p.get("name")}
    everyone = {p["name"] for p in Service(service.path, clock=service.clock).people() if p.get("name")}
    q = question or ""
    # 볼 수 있는 이름의 일부로만 나온 경우(예: '김협력' 안의 '협력')는 빼고, 동명이인이 볼 수 있는 쪽에 있으면 묻지 않는다
    return sorted(n for n in everyone - visible
                  if len(n) >= 2 and n in q and not any(n in v and v in q for v in visible))


def precheck(service, question: str, agent_key: str, agent_labels: dict[str, str],
             history: list[dict] | None = None) -> dict | None:
    """LLM을 부르기 전에 답이 정해진 요청이면 {"kind", "answer"}를 돌려준다. 아니면 None."""
    q = question.strip()
    access = getattr(service, "access", None)
    if access is not None and not access.all_projects:
        # 과제 리더가 담당 밖 과제를 물으면(질문 속 '나는 ○○ 리더야'와 무관하게) 정해진 문장으로 답한다(v3 평가 P01:
        # 모델이 질문 속 담당 과제 이름을 대신 조회해 엉뚱한 목록을 보여줌). 문장은 없는 과제와 같아 존재를 알리지 않는다.
        from pmo.service import Service
        everyone = Service(service.path, clock=service.clock).project_names()
        asked = [p for p in everyone if _fold(p) in _fold(q) and not access.sees_project(p)]
        # 11/08 수정 요청 승인: 담당 인력을 다른 과제로 옮기는 변경 요청은 막지 않는다(PM·PMO 승인 후 반영).
        # 담당 과제 인력이 질문에 있고 변경 말(옮겨·교체 등)이 있을 때만 — 담당 밖 과제의 인력 조회는 그대로 막는다.
        if asked and any(w in q for w in CHANGE_WORDS) and mentioned_people(service, q):
            asked = []
        if asked:
            return {"kind": "scope", "answer": ANSWERS["scope"].format(project=asked[0],
                                                                       mine=", ".join(service.project_names()) or "없음")}
        # 11/11 담당자: 다른 과제 인력을 물으면 '등록되어 있지 않습니다'(전체에 없는 사람처럼 보임) 대신 담당 과제 인력만 조회된다고 답한다
        hidden = hidden_people(service, q)
        if hidden and not mentioned_people(service, q):
            return {"kind": "person_scope", "answer": person_scope_message(service, hidden[0])}
    if INJECTION.search(q):
        return {"kind": "injection", "answer": ANSWERS["injection"]}
    if NOT_OFFERED.search(q):
        return {"kind": "out_of_scope", "answer": ANSWERS["out_of_scope"]}
    m = UNSUPPORTED.search(q)
    if m:
        return {"kind": "unsupported", "answer": ANSWERS["unsupported"].format(what=m.group(0))}
    owner = owner_of(q)
    follow_up = bool(history) and len(q) <= 20          # 되물음에 대한 짧은 대답("감마테크 김협력", "1번")
    known = any(w in q for w in _vocabulary(service))
    if owner is None and not follow_up and not known and not any(w in q for w in BUSINESS_WORDS) \
            and not END_WORDS.search(q):
        return {"kind": "out_of_scope", "answer": ANSWERS["out_of_scope"]}
    if agent_key != "auto" and owner and owner != agent_key and scores(q)[agent_key] == 0 and not follow_up:
        return {"kind": "redirect", "answer": ANSWERS["redirect"].format(here=agent_labels[agent_key],
                                                                         there=agent_labels[owner])}
    return None


REDIRECT = re.compile(r"카테고리에서 처리할 수 없습니다")
NAMED_CATEGORY = _NAMED = re.compile(r"['\"‘“]([^'\"’”]{1,15})['\"’”]\s*(?:를|을)?\s*골라")


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


def invented_name(tool: str, args: dict, texts: list[str], seen: list[str]) -> str | None:
    """인력 찾기에 사용자가 말하지 않은(등록되지도 않은) 이름을 넣으면 실행하지 않는다.

    v3.1 PC 평가 G02(라우터): '계약 종료일 12월 31일로 바꿔줘'에 '김민수'를 지어내 찾고 '등록되지 않았다'고 답함.
    등록된 이름은 ungrounded_person이 보고, 오타(사용자 문장에 있는 이름)는 도구가 되묻는다.
    """
    if tool != "find_people":
        return None
    value = str(args.get("name") or "").strip()
    pool = _fold(" ".join(texts + seen))
    if not value or _fold(value) in pool or _fold(value.split()[0]) in pool:
        return None
    return (f"'{value}'은(는) 사용자가 말한 이름이 아닙니다. 이름을 지어내지 말고, "
            "누구의 일인지 사용자에게 물어보세요.")


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


def period_said(start: str, end: str, texts: list[str]) -> bool:
    """기간 MM 계산의 기간을 사용자가 말했는지: 날짜, 그 달('8월'), '이번 달'처럼 달을 가리키는 말."""
    if date_said(start, texts) or date_said(end, texts):
        return True
    joined = " ".join(texts)
    if re.search(r"(이번|지난|다음|저번)\s*달|올해|작년|내년|분기", joined):
        return True
    months = {int(m) for m in _MONTH_ANY.findall(joined)}
    try:
        return bool(months & {date.fromisoformat(str(start)[:10]).month, date.fromisoformat(str(end)[:10]).month})
    except ValueError:
        return False


_MONTH_ANY = re.compile(r"(\d{1,2})\s*월")
RATE_FIELDS = ("purchase_rate", "sales_rate", "rate")


def drop_bad_rates(args: dict) -> dict:
    """단가 칸에 숫자가 아닌 값('L6'·'미정' 등)을 넣으면 값을 안 준 것으로 본다(v3.1 PC 평가 X01 '매입단가는 숫자로')."""
    def numeric(v) -> bool:
        try:
            float(str(v).replace(",", "").replace("원", "").strip())
            return True
        except ValueError:
            return False
    return {k: v for k, v in args.items() if k not in RATE_FIELDS or v in (None, "") or numeric(v)}


# 사용자가 말해야 쓰는 선택 날짜(말하지 않으면 도구가 기존 종료일을 쓴다). v2 평가 X01: 모델이 종료일을 지어내 '시작일보다 빠름' 오류
OPTIONAL_DATES = {"prepare_assignment_update": ("end_date",), "prepare_assignment_transfer": ("end_date",),
                  "prepare_assignment_replace": ("end_date",)}
_GRADE = re.compile(r"(?<![A-Za-z])(L\d)(?!\d)")
_DAY = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_MONTH = re.compile(r"(\d{1,2})\s*월")


def _year_for(month: int, today: date) -> int:
    """'12월'의 연도: 오늘 기준 가까운 쪽(지난 달이 6개월 넘게 지났으면 내년)."""
    return today.year + 1 if month < today.month - 6 else today.year - 1 if month > today.month + 6 else today.year


def tidy_args(tool: str, args: dict, question: str, texts: list[str], today: date) -> dict:
    """도구 입력을 가드레일 규칙으로 정리한다."""
    args = drop_bad_rates(drop_placeholders(args))
    for field in OPTIONAL_DATES.get(tool, ()):
        if args.get(field) and not date_said(args[field], texts):
            args = {k: v for k, v in args.items() if k != field}
    if tool == "prepare_assignment_replace" and not args.get("grade"):
        # '노협력(L6)으로 교체'의 등급을 모델이 빠뜨림(v3 평가 X01 전문). 사용자 문장에 등급이 하나뿐일 때만 채운다
        grades = set(_GRADE.findall(question))
        if len(grades) == 1:
            args = dict(args, grade=grades.pop())
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


CHANGE_WORDS = ("바꿔", "변경", "수정", "삭제", "등록", "처리해", "옮겨", "교체", "넣어")


def hold_answer(service, question: str, names: list[str]) -> tuple[str, str]:
    """답변 보류 문장. 대상 없이 변경을 요청했으면 지어낸 이름 대신 대상을 묻는다(v3 평가 G02 전문)."""
    if not mentioned_people(service, question) and any(w in question for w in CHANGE_WORDS):
        return ANSWERS["ask_target"], "ask_target"
    return ANSWERS["hold"].format(names=", ".join(names)), "hold"


def mentioned_people(service, text: str) -> list[str]:
    """문장에 나온 등록 인력 이름(볼 수 있는 인력만, 긴 이름 우선)."""
    names = sorted({p["name"] for p in service.people() if p.get("name") and len(p["name"]) >= 2}, key=len, reverse=True)
    found = [n for n in names if n in text]
    return [n for n in found if not any(n != m and n in m for m in found)]


def pin_person(service, tool: str, args: dict, question: str) -> dict | None:
    """인력 찾기 입력을 질문 속 인력(한 명)으로 바로잡는다. 바꿀 것이 없으면 None.

    11/10 PC 평가: 이름 자리에 다른 값을 넣어 18명이 나오거나(R01), 등록된 김컨설을 '없다'고 답하거나(P02),
    '감마테크 김협력'에서 업체를 빼 동명이인 둘이 나와 기간 대신 되물음(S06).
    """
    if tool != "find_people":
        return None
    names = mentioned_people(service, question)
    if len(names) != 1:
        return None
    fixed = dict(args)
    if str(args.get("name") or "").strip() != names[0]:
        fixed["name"] = names[0]
    companies = {p["company"] for p in service.people() if p.get("name") == names[0] and p.get("company")}
    company = str(args.get("company") or "").strip()
    if company and not any(company in c for c in companies):
        fixed.pop("company")                # 그 사람의 업체가 아닌 값(분류·이름 등)을 업체 칸에 넣음
    if not company or "company" not in fixed:
        said = [c for c in companies if c in question]
        if len(companies) > 1 and len(said) == 1:
            fixed["company"] = said[0]      # 동명이인을 질문 속 업체로 가린다
    return fixed if fixed != args else None


# 연장이 필요한 인력(종료 14일 전~당일, 연장 계약 미등록)을 묻는 말(11/10 PC 평가 C08)
NEEDS_EXTENSION = re.compile(r"연장.{0,6}(필요|해야|안\s?된|안\s?한|미등록)")
# 매출귀속 프로젝트가 정해지지 않은 인력을 묻는 말(11/10 PC 평가 R04)
NO_REVENUE_PROJECT = re.compile(r"매출\s?귀속.{0,12}(미정|정해지지|안\s?정해|없는|미입력|비어)")


def condition_from_question(tool: str, args: dict, question: str) -> dict | None:
    """질문에 있는 조건을 모델이 빠뜨린 조회 입력에 채운다. 바꿀 것이 없으면 None."""
    if tool == "pending_contract_alerts" and NEEDS_EXTENSION.search(question) and not args.get("needs_extension"):
        return dict(args, needs_extension=True)
    if tool == "search_people" and NO_REVENUE_PROJECT.search(question) and not args.get("without_revenue_project"):
        return dict(args, without_revenue_project=True)
    return None


def name_read_as_category(service, tool: str, args: dict, question: str) -> str | None:
    """조건으로 인력 찾기의 분류(협력·컨설팅·정)를 질문 속 인력 이름에서 따왔으면 그 이름(질문의 인력이 한 명일 때).

    11/09: '이협력 투입 중인 과제 뭐야?'를 '이 협력 (인력)'으로 읽어 search_people(category='협력')을 부르고
    '투입 중인 협력 인력은 없습니다'라고 답함. 이름을 빼고 남은 질문에 그 분류 말이 없으면 이름에서 온 것으로 본다.
    """
    category = str(args.get("category") or "").strip()
    if tool != "search_people" or not category:
        return None
    names = mentioned_people(service, question)
    if len(names) != 1:
        return None
    rest = question.replace(names[0], " ")
    return None if category in rest else names[0]


# 대상(인력·계약)이 있어야 하는 저장 준비 도구. 모델이 대상 없이 부르면 실행하지 않고 되묻게 한다(v3 평가 G02).
TARGET_FIELDS = ("person_id", "contract_id", "contract_no")


def missing_target(tool: str, args: dict) -> str | None:
    if tool.startswith("prepare_") and not any(str(args.get(f) or "").strip() for f in TARGET_FIELDS):
        return "누구의 일인지 알 수 없습니다. 도구를 부르지 말고 대상 인력의 이름을 사용자에게 물어보세요."
    return None
