import json
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from pmo.db import connection
from pmo.service import ConflictError, Service, ValidationError


def intake():
    return {
        "person": dict(name="가상 신규 인력", company="가상 업체", category="협력"),
        "assignment": dict(grade="L6", start_date="2026-11-01", end_date="2027-01-31",
                           assignment_type="Full time"),
    }


def test_accountless_intake_retry_later_account_and_unknown_rates(tmp_path):
    service = Service(tmp_path / "intake.db")
    data = intake()
    person_id = service.save_intake(data, token="fake-new-intake", actor="테스트")
    assert UUID(person_id)
    assert service.save_intake(data, token="fake-new-intake", actor="테스트") == person_id
    assert len(service.people()) == len(service.assignments()) == 1
    assert len(service.history()) == 2
    initial = service.assignments()[0]
    assert initial["project"] is None
    assert initial["sales_rate"] is None and initial["purchase_rate"] is None
    person = service.people()[0]
    assert person["account_id"] is None
    service.save_person(dict(person, account_id="fake-issued-id"), token="fake-account-update",
                        actor="테스트", expected_version=person["version"])
    assert service.people()[0]["id"] == person_id
    assert service.people()[0]["account_id"] == "fake-issued-id"
    assert service.assignments()[0]["id"] == initial["id"]
    assert service.assignments()[0]["person_id"] == person_id
    with connection(service.path) as con:
        assert con.execute("SELECT count(*) FROM checklist").fetchone()[0] == 4
    # Missing rates stay unknown; an explicitly entered zero is a real value.
    service.save_assignment(dict(initial, purchase_rate=0, rate_unit="원/MM"),
                            token="fake-rate-update", actor="테스트",
                            assignment_id=initial["id"], expected_version=1)
    assert service.assignments()[0]["purchase_rate"] == "0"
    assert service.assignments()[0]["sales_rate"] is None
    with pytest.raises(ConflictError):
        altered = intake()
        altered["person"]["name"] = "다른 가상 인력"
        service.save_intake(altered, token="fake-new-intake", actor="테스트")


def test_same_name_company_keeps_distinct_internal_identifiers(tmp_path):
    service = Service(tmp_path / "same-name.db")
    first = service.save_intake(intake(), token="fake-1", actor="테스트")
    second = service.save_intake(intake(), token="fake-2", actor="테스트")
    assert first != second
    assert len(service.people()) == len(service.assignments()) == 2


@pytest.mark.parametrize(("part", "field", "value"), [
    ("person", "name", ""), ("person", "company", ""), ("person", "category", None),
    ("assignment", "grade", None), ("assignment", "start_date", None),
    ("assignment", "end_date", "2026-10-31")
])
def test_invalid_intake_does_not_save_partial_person(tmp_path, part, field, value):
    service = Service(tmp_path / "invalid.db")
    data = intake()
    data[part][field] = value
    with pytest.raises(ValidationError):
        service.save_intake(data, token="fake-request", actor="테스트")
    assert not service.people() and not service.assignments() and not service.history()


def test_failed_assignment_rolls_back_intake_and_audit(tmp_path):
    service = Service(tmp_path / "rollback.db")
    with connection(service.path) as con:
        con.execute("""CREATE TRIGGER fail_checklist BEFORE INSERT ON checklist BEGIN
                       SELECT RAISE(ABORT, 'fake failure'); END""")
    with pytest.raises(ValidationError):
        service.save_intake(intake(), token="fake-request", actor="테스트")
    assert not service.people() and not service.assignments() and not service.history()
    with connection(service.path) as con:
        assert con.execute("SELECT count(*) FROM requests").fetchone()[0] == 0


def legacy_database(path):
    schema = (Path(__file__).parent / "fixtures" / "schema-v1.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as con:
        con.executescript(schema)
        con.execute("INSERT INTO people VALUES ('FAKE-OLD','가상 기존 인력','가상 업체','협력',2)")
        con.execute("""INSERT INTO assignments VALUES
            ('FAKE-ASSIGN','FAKE-OLD','가상 과제','2026-10-01','2026-10-31',
             'L6','123.45','0','원/MM',3)""")
        con.execute("INSERT INTO checklist VALUES ('FAKE-ASSIGN','ID카드 신청','미진행','')")
        con.execute("INSERT INTO requests VALUES ('fake-old-token','fake-hash','FAKE-OLD')")
        con.execute("INSERT INTO audit VALUES (1,'people','FAKE-OLD',NULL,?,?,'테스트')",
                    (json.dumps({"name": "가상 기존 인력"}), "2026-10-13T18:00:00+09:00"))


def test_v1_migration_backup_and_all_relationships_preserved(tmp_path):
    path = tmp_path / "existing.db"
    legacy_database(path)
    service = Service(path)
    assert service.people()[0]["id"] == "FAKE-OLD"
    assert service.people()[0]["version"] == 2
    # Do not guess whether the old manually entered ID was a company account.
    assert service.people()[0]["account_id"] is None
    assert service.assignments()[0]["sales_rate"] == "123.45"
    assert service.assignments()[0]["version"] == 3
    assert len(service.history()) == 1
    with connection(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("SELECT count(*) FROM checklist").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    backups = list(tmp_path.glob("existing.db.backup-v1-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        assert con.execute("SELECT id FROM people").fetchone()[0] == "FAKE-OLD"
    Service(path)
    assert len(list(tmp_path.glob("existing.db.backup-v1-*.db"))) == 1
    service.save_intake(intake(), token="fake-new-intake", actor="테스트")
    assert len(service.people()) == 2


def test_failed_migration_rolls_back_schema_and_preserves_backup(tmp_path):
    path = tmp_path / "broken.db"
    legacy_database(path)
    # Simulate a damaged v1 schema, forcing failure after the new column is added.
    with sqlite3.connect(path) as con:
        con.execute("DROP TABLE assignments")
    with pytest.raises(sqlite3.OperationalError):
        Service(path)
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "account_id" not in [r[1] for r in con.execute("PRAGMA table_info(people)")]
        assert con.execute("SELECT id FROM people").fetchone()[0] == "FAKE-OLD"
    assert len(list(tmp_path.glob("broken.db.backup-v1-*.db"))) == 1
