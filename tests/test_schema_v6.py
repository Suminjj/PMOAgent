"""v6: 컨설팅 등급 생략, 매출귀속팀, 직무 (10/20 담당자 실제 양식 반영). 가상 데이터만 사용."""
import sqlite3
from datetime import date

import pytest

from pmo.db import LATEST_VERSION, connection
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
            INSERT INTO checklist (assignment_id,item,status) VALUES ('A-OLD','방문예약','완료');
            INSERT INTO contracts (id,person_id,assignment_id,company,contract_no,start_date,end_date,status)
                VALUES ('C-OLD','OLD','A-OLD','업체','4500000009','2026-10-01','2026-12-31','확정');
            PRAGMA user_version = 5;
        """)
    service = Service(path, clock=lambda: TODAY)
    row = service.assignments("OLD")[0]
    assert (row["id"], row["grade"], row["mm"], row["revenue_team"]) == ("A-OLD", "L6", "3", None)
    assert service.contracts("OLD")[0]["assignment_id"] == "A-OLD"
    with connection(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        grade = next(r for r in con.execute("PRAGMA table_info(assignments)") if r[1] == "grade")
        assert grade[3] == 0                                              # NULL 허용
    assert len(list(tmp_path.glob("v5.db.backup-v5-*.db"))) == 1
    with connection(path) as con:                                         # v8: 준비 항목 표는 지움(11/06 담당자)
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='checklist'").fetchall()
    assert len(list(tmp_path.glob("v5.db.backup-v7-*.db"))) == 1          # 지우기 전 백업


def test_v7_database_drops_checklist_with_its_history(tmp_path):
    """v8(11/06): 근무 환경 준비 표·변경 이력·실행 기록을 지우고, 나머지 데이터는 그대로 둔다."""
    path = tmp_path / "v7.db"
    service = Service(path, clock=lambda: TODAY)
    pid = service.save_intake(intake("협력", "L6"), token="t", actor="담당자")
    with connection(path) as con:                                         # v7 DB 흉내: 예전 준비 항목과 그 기록
        con.execute("PRAGMA user_version = 7")
        con.execute("CREATE TABLE checklist (assignment_id TEXT, item TEXT, status TEXT)")
        con.execute("INSERT INTO checklist VALUES ('A','ID카드 신청','완료')")
        con.execute("INSERT INTO audit(entity,entity_id,before_json,after_json,changed_at,actor) "
                    "VALUES ('checklist','A:ID카드 신청',NULL,'{}','2026-10-20','담당자')")
        con.execute("INSERT INTO run_log(operation,input_json,ok,started_at,duration_ms) "
                    "VALUES ('update_checklist_item','{}',1,'2026-10-20',1)")
    service = Service(path, clock=lambda: TODAY)
    with connection(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION == 9
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='checklist'").fetchall()
        assert not con.execute("SELECT 1 FROM audit WHERE entity='checklist'").fetchall()
        assert not con.execute("SELECT 1 FROM run_log WHERE operation LIKE '%checklist%'").fetchall()
    assert service.people()[0]["id"] == pid and service.history()                # 다른 데이터·이력은 그대로
    assert len(list(tmp_path.glob("v7.db.backup-v7-*.db"))) == 1
