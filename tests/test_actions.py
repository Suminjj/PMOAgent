"""Agent 저장 요청 → 변경 계획(현재 → 변경) → 확인 후 실행, 그리고 계약 삭제 업무 함수 테스트."""
from datetime import date
from uuid import uuid4

import pytest

from pmo.agent.actions import build_plan, execute_plan
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


def run(service, plan, checked=False):
    return execute_plan(service, plan, token=str(uuid4()), actor="담당자", checked=checked)


def contract_of(service, pid, number=None):
    return next(c for c in service.contracts(pid) if number is None or c["contract_no"] == number)


def item_status(service, pid, item):
    aid = [a for a in service.assignments(pid) if a["end_date"] >= TODAY.isoformat()][0]["id"]
    return next(r for r in service.checklist(aid) if r["item"] == item)


# --- 도구는 계획만 만들고 저장하지 않는다 ---------------------------------------------------

@pytest.mark.parametrize("tool, args, action", [
    ("prepare_checklist_update", {"person_id": "DEMO-002", "item": "ID카드 신청", "status": "진행 중"},
     "checklist_update"),
    ("prepare_contract_save", {"person_id": "DEMO-002", "end_date": "2026-12-31"}, "contract_save"),
    ("prepare_contract_delete", {"person_id": "DEMO-003"}, "contract_delete"),
    ("prepare_account_id", {"person_id": "DEMO-002", "account_id": "lee.fake"}, "account_id"),
    ("prepare_alert_ack", {"person_id": "DEMO-002"}, "alert_ack"),
])
def test_prepare_tools_build_plans_without_saving(service, tool, args, action):
    before = snapshot(service)
    result = call_tool(service, tool, args)
    assert result["ok"], result.get("error")
    plan = result["data"]["plan"]
    assert plan["action"] == action and plan["rows"] and plan["params"]
    assert all(set(r) == {"항목", "현재", "변경"} for r in plan["rows"])
    assert "저장하지 않았습니다" in result["data"]["message"]
    assert snapshot(service) == before


def test_agent_sees_prepare_and_query_tools():
    names = {t["name"] for t in tool_specs()}
    assert {"query_database", "prepare_checklist_update", "prepare_contract_save",
            "prepare_contract_delete", "prepare_account_id", "prepare_alert_ack"} <= names


# --- 대상 확정: 이름·계약번호 보완, 애매하면 되물음 ----------------------------------------------

def test_name_and_title_are_resolved_to_person(service):
    plan = build_plan(service, "checklist_update",
                      {"person_id": "이가상 차장", "item": "ID카드 신청", "status": "진행 중"})
    assert "DEMO-002" in plan["rows"][0]["현재"]


def test_ambiguous_or_missing_targets_ask_instead_of_planning(service):
    with pytest.raises(ValidationError, match="2명.*DEMO-001.*DEMO-005"):
        build_plan(service, "checklist_update", {"person_id": "김가상", "item": "좌석 배정", "status": "진행 중"})
    with pytest.raises(ValidationError, match="없습니다"):
        build_plan(service, "account_id", {"person_id": "홍길동", "account_id": "x"})
    with pytest.raises(ValidationError, match="누구"):
        build_plan(service, "account_id", {"account_id": "x"})
    with pytest.raises(ValidationError, match="계약이 3건"):          # 김가상(알파) 계약 여러 건
        build_plan(service, "contract_save", {"person_id": "DEMO-001", "end_date": "2027-03-31"})
    with pytest.raises(ValidationError, match="찾지 못했습니다"):
        build_plan(service, "contract_delete", {"person_id": "DEMO-002", "contract_id": "없는번호"})
    with pytest.raises(ValidationError, match="알림이 2건"):
        build_plan(service, "alert_ack", {})
    with pytest.raises(ValidationError, match="구매계약 대상이 아닙니다"):
        build_plan(service, "contract_save", {"person_id": "DEMO-004", "mode": "신규"})


def test_missing_or_no_op_values_are_reported(service):
    with pytest.raises(ValidationError, match="어느 항목"):
        build_plan(service, "checklist_update", {"person_id": "DEMO-002", "status": "완료"})
    with pytest.raises(ValidationError, match="사유"):
        build_plan(service, "checklist_update", {"person_id": "DEMO-002", "item": "방문예약", "status": "해당 없음"})
    with pytest.raises(ValidationError, match="이미 '미진행'"):
        build_plan(service, "checklist_update", {"person_id": "DEMO-002", "item": "좌석 배정", "status": "미진행"})
    with pytest.raises(ValidationError, match="시작일과 종료일"):
        build_plan(service, "contract_save", {"person_id": "DEMO-002", "mode": "신규"})


