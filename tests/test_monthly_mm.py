"""회계연도(4월~다음 해 3월) 월별 MM 표 (가상 데이터)."""
from datetime import date

import pytest

from pmo.service import Service
from tests.test_schema_v6 import intake

TODAY = date(2026, 10, 20)


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path / "monthly.db", clock=lambda: TODAY)


def add(service, start, end, kind="Full time", mm=None, token=None):
    data = intake("협력", "L6")
    data["assignment"].update(start_date=start, end_date=end, assignment_type=kind, mm=mm)
    return service.save_intake(data, token=token or f"{start}-{end}", actor="담당자")


def test_fiscal_year_and_months():
    assert Service.fiscal_year_of(date(2026, 10, 20)) == 2026
    assert Service.fiscal_year_of(date(2027, 3, 31)) == 2026
    assert Service.fiscal_year_of(date(2027, 4, 1)) == 2027
    months = Service.fiscal_months(2026)
    assert months[0] == "2026-04" and months[-1] == "2027-03" and len(months) == 12


def test_monthly_values_follow_purchase_system_rule(service):
    add(service, "2026-08-08", "2026-10-31")                   # 8월 15/20, 9·10월 전체
    add(service, "2026-04-27", "2026-05-08")                   # 4월 4/22, 5월 5/19 (노동절은 근무일)
    rows = {r["start_date"]: r for r in service.monthly_mm(2026)}
    split = rows["2026-08-08"]["months"]
    assert (split["2026-08"], split["2026-09"], split["2026-10"]) == ("0.750", "1.000", "1.000")
    assert split["2026-07"] is None and split["2026-11"] is None
    assert rows["2026-08-08"]["mm"] == "2.750"
    early = rows["2026-04-27"]["months"]
    assert (early["2026-04"], early["2026-05"]) == ("0.182", "0.263")   # 합계 0.445는 '투입 MM' 열


def test_fiscal_year_boundary_and_part_time(service):
    add(service, "2027-03-15", "2027-04-20")
    add(service, "2026-01-05", "2026-03-31")                   # 2025 회계연도라 2026 표에 없음
    add(service, "2026-11-01", "2026-11-30", kind="Part time", mm="0.5")
    rows = service.monthly_mm(2026)
    assert len(rows) == 2
    cross = next(r for r in rows if r["start_date"] == "2027-03-15")
    assert cross["months"]["2027-03"] is not None and "2027-04" not in cross["months"]
    part = next(r for r in rows if r["assignment_type"] == "Part time")
    assert all(v is None for v in part["months"].values()) and "Part time" in part["note"]
    assert part["mm"] == "0.5"


def test_assignment_page_shows_monthly_columns(monkeypatch, tmp_path):
    pytest.importorskip("streamlit")
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from pmo.config import today
    from pmo.demo import seed_demo
    path = tmp_path / "ui-mm.db"
    seed_demo(Service(path), today())
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30).run()
    app.button(key="nav_2").click().run()                       # 왼쪽 메뉴 '투입 관리'
    assert not app.exception
    table = app.dataframe[0].value
    months = ["4월", "5월", "6월", "7월", "8월", "9월", "10월", "11월", "12월", "1월", "2월", "3월"]
    assert list(table.columns) == ["이름", "업체명", *months, "투입 MM", "투입 Type", "투입 시작일", "투입 종료일"]
    assert "관리번호" not in " ".join(table.columns)
    assert table.iloc[-1]["이름"] == "월 합계 (Full time)"
    cells = table[months].values.ravel()
    assert all(isinstance(v, str) for v in cells)            # None·NaN 없이 빈 칸('')이나 '0.750' 같은 글자
