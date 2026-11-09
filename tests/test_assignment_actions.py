"""Agent 투입 변경·과제 이동·인력 교체: 변경 계획(확인 카드) → 확인 후 한 트랜잭션 저장 (평가용 가상 데이터)."""
import json
from uuid import uuid4

import pytest

from pmo.agent.actions import build_plan, execute_plan
from pmo.agent.runner import _ungrounded
from pmo.db import connection
from pmo.access import authenticate
from pmo.eval_data import EVAL_PASSWORD, TODAY, seed_eval
from pmo.service import ConflictError, Service, ValidationError
from pmo.tools import call_tool


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "assign.db", clock=lambda: TODAY)
    seed_eval(s, TODAY)
    return s


def snapshot(service):
    with connection(service.path) as con:
        return [tuple(r) for t in ("assignments", "contracts")
                for r in con.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]


def run(service, plan, token=None):
    return execute_plan(service, plan, token=token or str(uuid4()), actor="담당자", checked=True)


def rows_of(service, pid):
    return sorted(service.assignments(pid), key=lambda a: a["start_date"])


@pytest.mark.parametrize("tool, args", [
    ("prepare_assignment_update", {"person_id": "신협력", "month": "2026-12", "revenue_project": "30133686-D047"}),
    ("prepare_assignment_transfer", {"person_id": "명협력", "to_project": "APC Analysis", "start_date": "2026-12-01"}),
    ("prepare_assignment_replace", {"person_id": "류협력", "new_person_id": "노협력", "start_date": "2026-12-01",
                                    "grade": "L6"}),
])
def test_tools_only_build_plans(service, tool, args):
    before = snapshot(service)
    result = call_tool(service, tool, args)
    assert result["ok"], result.get("error")
    assert all(set(r) == {"항목", "현재", "변경"} for r in result["data"]["plan"]["rows"])
    assert snapshot(service) == before


def test_month_change_touches_only_that_month(service):
    """질문 R02: 월마다 다른 매출귀속 프로젝트 중 12월만 바꾼다."""
    before = {a["start_date"]: a["revenue_project"] for a in rows_of(service, "EV-17")}
    run(service, build_plan(service, "assignment_update",
                            {"person_id": "신협력", "month": "2026-12", "revenue_project": "30133686-D047"}))
    after = {a["start_date"]: a["revenue_project"] for a in rows_of(service, "EV-17")}
    assert after == dict(before, **{"2026-12-01": "30133686-D047"})


def test_month_change_inside_long_assignment_splits_it(service):
    """기간 일부(12월)만 바뀌면 투입을 셋으로 나누고, 계약 연결을 풀어 검수에서 빠지지 않게 한다."""
    original = rows_of(service, "EV-21")[0]
    run(service, build_plan(service, "assignment_update",
                            {"person_id": "명협력", "month": "2026-12", "revenue_project": "30133686-D050"}))
    parts = rows_of(service, "EV-21")
    assert [(a["start_date"], a["end_date"], a["revenue_project"], a["mm"]) for a in parts] == [
        ("2026-09-01", "2026-11-30", "30133686-D048", "3.000"),
        ("2026-12-01", "2026-12-31", "30133686-D050", "1.000"),
        ("2027-01-01", "2027-02-28", "30133686-D048", "2.000")]
    assert parts[0]["id"] == original["id"]
    contract = service.contracts("EV-21")[0]
    assert contract["assignment_id"] is None
    december = service.inspection("감마테크", "450012202", "2026-12", "2026-12")["selected"]["lines"]
    # 11/09 계약번호 하나에 여러 인력: 명협력 12월 몫은 바뀐 매출귀속 프로젝트(D050)로 잡힌다
    assert [line["revenue_project"] for line in december if line["name"] == "명협력"] == ["30133686-D050"]


def test_transfer_ends_old_project_and_starts_new(service):
    """질문 X02: 기존 과제에서 빠지고 다른 과제로 투입."""
    run(service, build_plan(service, "assignment_transfer",
                            {"person_id": "명협력", "to_project": "APC Analysis", "start_date": "2026-12-01"}))
    old, new = rows_of(service, "EV-21")
    assert (old["project"], old["end_date"]) == ("Soft Lab 고도화", "2026-11-30")
    assert (new["project"], new["start_date"], new["end_date"]) == ("APC Analysis", "2026-12-01", "2027-02-28")
    assert new["revenue_project"] is None and new["grade"] == old["grade"]          # 매출귀속 프로젝트는 미정
    timeline = service.assignment_timeline("EV-21", TODAY)
    assert [a["project"] for a in timeline["current"]] == ["Soft Lab 고도화"]
    assert [a["project"] for a in timeline["upcoming"]] == ["APC Analysis"]


