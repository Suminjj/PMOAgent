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

        def plan(self, question, tools):
            return [("pending_contract_alerts", {})]

    report = run(Wrong(), save=False)
    assert report["passed"] == 1  # Q5만 우연히 맞음
    assert any("도구 순서" in p for r in report["results"] for p in r["problems"])
