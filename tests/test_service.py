import json
import sqlite3
from uuid import uuid4

import pytest

from pmo.db import connection
from pmo.service import ConflictError, Service, ValidationError


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path / "test.db")


def person(id="FAKE-001", **changes):
    return dict(id=id, name="가상 인력", company="가상 업체", category="협력", **changes)


def assignment(**changes):
    data = dict(person_id="FAKE-001", project="가상 과제", start_date="2026-10-01",
                end_date="2026-10-31", grade="L6", sales_rate="1234567.89",
                purchase_rate="1000000.01", rate_unit="원/MM")
    return dict(data, **changes)


def save_person(service, data=None, **kwargs):
    return service.save_person(data or person(), token=str(uuid4()), actor="테스트", **kwargs)


def save_assignment(service, data=None, **kwargs):
    return service.save_assignment(data or assignment(), token=str(uuid4()), actor="테스트", **kwargs)


def test_persistence_same_name_distinct_ids_and_audit(service):
    save_person(service)
    save_person(service, person("FAKE-002"))
    reopened = Service(service.path)
    assert len(reopened.people()) == 2
    assert reopened.history()[0]["actor"] == "테스트"
    assert "+09:00" in reopened.history()[0]["changed_at"]


@pytest.mark.parametrize("data", [
    dict(person(), id=" "), dict(person(), name=""), dict(person(), category="기타")
])
def test_invalid_person_not_saved(service, data):
    with pytest.raises(ValidationError):
        save_person(service, data)
    assert not service.people() and not service.history()


def test_update_history_and_stale_version(service):
    save_person(service)
    updated = dict(person(), name="가상 인력 수정")
    save_person(service, updated, expected_version=1)
    assert service.people()[0]["version"] == 2
    history = service.history()[0]
    assert json.loads(history["before_json"])["name"] == "가상 인력"
    assert json.loads(history["after_json"])["name"] == "가상 인력 수정"
    with pytest.raises(ConflictError):
        save_person(service, dict(person(), company="다른 업체"), expected_version=1)
    assert len(service.history()) == 2


def test_duplicate_id_and_idempotent_requests(service):
    token = str(uuid4())
    args = dict(token=token, actor="테스트")
    first = service.save_person(person(), **args)
    assert service.save_person(person(), **args) == first
    assert len(service.history()) == 1
    with pytest.raises(ConflictError):
        service.save_person(dict(person(), name="다른 인력"), **args)
    with pytest.raises(ValidationError):
        save_person(service)


@pytest.mark.parametrize("changes", [
    {"end_date": "2026-09-30"}, {"start_date": "2026-02-30"},
    {"purchase_rate": "-1"}, {"sales_rate": "NaN"}, {"sales_rate": "Infinity"},
    {"purchase_rate": "abc"}, {"rate_unit": ""}, {"grade": "L9"},
    {"person_id": "없는 ID"}
])
def test_invalid_assignment_not_saved(service, changes):
    save_person(service)
    with pytest.raises(ValidationError):
        save_assignment(service, assignment(**changes))
    assert service.assignments() == []
    assert len(service.history()) == 1


def test_assignments_overlap_precision_and_checklists(service):
    save_person(service)
    token = str(uuid4())
    first = service.save_assignment(assignment(), token=token, actor="테스트")
    assert service.save_assignment(assignment(), token=token, actor="테스트") == first
    assert service.assignments()[0]["purchase_rate"] == "1000000.01"
    assert len(service.overlaps(assignment())) == 1
    with pytest.raises(ValidationError):
        save_assignment(service)
    second = save_assignment(service, assignment(project="가상 과제 B"), overlap_reviewed=True)
    assert first != second
    third = save_assignment(service, assignment(start_date="2026-11-01", end_date="2026-11-30"))
    assert len(service.assignments()) == 3
    with connection(service.path) as con:
        assert con.execute("SELECT count(*) FROM checklist").fetchone()[0] == 12
        assert con.execute("SELECT count(*) FROM checklist WHERE assignment_id=?", (third,)).fetchone()[0] == 4
    save_assignment(service, assignment(project="수정 과제"), assignment_id=first,
                    expected_version=1, overlap_reviewed=True)
    with pytest.raises(ConflictError):
        save_assignment(service, assignment(), assignment_id=first, expected_version=1,
                        overlap_reviewed=True)


def test_audit_failure_rolls_back_all_writes(service):
    save_person(service)
    with connection(service.path) as con:
        con.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON audit BEGIN
                       SELECT RAISE(ABORT, 'test failure'); END""")
    with pytest.raises(ValidationError):
        save_assignment(service)
    assert not service.assignments()
    with connection(service.path) as con:
        assert con.execute("SELECT count(*) FROM checklist").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM requests").fetchone()[0] == 1


def test_unknown_database_preserved(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE existing(value TEXT)")
        con.execute("INSERT INTO existing VALUES ('preserve')")
    with pytest.raises(RuntimeError):
        Service(path)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT * FROM existing").fetchone()[0] == "preserve"
