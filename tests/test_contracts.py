"""구매계약·만료 알림·투입 이력 업무 함수 테스트 (가상 데이터)."""
import json
import sqlite3
from datetime import date
from uuid import uuid4

import pytest

from pmo.db import connection
from pmo.service import ConflictError, Service, ValidationError

TODAY = date(2026, 10, 17)


class Clock:
    def __init__(self, day: date):
        self.day = day

    def __call__(self) -> date:
        return self.day


@pytest.fixture
def clock():
    return Clock(TODAY)


@pytest.fixture
def service(tmp_path, clock):
    return Service(tmp_path / "contracts.db", clock=clock)


def token() -> str:
    return str(uuid4())


def add_person(service, pid, name="가상 인력", company="가상 업체", category="협력"):
    return service.save_person(dict(id=pid, name=name, company=company, category=category),
                               token=token(), actor="테스트")


def add_assignment(service, pid, start, end, project="가상 과제", **extra):
    data = dict(person_id=pid, project=project, start_date=start, end_date=end,
                grade="L6", assignment_type="Part time", mm="1", **extra)
    return service.save_assignment(data, token=token(), actor="테스트", overlap_reviewed=True)


def contract(pid="P-1", **changes):
    data = dict(person_id=pid, company="가상 업체", contract_no="C-001",
                start_date="2026-07-01", end_date="2026-10-31", status="확정")
    return dict(data, **changes)


def add_contract(service, data=None, **kwargs):
    return service.save_contract(data or contract(), token=token(), actor="테스트", **kwargs)


# --- 저장·검증 ---------------------------------------------------------------

def test_contract_saved_with_audit_and_persists(service, clock):
    add_person(service, "P-1")
    cid = add_contract(service)
    reopened = Service(service.path, clock=clock)
    [row] = reopened.contracts()
    assert row["id"] == cid and row["contract_no"] == "C-001" and row["name"] == "가상 인력"
    audit = [h for h in reopened.history() if h["entity"] == "contracts"]
    assert json.loads(audit[0]["after_json"])["end_date"] == "2026-10-31"


@pytest.mark.parametrize("changes, message", [
    (dict(end_date="2026-06-30"), "종료일"),
    (dict(start_date="2026-13-01"), "시작일"),
    (dict(company=" "), "업체명"),
    (dict(status="완료"), "상태"),
    (dict(contract_no=""), "계약번호"),
])
def test_invalid_contract_not_saved(service, changes, message):
    add_person(service, "P-1")
    with pytest.raises(ValidationError, match=message):
        add_contract(service, contract(**changes))
    assert not service.contracts()


def test_draft_contract_without_number_then_confirm(service):
    add_person(service, "P-1")
    cid = add_contract(service, contract(contract_no="", status="준비"))
    assert service.contracts()[0]["contract_no"] is None
    add_contract(service, contract(), contract_id=cid, expected_version=1)
    row = service.contracts()[0]
    assert row["status"] == "확정" and row["version"] == 2


def test_regular_staff_and_foreign_links_rejected(service):
    add_person(service, "P-1")
    add_person(service, "P-2", name="정규 인력", category="정")
    with pytest.raises(ValidationError, match="협력·컨설팅"):
        add_contract(service, contract("P-2"))
    with pytest.raises(ValidationError, match="등록된 인력"):
        add_contract(service, contract("P-404"))
    add_person(service, "P-3", name="다른 인력")
    other_assignment = add_assignment(service, "P-3", "2026-10-01", "2026-10-31")
    with pytest.raises(ValidationError, match="같은 인력의 투입"):
        add_contract(service, contract(assignment_id=other_assignment))
    other_contract = add_contract(service, contract("P-3"))
    with pytest.raises(ValidationError, match="같은 인력의 계약"):
        add_contract(service, contract(previous_contract_id=other_contract))
    assert len(service.contracts()) == 1


