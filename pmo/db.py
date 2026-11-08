"""SQLite connections and initial schema. Never reset an existing database."""
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from uuid import uuid4

V4_TABLES = """
CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
    assignment_id TEXT REFERENCES assignments(id),
    company TEXT NOT NULL, contract_no TEXT,
    start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('준비','확정')),
    previous_contract_id TEXT REFERENCES contracts(id),
    note TEXT, version INTEGER NOT NULL DEFAULT 1,
    CHECK(end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS contracts_person ON contracts(person_id);
CREATE INDEX IF NOT EXISTS contracts_end ON contracts(end_date);
CREATE TABLE IF NOT EXISTS contract_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id TEXT NOT NULL REFERENCES contracts(id),
    kind TEXT NOT NULL, end_date TEXT NOT NULL, notify_from TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '미확인' CHECK(status IN ('미확인','확인','기준일 변경')),
    created_at TEXT NOT NULL, acknowledged_at TEXT, acknowledged_by TEXT,
    UNIQUE(contract_id, kind, end_date)
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL, actor TEXT, input_json TEXT,
    ok INTEGER NOT NULL, result TEXT, error TEXT,
    duration_ms INTEGER NOT NULL, started_at TEXT NOT NULL
);
"""

# v7: 데이터 접근 권한 (사용자·역할·담당 과제, 과제 리더의 수정 요청). 11/03 담당자: 역할은 PMO·PM·과제 리더
V7_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, login TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('PMO','PM','과제 리더')),
    password_hash TEXT NOT NULL, salt TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS user_projects (
    user_id TEXT NOT NULL REFERENCES users(id), project TEXT NOT NULL,
    PRIMARY KEY(user_id, project)
);
CREATE TABLE IF NOT EXISTS change_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id TEXT NOT NULL REFERENCES users(id), requester_name TEXT NOT NULL,
    person_id TEXT REFERENCES people(id), project TEXT NOT NULL, content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '요청' CHECK(status IN ('요청','처리 완료','반려')),
    created_at TEXT NOT NULL, handled_by TEXT, handled_at TEXT, note TEXT,
    action_json TEXT
);
"""

# v6: 컨설팅 인력은 등급 없이 저장(grade NULL 허용), 매출귀속팀(revenue_team) 추가, 인력 직무(people.job_role) 추가
ASSIGNMENTS_V6 = """CREATE TABLE IF NOT EXISTS {name} (
    id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
    project TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    grade TEXT, sales_rate TEXT, purchase_rate TEXT,
    rate_unit TEXT, version INTEGER NOT NULL DEFAULT 1,
    assignment_type TEXT, mm TEXT, mm_details TEXT, revenue_project TEXT, revenue_team TEXT,
    CHECK(end_date >= start_date)
)"""
ASSIGNMENT_V5_COLUMNS = ("id, person_id, project, start_date, end_date, grade, sales_rate, purchase_rate, "
                         "rate_unit, version, assignment_type, mm, mm_details, revenue_project")

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('정','협력','컨설팅')),
    version INTEGER NOT NULL DEFAULT 1, account_id TEXT, job_role TEXT
);
{ASSIGNMENTS};
CREATE INDEX IF NOT EXISTS assignments_person ON assignments(person_id);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL, entity_id TEXT NOT NULL,
    before_json TEXT, after_json TEXT NOT NULL,
    changed_at TEXT NOT NULL, actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    token TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result_id TEXT NOT NULL
);
""".replace("{ASSIGNMENTS}", ASSIGNMENTS_V6.format(name="assignments")) + V4_TABLES + V7_TABLES + \
    "PRAGMA user_version = 9;"

