"""Agent 도구 계층과 평가 하네스 테스트."""
from datetime import date

import pytest

from pmo.demo import seed_demo
from pmo.harness import OraclePlanner, run
from pmo.service import Service
from pmo.tools import TOOLS, call_tool, tool_specs

TODAY = date(2026, 10, 17)


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "tools.db", clock=lambda: TODAY)
    seed_demo(s, TODAY)
    return s


def test_specs_hide_planned_tools_by_default():
    names = {t["name"] for t in tool_specs()}
    assert "contracts_ending" in names and "monthly_purchase_needs" not in names
    assert {t["name"] for t in tool_specs(include_planned=True)} == set(TOOLS)


def test_call_tool_success_and_run_log(service):
    result = call_tool(service, "contracts_ending", {"year": "2026", "month": 10, "company": "베타컨설팅"})
    assert result["ok"] and [r["name"] for r in result["data"]] == ["이가상"]
    assert result["duration_ms"] >= 0
    assert service.run_log()[0]["operation"] == "tool:contracts_ending"


@pytest.mark.parametrize("name, args, message", [
    ("nope", {}, "없는 도구"),
    ("monthly_purchase_needs", {"year": 2026, "month": 11}, "구현 예정"),
    ("contracts_ending", {"year": 2026}, "필수 입력"),
    ("contracts_ending", {"year": 2026, "month": "열"}, "정수"),
    ("find_people", {"name": "가상", "sql": "DROP TABLE people"}, "알 수 없는 입력"),
    ("contracts_ending", {"year": 2026, "month": 13}, "1~12"),
])
def test_call_tool_errors_are_returned_not_raised(service, name, args, message):
    result = call_tool(service, name, args)
    assert result["ok"] is False and message in result["error"]
    assert service.run_log()[0]["ok"] == 0


def test_demo_seed_refuses_non_empty_db(service):
    with pytest.raises(RuntimeError):
        seed_demo(service, TODAY)


def test_oracle_eval_all_pass():
    report = run(OraclePlanner(), save=False)
    assert report["passed"] == report["total"] == 7, report["results"]


def test_eval_detects_wrong_tool_choice():
    class Wrong:
        name = "wrong"

        def plan(self, question, tools, service):
            return [("pending_contract_alerts", {})]

    report = run(Wrong(), save=False)
    assert report["passed"] == 1  # Q5만 우연히 맞음
    assert any("도구 순서" in p for r in report["results"] for p in r["problems"])


def test_purchase_needs_is_planned_but_returns_ending_contracts_as_reference(service):
    """11/09: 판정 기준 미확정 → 실패 그대로, 그 달 계약 종료 예정 인력을 참고로 붙인다."""
    result = call_tool(service, "monthly_purchase_needs", {"year": 2026, "month": 10})
    assert result["ok"] is False and "구현 예정" in result["error"] and "판정하지 않습니다" in result["error"]
    assert result["data"]["basis"].startswith("계약 종료 예정")
    assert [r["name"] for r in result["data"]["ending_contracts"]] == \
        [r["name"] for r in service.contracts_ending(2026, 10)]


def test_days_left_wording_for_agent(service):
    """11/11: 'D-46'을 모델이 '46일 전에 종료'로 거꾸로 옮김 → 도구 결과는 '종료까지 46일 남음'."""
    from pmo.tools import _plain_days
    assert _plain_days({"label": "종료 D-46", "rows": [{"state": "종료 D-5"}, "만료 3일 경과", "오늘 종료"]}) == \
        {"label": "종료까지 46일 남음", "rows": [{"state": "종료까지 5일 남음"}, "만료 3일 경과", "오늘 종료"]}
    rows = call_tool(service, "contracts_ending", {"year": 2026, "month": 10})["data"]
    assert not any(str(r.get("label", "")).startswith("종료 D-") for r in rows)
