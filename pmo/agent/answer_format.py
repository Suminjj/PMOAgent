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
    # 인력 찾기 요약은 현재 투입이 없으면 목록 대신 '현재 투입 없음' 문장을 준다 → 목록(dict)만 쓴다(11/15 평가 오류)
    by_start = lambda rows: sorted((a for a in rows if isinstance(a, dict)) if isinstance(rows, list) else [],
                                   key=lambda a: a.get("start_date") or "")
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


# 11/15 평가: 모델이 답변에 내부 관리번호(EV-07, DEMO-002, uuid)를 적었다(네 모델 모두 15~16문항).
# 10/20 담당자 [확정]: 내부 관리번호는 화면에 보이지 않는다 → 이번 턴 도구 결과에 나온 관리번호를 답변에서 지운다.
_ID_KEYS = {"id", "person_id", "new_person_id", "assignment_id", "contract_id", "previous_contract_id"}
# 한글 조사가 바로 붙어도(EV-05의) 찾도록 \b 대신 영문·숫자 경계를 본다
_KNOWN_ID = re.compile(r"(?<![A-Za-z0-9])(?:(?:EV|DEMO)-\d+(?!\d)|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])")


def _ids_in(value, found: set[str], names: set[str]) -> None:
    if isinstance(value, dict):
        for key, v in value.items():
            if key in _ID_KEYS and isinstance(v, str) and len(v) >= 3:
                found.add(v)
            elif key == "name" and isinstance(v, str):
                names.add(v)
            else:
                _ids_in(v, found, names)
    elif isinstance(value, list):
        for v in value:
            _ids_in(v, found, names)


def hide_internal_ids(answer: str, calls: list[dict]) -> str:
    """답변에서 내부 관리번호와 그 앞뒤 표시('관리번호', ' · ', 괄호)를 지운다. 이름·업체·계약번호는 그대로."""
    if not answer:
        return answer
    ids: set[str] = set(_KNOWN_ID.findall(answer))
    names: set[str] = set()
    for call in calls:
        # 도구 결과(data)의 관리번호만 모은다. 도구 입력(arguments)의 person_id에는 모델이 이름을 넣기도 해서
        # 그 이름까지 지우던 문제(11/15 qwen3:8b 평가: '신협력의 …'가 '의 …'로)를 막는다.
        _ids_in(call.get("data"), ids, names)
    answer = _drop_id_columns(answer)
    answer = re.sub(r"\s*(?:또는|이나|혹은)\s*(?:내부\s*)?관리\s*번호(?:로|를)?", "", answer)   # '이름 또는 관리번호로'
    ids = {i for i in ids if i in answer and i not in names and not re.fullmatch(r"[가-힣]{2,5}", i)}
    if not ids:
        return answer
    label = r"(?:내부\s*)?관리\s*번호\s*[:：]?\s*"
    for value in sorted(ids, key=len, reverse=True):
        v = re.escape(value)
        answer = re.sub(rf"\s*[(（]\s*(?:{label})?{v}\s*[)）]", "", answer)       # (EV-07) · (관리번호 EV-07)
        answer = re.sub(rf"\s*[·,/]\s*(?:{label})?{v}", "", answer)               # · EV-07 · 관리번호 EV-07
        answer = re.sub(rf"(?:{label})?{v}\s*[·,/]?\s*", "", answer)              # 맨 앞이나 홀로 쓴 경우
    answer = re.sub(r"[ \t]+\n", "\n", answer)
    answer = re.sub(r"[ \t]{2,}(?=\S)", " ", answer)
    return answer.strip()


_ID_HEADER = re.compile(r"^\s*(?:내부\s*)?관리\s*번호\s*$|^\s*(?:person_)?id\s*$", re.I)


def _drop_id_columns(answer: str) -> str:
    """표(마크다운)에 '관리번호' 열이 있으면 그 열을 통째로 뺀다."""
    lines = answer.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|") and i + 1 < len(lines) and set(lines[i + 1].replace("|", "").strip()) <= set("-: "):
            cells = [c for c in line.strip().strip("|").split("|")]
            drop = {k for k, c in enumerate(cells) if _ID_HEADER.match(c)}
            if drop:
                j = i
                while j < len(lines) and lines[j].strip().startswith("|"):
                    row = lines[j].strip().strip("|").split("|")
                    out.append("| " + " | ".join(c.strip() for k, c in enumerate(row) if k not in drop) + " |")
                    j += 1
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)