LATEST_VERSION = 9
# v5: 예전 준비 항목 표에 칸 추가 (v8에서 표 자체를 지우지만, v4 이전 DB를 차례로 올릴 때 필요)
V5_CHECKLIST_COLUMNS = ("version INTEGER NOT NULL DEFAULT 1", "draft TEXT", "drafted_at TEXT",
                        "applied_at TEXT", "updated_at TEXT", "updated_by TEXT")


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connection(path) as con:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version == LATEST_VERSION:
            return
        if version == 1:
            migrate_v1(con, path)
            version = 2
        if version == 2:
            migrate_v2(con, path)
            version = 3
        if version == 3:
            migrate_v3(con, path)
            version = 4
        if version == 4:
            migrate_v4(con, path)
            version = 5
        if version == 5:
            migrate_v5(con, path)
            version = 6
        if version == 6:
            migrate_v6(con, path)
            version = 7
        if version == 7:
            migrate_v7(con, path)
            version = 8
        if version == 8:
            migrate_v8(con, path)
            return
        tables = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if version != 0 or tables:
            raise RuntimeError("알 수 있는 초기 DB가 아닙니다. 백업 후 스키마를 확인하세요.")
        con.executescript(SCHEMA)


def migrate_v1(con: sqlite3.Connection, path: Path) -> None:
    """Back up a consistent v1 snapshot, then upgrade without changing IDs."""
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        con.execute("BEGIN IMMEDIATE")
        # A second app session may have upgraded while we waited for the lock.
        if con.execute("PRAGMA user_version").fetchone()[0] >= 2:
            con.commit()
            return
        backup = path.with_name(
            path.name + ".backup-v1-" + datetime.now().strftime("%Y%m%d%H%M%S") +
            "-" + uuid4().hex[:8] + ".db"
        )
        # Read through a separate connection; backing up our write transaction
        # through the same connection can deadlock.
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        con.execute("ALTER TABLE people ADD COLUMN account_id TEXT")
        con.execute("""CREATE TABLE assignments_v2 (
            id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
            project TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            grade TEXT NOT NULL, sales_rate TEXT, purchase_rate TEXT,
            rate_unit TEXT, version INTEGER NOT NULL DEFAULT 1,
            CHECK(end_date >= start_date))""")
        con.execute("""INSERT INTO assignments_v2
            SELECT id,person_id,project,start_date,end_date,grade,
                   sales_rate,purchase_rate,rate_unit,version FROM assignments""")
        con.execute("DROP TABLE assignments")
        con.execute("ALTER TABLE assignments_v2 RENAME TO assignments")
        con.execute("CREATE INDEX assignments_person ON assignments(person_id)")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("DB 연결 관계 검증에 실패했습니다. 기존 DB와 백업을 확인하세요.")
        con.execute("PRAGMA user_version = 2")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.execute("PRAGMA foreign_keys = ON")


def migrate_v2(con: sqlite3.Connection, path: Path) -> None:
    try:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("PRAGMA user_version").fetchone()[0] >= 3:
            con.commit()
            return
        backup = path.with_name(path.name + ".backup-v2-" + uuid4().hex[:8] + ".db")
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        for column in ("assignment_type", "mm", "mm_details"):
            con.execute(f"ALTER TABLE assignments ADD COLUMN {column} TEXT")
        con.execute("PRAGMA user_version = 3")
        con.commit()
    except Exception:
        con.rollback()
        raise


def migrate_v3(con: sqlite3.Connection, path: Path) -> None:
    """Add purchase contracts, expiry alerts and the run log. Existing rows are untouched."""
    try:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("PRAGMA user_version").fetchone()[0] >= 4:
            con.commit()
            return
        backup = path.with_name(path.name + ".backup-v3-" + uuid4().hex[:8] + ".db")
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        con.execute("ALTER TABLE assignments ADD COLUMN revenue_project TEXT")
        for statement in V4_TABLES.split(";"):
            if statement.strip():
                con.execute(statement)
        con.execute("PRAGMA user_version = 4")
        con.commit()
    except Exception:
        con.rollback()
        raise


def migrate_v4(con: sqlite3.Connection, path: Path) -> None:
    """체크리스트에 상태 변경용 칸을 추가한다. 기존 항목·상태·사유는 그대로 둔다."""
    try:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("PRAGMA user_version").fetchone()[0] >= 5:
            con.commit()
            return
        backup = path.with_name(path.name + ".backup-v4-" + uuid4().hex[:8] + ".db")
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        for column in V5_CHECKLIST_COLUMNS:
            con.execute(f"ALTER TABLE checklist ADD COLUMN {column}")
        con.execute("PRAGMA user_version = 5")
        con.commit()
    except Exception:
        con.rollback()
        raise