def test_same_token_does_not_duplicate_and_stale_version_rejected(service):
    add_person(service, "P-1")
    t = token()
    first = service.save_contract(contract(), token=t, actor="테스트")
    assert service.save_contract(contract(), token=t, actor="테스트") == first
    assert len(service.contracts()) == 1
    with pytest.raises(ConflictError):
        service.save_contract(contract(note="다른 내용"), token=t, actor="테스트")
    add_contract(service, contract(note="수정"), contract_id=first, expected_version=1)
    with pytest.raises(ConflictError):
        add_contract(service, contract(note="오래된 화면"), contract_id=first, expected_version=1)


def test_extension_and_multiple_people_on_same_contract_number(service):
    add_person(service, "P-1")
    add_person(service, "P-2", name="가상 인력 2")
    first = add_contract(service)
    add_contract(service, contract(start_date="2026-11-01", end_date="2027-01-31",
                                   contract_no="C-002", previous_contract_id=first))
    add_contract(service, contract("P-2"))
    rows = {(r["person_id"], r["contract_no"]): r for r in service.contracts()}
    assert rows[("P-1", "C-001")]["has_successor"] is True
    assert rows[("P-1", "C-002")]["previous_contract_id"] == first
    assert sum(r["contract_no"] == "C-001" for r in rows.values()) == 2


# --- 월·업체별 종료 예정 조회 ----------------------------------------------------

def test_contracts_ending_by_month_and_company(service):
    add_person(service, "P-1")
    add_person(service, "P-2", name="B 인력", company="B 업체")
    add_contract(service, contract(end_date="2026-10-01"))
    add_contract(service, contract("P-2", company="B 업체", end_date="2026-10-31"))
    add_contract(service, contract("P-2", company="B 업체", end_date="2026-11-01"))
    assert len(service.contracts_ending(2026, 10)) == 2
    [row] = service.contracts_ending(2026, 10, " b 업체 ")
    assert row["name"] == "B 인력" and row["end_date"] == "2026-10-31"
    assert [r["end_date"] for r in service.contracts_ending(2026, 11)] == ["2026-11-01"]
    with pytest.raises(ValidationError):
        service.contracts_ending(2026, 13)


# --- 만료 알림 -----------------------------------------------------------------

@pytest.mark.parametrize("today, expected", [
    (date(2026, 10, 16), None),                 # 15일 전: 대상 아님
    (date(2026, 10, 17), "종료 D-14"),           # 14일 전부터 표시
    (date(2026, 10, 31), "오늘 종료"),
    (date(2026, 11, 3), "만료 3일 경과"),         # 만료 이후에도 미처리면 계속 표시
])
def test_alert_window(service, clock, today, expected):
    clock.day = today
    add_person(service, "P-1")
    add_contract(service)
    alerts = service.pending_alerts()
    if expected is None:
        assert alerts == []
    else:
        [alert] = alerts
        assert alert["label"] == expected and alert["notify_from"] == "2026-10-17"


def test_alert_created_once_and_rechecked_on_restart(service, clock):
    add_person(service, "P-1")
    add_contract(service)
    for _ in range(3):
        assert len(service.pending_alerts()) == 1
    restarted = Service(service.path, clock=clock)
    assert len(restarted.pending_alerts()) == 1
    with connection(service.path) as con:
        assert con.execute("SELECT COUNT(*) FROM contract_alerts").fetchone()[0] == 1


def test_acknowledge_is_separate_from_renewal(service):
    add_person(service, "P-1")
    add_contract(service)
    [alert] = service.pending_alerts()
    service.acknowledge_alert(alert["id"], actor="담당자")
    assert service.pending_alerts() == []
    with pytest.raises(ConflictError):
        service.acknowledge_alert(alert["id"], actor="담당자")
    # 알림을 확인해도 계약 자체(갱신 여부)는 바뀌지 않는다.
    row = service.contracts()[0]
    assert row["has_successor"] is False and row["version"] == 1
    assert any(h["entity"] == "contract_alerts" for h in service.history())


