"""v2.1 조건 조회 도구: 계약번호·미발급 계약, 계정 ID 미발급 인력 (평가 C09·C10·A01)."""
import pytest

from pmo.access import authenticate
from pmo.agent.agents import AGENTS
from pmo.eval_data import EVAL_PASSWORD, TODAY, seed_eval
from pmo.service import Service
from pmo.tools import call_tool


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "search.db", clock=lambda: TODAY)
    seed_eval(s, TODAY)
    return s


def names(result):
    assert result["ok"], result.get("error")
    return sorted({r["name"] for r in result["data"]})


def test_search_contracts_by_number_ignores_case_and_spaces(service):
    expected = sorted({c["name"] for c in service.contracts() if c["contract_no"] == "C-2026-202"})
    assert len(expected) > 1
    assert names(call_tool(service, "search_contracts", {"contract_no": "C-2026-202"})) == expected
    assert names(call_tool(service, "search_contracts", {"contract_no": "c-2026- 202"})) == expected


def test_search_contracts_without_number(service):
    result = call_tool(service, "search_contracts", {"without_number": True})
    assert names(result) == sorted({c["name"] for c in service.contracts() if not c["contract_no"]})
    assert all(r["contract_no"] == "미발급" for r in result["data"])


def test_search_people_without_account(service):
    result = call_tool(service, "search_people", {"without_account": True})
    assert names(result) == sorted({p["name"] for p in service.people() if not p["account_id"]})
    assert all(r["account_id"] == "미발급" for r in result["data"])
    assert "id" not in result["data"][0]                                          # 내부 관리번호는 보이지 않음


@pytest.mark.parametrize("tool, args, hint", [
    ("find_people", {"name": "C-2026-202"}, "search_contracts"),
    ("get_assignment_timeline", {"person_id": "C-2026-202"}, "search_contracts"),
    ("find_people", {"name": "계정 ID 없는 사람"}, "search_people"),
])
def test_condition_in_name_slot_points_to_search_tool(service, tool, args, hint):
    result = call_tool(service, tool, args)
    text = str(result.get("error") or result.get("data"))
    assert hint in text


def test_leader_sees_only_own_project_in_search(service):
    leader = Service(service.path, clock=lambda: TODAY,
                     access=authenticate(service.path, "leader-softlab", EVAL_PASSWORD))
    visible = {p["name"] for p in leader.people()}
    for tool, args in [("search_contracts", {"without_number": True}), ("search_people", {"without_account": True}),
                       ("search_contracts", {"contract_no": "C-2026-202"})]:
        assert set(names(call_tool(leader, tool, args))) <= visible


def test_search_tools_are_given_to_the_right_agents():
    assert "search_contracts" in AGENTS["contract"].tools and "search_people" in AGENTS["staff"].tools
