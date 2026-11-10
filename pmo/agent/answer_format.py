"""Agent 답변을 읽기 쉬운 표로 바꾼다(11/10 담당자).

'이협력 투입 중인 과제 뭐야?'처럼 한 사람의 투입을 묻는 질문은 모델이 줄글로 길게 쓰면 읽기 어렵다.
도구 결과(인력 찾기 요약·투입 이력 조회)로 코드가 표를 만들어 답으로 쓴다. 표의 값은 도구 결과 그대로라 모델이 옮기다 틀릴 일이 없다.

표: 열 = 구분·과제명·매출귀속 프로젝트·기간·등급·투입 Type·투입 MM, 행 = 과거 → 현재 → 예정, 현재 행은 굵게.
"""
from __future__ import annotations

import re

# 한 사람의 투입(과제·기간)을 묻는 말
_TIMELINE = re.compile(r"과제|투입")
# 표보다 다른 답이 필요한 질문: MM·단가·계약·금액, 변경 요청
_OTHER = re.compile(r"mm|단가|매입|계약|금액|검수|구매|바꿔|바꾸|옮겨|옮기|교체|변경|이동|등록|삭제|수정|0\.5")
COLUMNS = ("구분", "과제명", "매출귀속 프로젝트", "기간", "등급", "투입 Type", "투입 MM")


def wants_timeline_table(question: str) -> bool:
    q = (question or "").casefold()
    return bool(_TIMELINE.search(q)) and not _OTHER.search(q)


def _person_timeline(calls: list[dict]) -> tuple[str, dict] | None:
    """이번 턴 도구 결과에서 한 사람의 투입(과거·현재·예정)을 찾는다. 마지막 결과를 쓴다."""
    found = None
    for call in calls:
        if not call.get("ok") or call.get("blocked"):
            continue
        data = call.get("data")
        if call["tool"] == "find_people" and isinstance(data, list) and len(data) == 1 and data[0].get("summary"):
            p = data[0]
            found = (f"{p['name']}({p.get('company') or '업체 미입력'})", p["summary"])
        elif call["tool"] == "get_assignment_timeline" and isinstance(data, dict) and "current" in data:
            rows = data["current"] + data["upcoming"] + data["past"]
            who = rows[0].get("name") if rows else None
            label = found[0] if found else (who or "")
            found = (label, data)
    return found


def _cell(value, empty: str = "—") -> str:
    text = str(value).strip() if value not in (None, "") else empty
    return text.replace("|", "/")


def _row(kind: str, a: dict, bold: bool) -> str:
    revenue = a.get("revenue_project")
    revenue = "미정" if not revenue or str(revenue).startswith("미정") else revenue
    period = f"{a.get('start_date') or '?'} \\~ {a.get('end_date') or '?'}"   # ~ 두 개는 취소선이 되므로 이스케이프
    cells = [kind, _cell(a.get("project"), "미입력"), revenue, period, _cell(a.get("grade")),
             _cell(a.get("assignment_type"), "미입력"), _cell(a.get("mm"), "미입력")]
    if bold:
        cells = [f"**{c}**" for c in cells]
    return "| " + " | ".join(cells) + " |"


def timeline_answer(question: str, calls: list[dict]) -> str | None:
    """한 사람의 투입을 묻는 질문이면 표로 된 답. 해당하지 않거나 투입이 하나도 없으면 None(모델 답을 그대로 쓴다)."""
    if not wants_timeline_table(question):
        return None
    found = _person_timeline(calls)
    if not found:
        return None
    label, t = found
    by_start = lambda rows: sorted(rows or [], key=lambda a: a.get("start_date") or "")
    past, current, upcoming = by_start(t.get("past")), by_start(t.get("current")), by_start(t.get("upcoming"))
    if not (past or current or upcoming):
        return None
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += [_row("과거", a, False) for a in past]
    lines += [_row("현재", a, True) for a in current]
    lines += [_row("예정", a, False) for a in upcoming]
    head = f"{label}의 투입 현황입니다" + (f" (기준일 {t['today']})." if t.get("today") else ".")
    if not current:
        head += " 현재 투입 중인 과제는 없습니다."
    return head + "\n\n" + "\n".join(lines)
