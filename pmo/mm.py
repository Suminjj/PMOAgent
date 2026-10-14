"""Monthly working-day fractions, independent of currency rounding."""
import calendar
import os
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction

import holidays


def extra_holidays() -> set[date]:
    try:
        return {date.fromisoformat(s.strip()) for s in
                os.environ.get("PMO_EXTRA_HOLIDAYS", "").split(",") if s.strip()}
    except ValueError as exc:
        raise ValueError("PMO_EXTRA_HOLIDAYS는 YYYY-MM-DD를 쉼표로 구분하세요.") from exc


def calculate_full_time(start: date, end: date) -> dict:
    if end < start:
        raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")
    public = holidays.country_holidays("KR", categories=holidays.PUBLIC, language="ko",
                                      years=range(start.year, end.year + 1), observed=True)
    if start.year < public.start_year or end.year > public.end_year:
        raise ValueError(f"공휴일 달력 지원 범위는 {public.start_year}~{public.end_year}년입니다.")
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
        value = format(Decimal(total.numerator) / Decimal(total.denominator), "f")
    return {"mm": value, "months": rows, "rule": "monthly-working-days-v1",
            "calendar": f"KR PUBLIC / holidays {holidays.__version__}",
            "inclusive_dates": True, "exact_fraction": f"{total.numerator}/{total.denominator}",
            "extra_holidays": sorted(d.isoformat() for d in extra if start.year <= d.year <= end.year)}