# --- 확인 후 실행 ----------------------------------------------------------------------

def test_checklist_plan_executes_and_done_needs_check(service):
    plan = build_plan(service, "checklist_update", {"person_id": "DEMO-002", "item": "ID카드 신청", "status": "완료"})
    assert plan["check"] and "실제 신청" in plan["check"]
    with pytest.raises(ValidationError, match="확인란"):
        run(service, plan, checked=False)
    assert item_status(service, "DEMO-002", "ID카드 신청")["status"] == "미진행"
    message = run(service, plan, checked=True)
    assert "저장했습니다" in message
    assert item_status(service, "DEMO-002", "ID카드 신청")["status"] == "완료"


def test_contract_update_by_number_and_new_contract(service):
    target = contract_of(service, "DEMO-002", "DEMO-C-200")
    plan = build_plan(service, "contract_save",
                      {"person_id": "이가상", "contract_id": "DEMO-C-200", "end_date": "2026-12-31"})
    assert any(r["항목"] == "계약 종료일" and r["현재"] == target["end_date"] and r["변경"] == "2026-12-31"
               for r in plan["rows"])
    run(service, plan)
    assert contract_of(service, "DEMO-002")["end_date"] == "2026-12-31"
    new = build_plan(service, "contract_save", {"person_id": "DEMO-002", "mode": "신규",
                                                "start_date": "2027-01-01", "end_date": "2027-06-30"})
    run(service, new)
    assert len(service.contracts("DEMO-002")) == 2


def test_stale_plan_is_blocked(service):
    plan = build_plan(service, "contract_save", {"person_id": "DEMO-002", "end_date": "2026-12-31"})
    target = contract_of(service, "DEMO-002")
    service.save_contract(dict(target, end_date="2026-11-30"), token=str(uuid4()), actor="다른 사람",
                          contract_id=target["id"], expected_version=target["version"])
    with pytest.raises(ConflictError):
        run(service, plan)


def test_delete_account_and_alert_plans(service):
    delete = build_plan(service, "contract_delete", {"person_id": "DEMO-003"})
    assert delete["check"]
    with pytest.raises(ValidationError):
        run(service, delete)
    run(service, delete, checked=True)
    assert not service.contracts("DEMO-003")
    run(service, build_plan(service, "account_id", {"person_id": "DEMO-002", "account_id": "lee.fake"}))
    assert next(p for p in service.people() if p["id"] == "DEMO-002")["account_id"] == "lee.fake"
    run(service, build_plan(service, "alert_ack", {"person_id": "DEMO-002"}))
    assert all(a["person_id"] != "DEMO-002" for a in service.pending_alerts())


def test_delete_blocked_when_extension_points_to_contract(service):
    with pytest.raises(ValidationError, match="연장 계약"):
        build_plan(service, "contract_delete", {"person_id": "DEMO-001", "contract_id": "DEMO-C-101"})


# --- 계약 삭제 업무 함수 -----------------------------------------------------------------

def test_delete_contract_removes_alerts_and_keeps_audit(service):
    target = contract_of(service, "DEMO-002", "DEMO-C-200")
    service.delete_contract(target["id"], token=str(uuid4()), actor="담당자", expected_version=target["version"])
    assert not service.contracts("DEMO-002")
    assert not any(a["contract_id"] == target["id"] for a in service.pending_alerts())
    audit = next(h for h in service.history() if h["entity_id"] == target["id"])
    assert '"deleted": true' in audit["after_json"] and "DEMO-C-200" in audit["before_json"]


def test_delete_contract_guards(service):
    first = contract_of(service, "DEMO-001", "DEMO-C-101")
    with pytest.raises(ValidationError, match="연장 계약"):
        service.delete_contract(first["id"], token=str(uuid4()), actor="담당자", expected_version=first["version"])
    other = contract_of(service, "DEMO-003")
    t = str(uuid4())
    service.delete_contract(other["id"], token=t, actor="담당자", expected_version=other["version"])
    assert service.delete_contract(other["id"], token=t, actor="담당자",
                                   expected_version=other["version"]) == other["id"]  # 재실행 안전
    with pytest.raises(ConflictError, match="이미 삭제"):
        service.delete_contract(other["id"], token=str(uuid4()), actor="담당자", expected_version=other["version"])