def test_end_date_change_recalculates_alert(service, clock):
    add_person(service, "P-1")
    cid = add_contract(service)
    [old] = service.pending_alerts()
    add_contract(service, contract(end_date="2026-12-31"), contract_id=cid, expected_version=1)
    assert service.pending_alerts() == []           # 새 기준일(12/17) 전이므로 표시 안 함
    with connection(service.path) as con:
        status = con.execute("SELECT status FROM contract_alerts WHERE id=?", (old["id"],)).fetchone()[0]
    assert status == "기준일 변경"
    clock.day = date(2026, 12, 17)
    [new] = service.pending_alerts()
    assert new["end_date"] == "2026-12-31" and new["id"] != old["id"]


# --- 투입 이력 -----------------------------------------------------------------

def test_assignment_timeline_and_revenue_project(service):
    add_person(service, "P-1")
    add_assignment(service, "P-1", "2025-01-01", "2025-06-30", project="과거 과제")
    add_assignment(service, "P-1", "2026-09-01", "2026-12-31", project="현재 과제",
                   revenue_project="매출 프로젝트 A")
    add_assignment(service, "P-1", "2027-01-01", "2027-03-31", project="예정 과제")
    timeline = service.assignment_timeline("P-1")
    assert [r["project"] for r in timeline["current"]] == ["현재 과제"]
    assert timeline["current"][0]["revenue_project"] == "매출 프로젝트 A"
    assert [r["project"] for r in timeline["past"]] == ["과거 과제"]
    assert [r["project"] for r in timeline["upcoming"]] == ["예정 과제"]


def test_find_people_returns_all_namesakes(service):
    add_person(service, "P-1", name="김가상")
    add_person(service, "P-2", name="김가상", company="다른 업체")
    assert {r["id"] for r in service.find_people("가상")} == {"P-1", "P-2"}


# --- 실행 기록·마이그레이션 ---------------------------------------------------------

def test_run_log_records_success_and_failure(service):
    add_person(service, "P-1")
    with pytest.raises(ValidationError):
        add_contract(service, contract(end_date="2020-01-01"))
    log = service.run_log()
    assert [r["operation"] for r in log[:2]] == ["save_contract", "save_person"]
    assert log[0]["ok"] == 0 and "종료일" in log[0]["error"]
    assert log[1]["ok"] == 1 and log[1]["duration_ms"] >= 0
    assert "token" not in log[0]["input_json"]


def test_v3_database_migrates_with_backup_and_keeps_rows(tmp_path):
    path = tmp_path / "v3.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE people (id TEXT PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL,
                category TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, account_id TEXT);
            CREATE TABLE assignments (id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
                project TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL, grade TEXT NOT NULL,
                sales_rate TEXT, purchase_rate TEXT, rate_unit TEXT, version INTEGER NOT NULL DEFAULT 1,
                assignment_type TEXT, mm TEXT, mm_details TEXT);
            CREATE TABLE checklist (assignment_id TEXT NOT NULL, item TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '미진행', reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(assignment_id, item));
            CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL,
                entity_id TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL,
                changed_at TEXT NOT NULL, actor TEXT NOT NULL);
            CREATE TABLE requests (token TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result_id TEXT NOT NULL);
            INSERT INTO people VALUES ('OLD-1','기존 인력','기존 업체','협력',1,NULL);
            INSERT INTO assignments (id,person_id,project,start_date,end_date,grade)
                VALUES ('A-1','OLD-1','기존 과제','2026-01-01','2026-12-31','L6');
            PRAGMA user_version = 3;
        """)
    service = Service(path, clock=Clock(TODAY))
    assert service.assignments()[0]["revenue_project"] is None
    assert service.people()[0]["id"] == "OLD-1"
    backups = list(tmp_path.glob("v3.db.backup-v3-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    add_contract(service, contract("OLD-1"))
    Service(path)  # 재실행 시 다시 마이그레이션하지 않는다
    assert len(list(tmp_path.glob("v3.db.backup-v3-*.db"))) == 1
