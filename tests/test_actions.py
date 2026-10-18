"""Agent 저장 제안 도구(값만 채움)와 계약 삭제 업무 함수 테스트."""
from datetime import date
from uuid import uuid4

import pytest

from pmo.db import connection
from pmo.demo import seed_demo
from pmo.service import ConflictError, Service, ValidationError
from pmo.tools import call_tool, tool_specs

TODAY = date(2026, 10, 18)


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "actions.db", clock=lambda: TODAY)
    seed_demo(s, TODAY)
    return s


def snapshot(service):
    with connection(service.path) as con:
        return [tuple(r) for t in ("people", "contracts", "checklist", "contract_alerts")
                for r in con.execute(f"SELECT * FROM {t} ORDER BY 1")]


@pytest.mark.parametrize("tool, args, action", [
    ("prepare_checklist_update", {"person_id": "DEMO-002", "item": "ID카드 신청", "status": "완료"},
     "checklist_update"),
    ("prepare_contract_save", {"person_id": "DEMO-002", "end_date": "2026-12-31"}, "contract_save"),
    ("prepare_contract_delete", {"person_id": "DEMO-003"}, "contract_delete"),
    ("prepare_account_id", {"person_id": "DEMO-002", "account_id": "lee.fake"}, "account_id"),
    ("prepare_alert_ack", {}, "alert_ack"),
])
def test_prepare_tools_only_propose_and_never_save(service, tool, args, action):
    before = snapshot(service)
    result = call_tool(service, tool, args)
    assert result["ok"]
    assert result["data"]["proposal"] == {"action": action, "values": args}
    assert "저장하지 않았습니다" in result["data"]["message"]
    assert snapshot(service) == before


def test_prepare_tools_reject_values_outside_choices(service):
    result = call_tool(service, "prepare_checklist_update", {"item": "주차 등록"})
    assert result["ok"] is False and "중 하나" in result["error"]
    result = call_tool(service, "prepare_contract_save", {"status": "종료"})
    assert result["ok"] is False


def test_agent_sees_prepare_and_query_tools():
    names = {t["name"] for t in tool_specs()}
    assert {"query_database", "prepare_checklist_update", "prepare_contract_save",
            "prepare_contract_delete", "prepare_account_id", "prepare_alert_ack"} <= names


def contract_of(service, pid, number=None):
    return next(c for c in service.contracts(pid) if number is None or c["contract_no"] == number)


def test_delete_contract_removes_alerts_and_keeps_audit(service):
    target = contract_of(service, "DEMO-002", "DEMO-C-200")
    assert any(a["contract_id"] == target["id"] for a in service.pending_alerts())
    service.delete_contract(target["id"], token=str(uuid4()), actor="담당자",
                            expected_version=target["version"])
    assert not service.contracts("DEMO-002")
    assert not any(a["contract_id"] == target["id"] for a in service.pending_alerts())
    audit = next(h for h in service.history() if h["entity_id"] == target["id"])
    assert '"deleted": true' in audit["after_json"] and "DEMO-C-200" in audit["before_json"]


def test_delete_contract_guards(service):
    first = contract_of(service, "DEMO-001", "DEMO-C-101")      # 연장 계약이 가리키는 계약
    with pytest.raises(ValidationError, match="연장 계약"):
        service.delete_contract(first["id"], token=str(uuid4()), actor="담당자",
                                expected_version=first["version"])
    other = contract_of(service, "DEMO-003")
    with pytest.raises(ConflictError):
        service.delete_contract(other["id"], token=str(uuid4()), actor="담당자",
                                expected_version=other["version"] + 1)
    t = str(uuid4())
    service.delete_contract(other["id"], token=t, actor="담당자", expected_version=other["version"])
    assert service.delete_contract(other["id"], token=t, actor="담당자",
                                   expected_version=other["version"]) == other["id"]  # 재실행 안전
    with pytest.raises(ConflictError, match="이미 삭제"):
        service.delete_contract(other["id"], token=str(uuid4()), actor="담당자",
                                expected_version=other["version"])