def migrate_v5(con: sqlite3.Connection, path: Path) -> None:
    """등급을 비울 수 있게 투입 표를 다시 만들고(SQLite는 NOT NULL을 바꿀 수 없음), 매출귀속팀·직무 칸을 더한다.

    모든 행·관리번호·연결(체크리스트·계약)을 그대로 옮긴다. 시작 전에 백업한다.
    """
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("PRAGMA user_version").fetchone()[0] >= 6:
            con.commit()
            return
        backup = path.with_name(path.name + ".backup-v5-" + uuid4().hex[:8] + ".db")
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        if "job_role" not in {r[1] for r in con.execute("PRAGMA table_info(people)")}:
            con.execute("ALTER TABLE people ADD COLUMN job_role TEXT")
        columns = {r[1]: r for r in con.execute("PRAGMA table_info(assignments)")}
        if "revenue_team" not in columns or columns["grade"][3]:      # [3] = NOT NULL 여부
            con.execute(ASSIGNMENTS_V6.format(name="assignments_v6"))
            con.execute(f"INSERT INTO assignments_v6 ({ASSIGNMENT_V5_COLUMNS}) "
                        f"SELECT {ASSIGNMENT_V5_COLUMNS} FROM assignments")
            con.execute("DROP TABLE assignments")
            con.execute("ALTER TABLE assignments_v6 RENAME TO assignments")
            con.execute("CREATE INDEX IF NOT EXISTS assignments_person ON assignments(person_id)")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("DB 연결 관계 검증에 실패했습니다. 기존 DB와 백업을 확인하세요.")
        con.execute("PRAGMA user_version = 6")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.execute("PRAGMA foreign_keys = ON")


def migrate_v6(con: sqlite3.Connection, path: Path) -> None:
    """사용자·담당 과제·수정 요청 표를 더한다(기존 표는 그대로). 시작 전에 백업한다."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] >= 7:
            con.commit()
            return
        backup = path.with_name(path.name + ".backup-v6-" + uuid4().hex[:8] + ".db")
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        for statement in V7_TABLES.split(";"):
            if statement.strip():
                con.execute(statement)
        con.execute("PRAGMA user_version = 7")
        con.commit()
    except Exception:
        con.rollback()
        raise


def migrate_v8(con: sqlite3.Connection, path: Path) -> None:
    """수정 요청 승인(11/08 담당자): 과제 리더가 Agent로 만든 변경 계획을 요청에 담는 칸(action_json)을 더한다. 시작 전에 백업한다."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] >= 9:
            con.commit()
            return
        backup = path.with_name(path.name + ".backup-v8-" + uuid4().hex[:8] + ".db")
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        columns = {r[1] for r in con.execute("PRAGMA table_info(change_requests)")}
        if "action_json" not in columns:
            con.execute("ALTER TABLE change_requests ADD COLUMN action_json TEXT")
        con.execute("PRAGMA user_version = 9")
        con.commit()
    except Exception:
        con.rollback()
        raise


def migrate_v7(con: sqlite3.Connection, path: Path) -> None:
    """근무 환경 준비 기능 삭제(11/06 담당자): 준비 항목 표와 그 변경 이력·실행 기록을 지운다. 시작 전에 백업한다."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] >= 8:
            con.commit()
            return
        backup = path.with_name(path.name + ".backup-v7-" + uuid4().hex[:8] + ".db")
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        con.execute("DROP TABLE IF EXISTS checklist")
        con.execute("DELETE FROM audit WHERE entity='checklist'")
        con.execute("DELETE FROM run_log WHERE operation LIKE '%checklist%'")
        con.execute("PRAGMA user_version = 8")
        con.commit()
    except Exception:
        con.rollback()
        raise
