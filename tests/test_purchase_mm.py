"""구매시스템과 같은 Full time MM 계산 (10/19 담당자가 구매시스템에 직접 넣어 받은 결과 11건)."""
from datetime import date

import pytest

from pmo.mm import WORKDAY_HOLIDAYS, calculate_full_time

PURCHASE_SYSTEM_CASES = [
    # 시작일, 종료일, 구매시스템 MM, 확인한 것
    ("2026-11-01", "2026-11-30", "1.000", "한 달 전체"),
    ("2026-11-02", "2026-11-13", "0.476", "근무일 기준(달력일 아님)"),
    ("2026-11-07", "2026-11-13", "0.238", "주말 시작"),
    ("2026-11-02", "2026-11-02", "0.048", "셋째 자리 반올림"),
    ("2026-11-16", "2026-12-15", "1.024", "월별 계산 후 합산"),
    ("2026-04-27", "2026-05-08", "0.445", "노동절은 근무일"),
    ("2026-07-13", "2026-07-24", "0.435", "제헌절은 근무일"),
    ("2026-06-01", "2026-06-12", "0.455", "지방선거일은 근무일"),
    ("2026-09-21", "2026-10-09", "0.550", "추석·대체공휴일·한글날은 휴일"),
    ("2026-11-03", "2026-12-01", "0.998", "합계를 한 번 반올림(월별 반올림이면 0.997)"),
    ("2026-01-05", "2026-02-04", "1.129", "합계를 한 번 반올림(월별 반올림이면 1.128)"),
]


@pytest.mark.parametrize("start, end, expected, why", PURCHASE_SYSTEM_CASES)
def test_matches_purchase_system(start, end, expected, why, monkeypatch):
    monkeypatch.delenv("PMO_EXTRA_HOLIDAYS", raising=False)
    result = calculate_full_time(date.fromisoformat(start), date.fromisoformat(end))
    assert result["mm"] == expected, why
    assert result["rule"] == "purchase-system-v2"


def test_basis_is_kept_for_audit():
    result = calculate_full_time(date(2026, 4, 27), date(2026, 5, 8))
    may = result["months"][1]
    assert (may["assigned_days"], may["working_days"]) == (5, 19)          # 5/1 노동절을 근무일로 셈
    assert result["workday_holidays"] == [{"date": "2026-05-01", "name": "노동절"}]
    assert result["mm_exact"].startswith("0.4449")                         # 반올림 전 값도 보존
    assert [d["date"] for d in may["excluded_holidays"]] == ["2026-05-05", "2026-05-25"]


def test_workday_holiday_names_are_only_the_confirmed_ones():
    assert WORKDAY_HOLIDAYS == {"노동절", "제헌절", "지방선거일"}
