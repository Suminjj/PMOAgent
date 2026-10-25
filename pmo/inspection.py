"""검수 금액 계산 (업체·계약번호 → 매출귀속 프로젝트별 금액). DB에 의존하지 않는 순수 함수.

10/25 담당자 확정:
- 금액 = 투입 MM × 매입단가(원/MM). 프로젝트 번호 = 투입의 매출귀속 프로젝트.
- 계약 기간 전체 금액(계약 총액)과, 검수용 선택 기간(기본: 이번 달) 금액을 함께 본다.

[제안] (담당자 '선호 없음' → 기본안, 구매시스템 계약금액과 대조 후 확정 필요):
- MM은 투입 기간 ∩ 계약 기간 ∩ 조회 기간에 대해 구매시스템 방식(pmo/mm.py)으로 다시 계산한다(합계 셋째 자리 반올림).
- 금액은 인력·투입 건마다 원 미만 절사 후 합산한다. 부가세는 별도(표시하지 않음).
- Part time은 기간 전체 MM만 있어 조회 기간이 투입 기간 전체를 덮을 때만 그 MM을 쓰고, 아니면 '계산 불가'.
- 매입단가 미입력·단위가 원/MM이 아닌 건·투입 Type 미입력은 0원이 아니라 '계산 불가'로 두고 합계에서 뺀 건수를 알린다.
"""
from datetime import date
from decimal import ROUND_DOWN, Decimal

from pmo.mm import calculate_full_time

RULE = ("금액 = 투입 MM × 매입단가(원/MM), 건별 원 미만 절사 · 부가세 별도 · "
        "MM은 투입·계약·조회 기간이 겹치는 날로 구매시스템 방식 계산")
UNIT = "원/MM"
NO_PROJECT = "(매출귀속 프로젝트 미입력)"


def _overlap(*ranges: tuple[str, str]) -> tuple[date, date] | None:
    start = max(date.fromisoformat(r[0]) for r in ranges)
    end = min(date.fromisoformat(r[1]) for r in ranges)
    return (start, end) if start <= end else None


def line_items(contracts: list[dict], assignments: list[dict], period: tuple[str, str]) -> list[dict]:
    """계약 행마다 연결된 투입(연결 투입이 없으면 그 인력의 모든 투입)의 조회 기간 금액."""
    lines = []
    for c in contracts:
        if c.get("assignment_id"):
            linked = [a for a in assignments if a["id"] == c["assignment_id"]]
        else:
            linked = [a for a in assignments if a["person_id"] == c["person_id"]]
        for a in linked:
            window = _overlap((a["start_date"], a["end_date"]), (c["start_date"], c["end_date"]), period)
            if window is None:
                continue
            mm, note = None, ""
            kind = a.get("assignment_type")
            if kind == "Full time":
                try:
                    mm = calculate_full_time(*window)["mm"]
                except ValueError as exc:
                    note = f"MM 계산 실패: {exc}"
            elif kind == "Part time":
                whole = window == (date.fromisoformat(a["start_date"]), date.fromisoformat(a["end_date"]))
                if whole and a.get("mm") not in (None, ""):
                    mm = a["mm"]
                else:
                    note = "Part time은 기간 전체 MM만 있어 이 기간으로 나눌 수 없음"
            else:
                note = "투입 Type 미입력"
            rate, unit = a.get("purchase_rate"), (a.get("rate_unit") or "").replace(" ", "")
            amount = None
            if mm is not None:
                if rate in (None, ""):
                    note = "매입단가 미입력"
                elif unit != UNIT:
                    note = f"단가 단위가 {UNIT}이 아님({a.get('rate_unit') or '미입력'})"
                else:
                    amount = int((Decimal(mm) * Decimal(rate)).to_integral_value(rounding=ROUND_DOWN))
            lines.append({"name": c.get("name"), "person_id": c["person_id"], "contract_id": c["id"],
                          "revenue_project": a.get("revenue_project") or NO_PROJECT,
                          "project": a.get("project"), "assignment_type": kind,
                          "purchase_rate": rate, "start": window[0].isoformat(), "end": window[1].isoformat(),
                          "mm": mm, "amount": amount, "note": note})
    return sorted(lines, key=lambda r: (r["revenue_project"], r["name"] or "", r["start"]))


def by_project(lines: list[dict]) -> dict:
    """매출귀속 프로젝트별 합계. 계산 불가 건은 합계에서 빼고 건수를 센다."""
    projects: dict[str, dict] = {}
    for line in lines:
        p = projects.setdefault(line["revenue_project"], {"revenue_project": line["revenue_project"],
                                                          "people": set(), "done": [], "incomplete": 0})
        p["people"].add(line["person_id"])
        if line["amount"] is None:
            p["incomplete"] += 1
        else:
            p["done"].append(line)
    rows = [_summary(p.pop("done"), dict(p, people=len(p["people"]))) for p in projects.values()]
    done = [line for line in lines if line["amount"] is not None]
    return _summary(done, {"projects": sorted(rows, key=lambda r: r["revenue_project"]),
                           "incomplete": sum(r["incomplete"] for r in rows)}, total_key="total")


def _summary(done: list[dict], base: dict, total_key: str = "amount") -> dict:
    """계산된 건만 더한다. 계산된 건이 하나도 없으면 MM·금액을 0이 아니라 None(계산 불가)으로 둔다."""
    if not done:
        return dict(base, mm=None, **{total_key: None})
    return dict(base, mm=format(sum((Decimal(d["mm"]) for d in done), Decimal(0)), "f"),
                **{total_key: sum(d["amount"] for d in done)})
