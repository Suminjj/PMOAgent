"""SQLite connections and initial schema. Never reset an existing database."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('정','협력','컨설팅')),
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
    project TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    grade TEXT NOT NULL, sales_rate TEXT NOT NULL, purchase_rate TEXT NOT NULL,
    rate_unit TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    CHECK(end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS assignments_person ON assignments(person_id);
CREATE TABLE IF NOT EXISTS checklist (
    assignment_id TEXT NOT NULL REFERENCES assignments(id),
    item TEXT NOT NULL, status TEXT NOT NULL DEFAULT '미진행',
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(assignment_id, item)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL, entity_id TEXT NOT NULL,
    before_json TEXT, after_json TEXT NOT NULL,
    changed_at TEXT NOT NULL, actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    token TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result_id TEXT NOT NULL
);
PRAGMA user_version = 1;
"""


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
        if version == 1:
            return
        tables = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if version != 0 or tables:
            raise RuntimeError("알 수 있는 초기 DB가 아닙니다. 백업 후 스키마를 확인하세요.")
        con.executescript(SCHEMA)
