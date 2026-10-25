"""검수 금액: 업체·계약번호 → 매출귀속 프로젝트별 금액, 계약 총액 (가상 데이터)."""
from datetime import date
from uuid import uuid4

import pytest

from pmo.inspection import NO_PROJECT
from pmo.service import Service, ValidationError

TODAY = date(2026, 10, 25)


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "inspection.db", clock=lambda: TODAY)
    for pid, name in (("P-1", "가상 일"), ("P-2", "가상 이"), ("P-3", "가상 삼"), ("P-4", "가상 사"), ("P-5", "가상 오")):
        s.save_person(dict(id=pid, name=name, company="가상 업체", category="협력"), token=str(uuid4()), actor="테스트")
    return s


def assign(s, pid, start, end, project, rate, kind="Full time", mm=None, unit="원/MM"):
    data = dict(person_id=pid, project="가상 과제", revenue_project=project, start_date=start, end_date=end,
                grade="L6", assignment_type=kind, mm=mm, purchase_rate=rate, rate_unit=unit if rate else None)
    return s.save_assignment(data, token=str(uuid4()), actor="테스트", overlap_reviewed=True)


def contract(s, pid, start, end, no="C-100", company="가상 업체", assignment_id=None):
    s.save_contract(dict(person_id=pid, company=company, contract_no=no, start_date=start, end_date=end,
                         status="확정", assignment_id=assignment_id), token=str(uuid4()), actor="테스트")


@pytest.fixture
def filled(service):
    a1 = assign(service, "P-1", "2026-10-01", "2026-12-31", "RP-A", "10000000")
    contract(service, "P-1", "2026-10-01", "2026-12-31", assignment_id=a1)            # 연결 투입
    assign(service, "P-2", "2026-08-01", "2027-03-31", "RP-B", "8000000")              # 계약보다 긴 투입
    contract(service, "P-2", "2026-10-01", "2026-12-31")                               # 연결 없음 → 인력의 투입
    assign(service, "P-3", "2026-10-01", "2026-10-10", "RP-A", "3333333")               # 10월 5/20 = 0.25MM
    contract(service, "P-3", "2026-10-01", "2026-10-31")
    assign(service, "P-4", "2026-11-01", "2026-11-30", "RP-B", "4000000", kind="Part time", mm="0.5")
    contract(service, "P-4", "2026-11-01", "2026-11-30")
    assign(service, "P-5", "2026-10-01", "2026-12-31", None, None)                     # 단가 미입력
    contract(service, "P-5", "2026-10-01", "2026-12-31")
    contract(service, "P-1", "2026-10-01", "2026-12-31", company="다른 업체")             # 같은 번호, 다른 업체
    return service


def test_selector_groups_contract_number(filled):
    [ours, other] = sorted(filled.inspection_contracts(), key=lambda g: g["company"] != "가상 업체")
    assert (ours["contract_no"], ours["people"], ours["start_date"], ours["end_date"]) == \
        ("C-100", 5, "2026-10-01", "2026-12-31")
    assert other["company"] == "다른 업체"


def test_month_amounts_by_revenue_project(filled):
    result = filled.inspection("가상 업체", "C-100", "2026-10", "2026-10")
    projects = {p["revenue_project"]: p for p in result["selected"]["projects"]}
    assert projects["RP-A"]["amount"] == 10_000_000 + 833_333                    # 1.000MM + 0.25MM(원 미만 절사)
    assert projects["RP-A"]["mm"] == "1.250" and projects["RP-A"]["people"] == 2
    assert projects["RP-B"]["amount"] == 8_000_000                              # 계약 기간과 겹치는 10월만
    assert "RP-B" in projects and projects["RP-B"]["incomplete"] == 0          # Part time(11월)은 10월에 없음
    assert projects[NO_PROJECT]["incomplete"] == 1 and projects[NO_PROJECT]["amount"] is None   # 0원 아님
    assert result["selected"]["total"] == 18_833_333 and result["selected"]["incomplete"] == 1
    note = next(l["note"] for l in result["selected"]["lines"] if l["person_id"] == "P-5")
    assert "매입단가 미입력" in note


def test_contract_total_uses_whole_contract_period(filled):
    result = filled.inspection("가상 업체", "C-100", "2026-10", "2026-10")
    total = result["contract"]
    projects = {p["revenue_project"]: p for p in total["projects"]}
    assert projects["RP-A"]["amount"] == 30_000_000 + 833_333                   # 10~12월 3MM + P-3
    assert projects["RP-B"]["amount"] == 24_000_000 + 2_000_000                 # 8월~3월 투입 중 계약 기간 3MM + Part time 0.5MM
    assert total["total"] == 56_833_333 and result["contract_period"] == ("2026-10-01", "2026-12-31")


def test_part_time_cannot_be_split(filled):
    nov = filled.inspection("가상 업체", "C-100", "2026-11", "2026-11")
    part = next(l for l in nov["selected"]["lines"] if l["person_id"] == "P-4")
    assert part["amount"] == 2_000_000                                          # 11월이 투입 기간 전체를 덮음
    service = filled
    a = assign(service, "P-4", "2026-12-15", "2027-01-15", "RP-B", "4000000", kind="Part time", mm="0.7")
    contract(service, "P-4", "2026-12-15", "2027-01-15", no="C-200", assignment_id=a)
    dec = service.inspection("가상 업체", "C-200", "2026-12", "2026-12")
    [line] = dec["selected"]["lines"]
    assert line["amount"] is None and "Part time" in line["note"]               # 0원으로 두지 않음


def test_unit_must_be_won_per_mm(service):
    assign(service, "P-1", "2026-10-01", "2026-10-31", "RP-A", "500000", unit="원/일")
    contract(service, "P-1", "2026-10-01", "2026-10-31")
    [line] = service.inspection("가상 업체", "C-100", "2026-10", "2026-10")["selected"]["lines"]
    assert line["amount"] is None and "원/MM" in line["note"]


def test_invalid_inputs(filled):
    with pytest.raises(ValidationError, match="계약"):
        filled.inspection("가상 업체", "없는-번호", "2026-10", "2026-10")
    with pytest.raises(ValidationError, match="늦습니다"):
        filled.inspection("가상 업체", "C-100", "2026-12", "2026-10")
    with pytest.raises(ValidationError):
        filled.inspection("가상 업체", "C-100", "2026-13", "2026-13")


def test_inspection_page_shows_totals(monkeypatch, tmp_path, filled):
    pytest.importorskip("streamlit")
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from ui.theme import PAGES
    monkeypatch.setenv("PMO_DB_PATH", str(filled.path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30).run()
    app.button(key=f"nav_{PAGES.index('검수하기')}").click().run()
    assert not app.exception
    table = app.dataframe[0].value                               # 기본 = 이번 달(2026-10은 계약 기간 안)
    assert list(table.columns) == ["인력명", "매출귀속 프로젝트", "MM", "검수금액"]
    assert table.iloc[-1]["인력명"] == "합계"


def test_period_with_only_uncomputable_lines_is_not_zero(service):
    assign(service, "P-1", "2026-10-01", "2026-10-31", "RP-A", None)
    contract(service, "P-1", "2026-10-01", "2026-10-31")
    result = service.inspection("가상 업체", "C-100", "2026-10", "2026-10")
    [project] = result["selected"]["projects"]
    assert project["amount"] is None and project["mm"] is None and project["incomplete"] == 1
    assert result["selected"]["total"] is None and result["contract"]["total"] is None
