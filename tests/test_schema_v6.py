"""v6: 컨설팅 등급 생략, 매출귀속팀, 직무 (10/20 담당자 실제 양식 반영). 가상 데이터만 사용."""
import sqlite3
from datetime import date

import pytest

from pmo.db import connection
from pmo.service import Service, ValidationError

TODAY = date(2026, 10, 20)


def intake(category, grade):
    return {"person": dict(name="가상", company="가상업체", category=category, job_role="컨설턴트"),
            "assignment": dict(grade=grade, start_date="2026-11-02", end_date="2026-11-30",
                               assignment_type="Full time", revenue_team="가상팀")}


def test_consulting_may_skip_grade_others_may_not(tmp_path):
    service = Service(tmp_path / "v6.db", clock=lambda: TODAY)
    pid = service.save_intake(intake("컨설팅", None), token="c", actor="담당자")
    saved = service.assignments(pid)[0]
    assert saved["grade"] is None and saved["revenue_team"] == "가상팀"
    assert service.people()[0]["job_role"] == "컨설턴트"
    for category in ("정", "협력"):
        with pytest.raises(ValidationError, match="컨설팅 인력만"):
            service.save_intake(intake(category, None), token=f"x-{category}", actor="담당자")
    other = service.save_intake(intake("협력", "L6"), token="ok", actor="담당자")
    row = service.assignments(other)[0]
    with pytest.raises(ValidationError, match="컨설팅 인력만"):
        service.save_assignment(dict(row, grade=None), token="edit", actor="담당자",
                                assignment_id=row["id"], expected_version=row["version"])


def test_v5_database_migrates_keeping_rows_links_and_backup(tmp_path):
    path = tmp_path / "v5.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE people (id TEXT PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL,
                category TEXT NOT NULL CHECK(category IN ('정','협력','컨설팅')),
                version INTEGER NOT NULL DEFAULT 1, account_id TEXT);
            CREATE TABLE assignments (id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
                project TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                grade TEXT NOT NULL, sales_rate TEXT, purchase_rate TEXT, rate_unit TEXT,
                version INTEGER NOT NULL DEFAULT 1, assignment_type TEXT, mm TEXT, mm_details TEXT,
                revenue_project TEXT, CHECK(end_date >= start_date));
            CREATE TABLE checklist (assignment_id TEXT NOT NULL REFERENCES assignments(id),
                item TEXT NOT NULL, status TEXT NOT NULL DEFAULT '미진행', reason TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1, draft TEXT, drafted_at TEXT, applied_at TEXT,
                updated_at TEXT, updated_by TEXT, PRIMARY KEY(assignment_id, item));
            CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL,
                entity_id TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL,
                changed_at TEXT NOT NULL, actor TEXT NOT NULL);
            CREATE TABLE requests (token TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result_id TEXT NOT NULL);
            CREATE TABLE contracts (id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
                assignment_id TEXT REFERENCES assignments(id), company TEXT NOT NULL, contract_no TEXT,
                start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('준비','확정')),
                previous_contract_id TEXT REFERENCES contracts(id), note TEXT,
                version INTEGER NOT NULL DEFAULT 1, CHECK(end_date >= start_date));
            CREATE TABLE contract_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id TEXT NOT NULL REFERENCES contracts(id), kind TEXT NOT NULL, end_date TEXT NOT NULL,
                notify_from TEXT NOT NULL, status TEXT NOT NULL DEFAULT '미확인',
                created_at TEXT NOT NULL, acknowledged_at TEXT, acknowledged_by TEXT,
                UNIQUE(contract_id, kind, end_date));
            CREATE TABLE run_log (id INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT NOT NULL, actor TEXT,
                input_json TEXT, ok INTEGER NOT NULL, result TEXT, error TEXT,
                duration_ms INTEGER NOT NULL, started_at TEXT NOT NULL);
            INSERT INTO people (id,name,company,category) VALUES ('OLD','기존','업체','협력');
            INSERT INTO assignments (id,person_id,project,start_date,end_date,grade,mm)
                VALUES ('A-OLD','OLD','과제','2026-10-01','2026-12-31','L6','3');
            INSERT INTO checklist (assignment_id,item,status) VALUES ('A-OLD','좌석 배정','완료');
            INSERT INTO contracts (id,person_id,assignment_id,company,contract_no,start_date,end_date,status)
                VALUES ('C-OLD','OLD','A-OLD','업체','4500000009','2026-10-01','2026-12-31','확정');
            PRAGMA user_version = 5;
        """)
    service = Service(path, clock=lambda: TODAY)
    row = service.assignments("OLD")[0]
    assert (row["id"], row["grade"], row["mm"], row["revenue_team"]) == ("A-OLD", "L6", "3", None)
    assert service.checklist("A-OLD")[0]["status"] == "완료"
    assert service.contracts("OLD")[0]["assignment_id"] == "A-OLD"
    with connection(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 6
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        grade = next(r for r in con.execute("PRAGMA table_info(assignments)") if r[1] == "grade")
        assert grade[3] == 0                                              # NULL 허용
    assert len(list(tmp_path.glob("v5.db.backup-v5-*.db"))) == 1
