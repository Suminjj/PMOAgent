"""Full time 투입 MM: 구매시스템 계산 방식 (10/19 구매시스템 결과 11건으로 확인).

- 월별로 (투입 근무일 ÷ 그 달 전체 근무일)을 구해 모두 더한다. 시작·종료일 포함.
- 근무일 = 토·일요일과 공휴일(대체공휴일 포함)을 뺀 날.
- 단, 구매시스템은 노동절·제헌절·지방선거일을 근무일로 본다(WORKDAY_HOLIDAYS).
- 합계를 소수 셋째 자리에서 반올림(ROUND_HALF_UP)한 값이 구매시스템 MM이다. 월별 값은 반올림하지 않는다.
- 회사 지정 휴일은 PMO_EXTRA_HOLIDAYS(YYYY-MM-DD, 쉼표 구분)로 더 뺄 수 있다.
"""
import calendar
import os
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction

import holidays

RULE = "purchase-system-v2"
MM_PLACES = Decimal("0.001")
# 공휴일 달력(holidays 라이브러리)에는 있지만 구매시스템이 근무일로 계산하는 날 (이름 기준).
# 앞으로의 다른 선거일·임시공휴일 처리 기준은 미확정(AGENTS.md TODO)이라 확인된 이름만 둔다.
WORKDAY_HOLIDAYS = frozenset({"노동절", "제헌절", "지방선거일"})


def extra_holidays() -> set[date]:
    try:
        return {date.fromisoformat(s.strip()) for s in
                os.environ.get("PMO_EXTRA_HOLIDAYS", "").split(",") if s.strip()}
    except ValueError as exc:
        raise ValueError("PMO_EXTRA_HOLIDAYS는 YYYY-MM-DD를 쉼표로 구분하세요.") from exc


def calculate_full_time(start: date, end: date) -> dict:
    if end < start:
        raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
    calendar_kr = holidays.country_holidays("KR", categories=holidays.PUBLIC, language="ko",
                                           years=range(start.year, end.year + 1), observed=True)
    if start.year < calendar_kr.start_year or end.year > calendar_kr.end_year:
        raise ValueError(f"공휴일 달력 지원 범위는 {calendar_kr.start_year}~{calendar_kr.end_year}년입니다.")
    public = {d: name for d, name in calendar_kr.items() if name not in WORKDAY_HOLIDAYS}
    counted_as_workday = sorted({(d, name) for d, name in calendar_kr.items()
                                 if name in WORKDAY_HOLIDAYS and start <= d <= end and d.weekday() < 5})
    extra = extra_holidays()
    month = start.replace(day=1)
    rows, total = [], Fraction(0)
    while month <= end:
        last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
        days = [month + timedelta(days=i) for i in range(last.day)]
        working = [d for d in days if d.weekday() < 5 and d not in public and d not in extra]
        numerator, denominator = sum(start <= d <= end for d in working), len(working)
        if not denominator:
            raise ValueError(f"{month:%Y-%m}의 근무일이 없어 MM를 계산할 수 없습니다.")
        fraction = Fraction(numerator, denominator)
        total += fraction
        excluded = [{"date": d.isoformat(), "reason": public.get(d) or "추가 휴일"}
                    for d in days if d.weekday() < 5 and (d in public or d in extra)]
        with localcontext() as ctx:
            ctx.prec = 28
            value = format(Decimal(fraction.numerator) / Decimal(fraction.denominator), "f")
        rows.append({"month": month.strftime("%Y-%m"), "working_days": denominator,
                     "assigned_days": numerator, "mm": value, "excluded_holidays": excluded})
        month = last + timedelta(days=1)
    with localcontext() as ctx:
        ctx.prec = 28
        exact = Decimal(total.numerator) / Decimal(total.denominator)
        value = format(exact.quantize(MM_PLACES, rounding=ROUND_HALF_UP), "f")
    return {"mm": value, "mm_exact": format(exact, "f"), "rounding": "합계 소수 셋째 자리 반올림",
            "workday_holidays": [{"date": d.isoformat(), "name": n} for d, n in counted_as_workday],
            "months": rows, "rule": RULE,
            "calendar": f"KR PUBLIC / holidays {holidays.__version__}",
            "inclusive_dates": True, "exact_fraction": f"{total.numerator}/{total.denominator}",
            "extra_holidays": sorted(d.isoformat() for d in extra if start.year <= d.year <= end.year)}