def test_replace_swaps_people_and_contracts(service):
    """질문 X01: 류협력 → 노협력 교체. 투입·계약을 한 번에 맞춘다."""
    run(service, build_plan(service, "assignment_replace", {"person_id": "류협력", "new_person_id": "노협력",
                                                            "start_date": "2026-12-01", "grade": "L6"}))
    old, = rows_of(service, "EV-19")
    new, = rows_of(service, "EV-20")
    assert old["end_date"] == "2026-11-30"
    assert (new["project"], new["revenue_project"], new["start_date"], new["end_date"], new["grade"]) == (
        "전표작성 자동화", "30133686-D047", "2026-12-01", "2027-01-31", "L6")
    assert service.contracts("EV-19")[0]["end_date"] == "2026-11-30"
    c, = service.contracts("EV-20")
    assert (c["status"], c["contract_no"], c["assignment_id"], c["company"]) == ("준비", None, new["id"], "알파솔루션")


@pytest.mark.parametrize("action, values, words", [
    ("assignment_replace", {"person_id": "류협력", "new_person_id": "노협력", "start_date": "2026-12-01"}, "등급"),
    ("assignment_replace", {"person_id": "류협력", "start_date": "2026-12-01", "grade": "L6"}, "교체 인력"),
    ("assignment_transfer", {"person_id": "명협력", "to_project": "APC Analisys", "start_date": "2026-12-01"},
     "'APC Analysis'"),
    ("assignment_transfer", {"person_id": "명협력", "to_project": "Soft Lab 고도화", "start_date": "2026-12-01"},
     "이미"),
    ("assignment_update", {"person_id": "신협력", "revenue_project": "30133686-D047"}, "2건"),
    ("assignment_update", {"person_id": "신협력", "month": "2026-12", "revenue_project": "30133686-D046"}, "바뀌는 값"),
    ("assignment_update", {"person_id": "최협력", "effective_date": "2026-11-16", "grade": "L6"}, "Part time"),
    ("assignment_update", {"person_id": "김협력", "month": "2026-12", "revenue_project": "30133686-D050"}, "2명"),
])
def test_missing_or_ambiguous_values_ask_back(service, action, values, words):
    with pytest.raises(ValidationError, match=words):
        build_plan(service, action, values)


def test_all_or_nothing_and_stale_plan(service):
    plan = build_plan(service, "assignment_transfer",
                      {"person_id": "명협력", "to_project": "APC Analysis", "start_date": "2026-12-01"})
    # 계획을 만든 뒤 데이터가 바뀌면(버전) 저장 안 함, 앞의 변경도 남지 않음
    stale = json.loads(json.dumps(plan))
    stale["params"]["changes"][-1]["expected_version"] += 5
    before = snapshot(service)
    with pytest.raises(ConflictError):
        run(service, stale)
    assert snapshot(service) == before
    token = str(uuid4())
    first = run(service, plan, token)
    assert run(service, plan, token) == first                       # 같은 요청 재실행: 한 번만 저장
    assert len(rows_of(service, "EV-21")) == 2


def test_leader_gets_no_card_and_cannot_save(service):
    """v2: 과제 리더는 저장 함수가 막힌다. 11/08부터 카드는 '수정 요청'(PM·PMO 승인 후 반영)으로 받는다."""
    leader = Service(service.path, clock=lambda: TODAY,
                     access=authenticate(service.path, "leader-softlab", EVAL_PASSWORD))
    result = call_tool(leader, "prepare_assignment_transfer",
                       {"person_id": "명협력", "to_project": "APC Analysis", "start_date": "2026-12-01"})
    assert result["ok"] and result["data"]["plan"]["request"] and "PM·PMO가 승인" in result["data"]["message"]
    plan = build_plan(service, "assignment_transfer",
                      {"person_id": "명협력", "to_project": "APC Analysis", "start_date": "2026-12-01"})
    with pytest.raises(ValidationError):
        run(leader, plan)
    other = call_tool(leader, "get_project_members", {"project": "APC Analisys"})
    assert not other["ok"] and "APC Analysis" not in other["error"]               # 다른 과제 이름을 알리지 않음


def test_values_the_user_did_not_say_are_rejected(service):
    texts = ["신협력 12월 매출귀속 프로젝트를 D047로 바꿔줘."]
    assert _ungrounded(service, "prepare_assignment_update", {"revenue_project": "30133686-D047"}, texts) is None
    assert _ungrounded(service, "prepare_assignment_update", {"revenue_project": "30133686-D099"}, texts)
    assert _ungrounded(service, "prepare_assignment_replace", {"grade": "L5"}, ["노협력(L6)으로 교체해줘"])
