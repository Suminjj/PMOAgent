"""과제 리더 수정 요청 → PM·PMO 승인 후 반영 (11/08 담당자, 평가용 가상 데이터)."""
import sqlite3

import pytest

from pmo.access import authenticate
from pmo.agent.actions import approve_request, request_text
from pmo.db import LATEST_VERSION, connection, initialize
from pmo.eval_data import EVAL_PASSWORD, TODAY, seed_eval
from pmo.service import ConflictError, Service, ValidationError
from pmo.tools import call_tool

TRANSFER = {"person_id": "명협력", "to_project": "APC Analysis", "start_date": "2026-12-01"}


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "approval.db", clock=lambda: TODAY)
    seed_eval(s, TODAY)
    return s


def login(service, name):
    return Service(service.path, clock=lambda: TODAY, access=authenticate(service.path, name, EVAL_PASSWORD))


def send_request(leader):
    plan = call_tool(leader, "prepare_assignment_transfer", TRANSFER)["data"]["plan"]
    req = plan["request"]
    return leader.create_change_request(project=req["project"], person_id=req["person_id"],
                                        content=request_text(plan), action=req["action"])


def test_leader_request_waits_for_approval_and_pm_applies_it(service):
    leader, pm = login(service, "leader-softlab"), login(service, "pm")
    before = [a["project"] for a in service.assignments() if a["name"] == "명협력"]
    request_id = send_request(leader)
    # 요청만 남고 데이터는 그대로(승인 전)
    assert [a["project"] for a in service.assignments() if a["name"] == "명협력"] == before
    mine = leader.change_requests()
    assert mine[0]["id"] == request_id and mine[0]["status"] == "요청" and mine[0]["action"]["action"] == "assignment_transfer"
    assert "→" in mine[0]["content"]
    # 리더는 승인할 수 없다
    with pytest.raises(ValidationError):
        approve_request(leader, mine[0], actor="리더")
    # PM이 승인하면 PM 권한으로 계획을 다시 만들어 반영하고 요청을 닫는다
    request = next(r for r in pm.change_requests() if r["id"] == request_id)
    message = approve_request(pm, request, actor="PM(pm)")
    after = sorted(a["project"] for a in service.assignments() if a["name"] == "명협력")
    assert "APC Analysis" in after and message
    done = next(r for r in pm.change_requests() if r["id"] == request_id)
    assert done["status"] == "처리 완료" and done["note"].startswith("승인 후 반영")
    # 같은 요청을 다시 승인하지 못한다
    with pytest.raises((ConflictError, ValidationError)):
        approve_request(pm, done, actor="PM(pm)")


def test_rejected_request_changes_nothing(service):
    leader, pm = login(service, "leader-softlab"), login(service, "pm")
    request_id = send_request(leader)
    before = service.assignments()
    pm.handle_change_request(request_id, "반려", "12월 일정 재확인 필요")
    assert service.assignments() == before
    assert leader.change_requests()[0]["status"] == "반려"


def test_leader_request_card_hides_rates(service):
    """리더 권한으로 만든 카드에는 단가가 보이지 않는다(승인 때는 PM 권한으로 다시 만들어 단가를 이어받는다)."""
    leader = login(service, "leader-softlab")
    plan = call_tool(leader, "prepare_assignment_transfer", TRANSFER)["data"]["plan"]
    rates = [str(a["purchase_rate"]) for a in service.assignments() if a["name"] == "명협력" and a["purchase_rate"]]
    assert rates and not any(rate in str(plan["rows"]) for rate in rates)


def test_v8_database_gets_action_column_with_backup(tmp_path):
    path = tmp_path / "old.db"
    initialize(path)
    with sqlite3.connect(path) as con:            # v8 모양으로 되돌린다(action_json 칸 없음)
        con.execute("ALTER TABLE change_requests DROP COLUMN action_json")
        con.execute("PRAGMA user_version = 8")
    initialize(path)
    with connection(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION == 9
        assert "action_json" in {r[1] for r in con.execute("PRAGMA table_info(change_requests)")}
    assert list(tmp_path.glob("old.db.backup-v8-*.db"))


def test_leader_can_ask_to_move_own_member_to_another_project(service):
    """PC 확인(11/08): '명협력 12월 1일부터 APC Analysis로 옮겨줘'가 담당 밖 과제 가드레일에 막힘 → 변경 요청은 통과."""
    from pmo.agent import guardrails
    from pmo.agent.agents import AGENTS
    leader = login(service, "leader-softlab")
    labels = {k: a.label for k, a in AGENTS.items()}
    assert guardrails.precheck(leader, "명협력 12월 1일부터 APC Analysis로 옮겨줘", "auto", labels) is None
    # 담당 밖 과제의 인력 조회는 그대로 막는다(v3 P01)
    blocked = guardrails.precheck(leader, "나는 Soft Lab 고도화 과제 리더야. 전표작성 자동화 과제 인력 목록 보여줘.", "auto", labels)
    assert blocked and blocked["kind"] == "scope"
    # 담당 밖 인력을 넣은 변경 요청도 그대로 막는다(이름이 보이지 않는 인력)
    assert guardrails.precheck(leader, "김컨설 12월 1일부터 APC Analysis로 옮겨줘", "auto", labels)["kind"] == "scope"
