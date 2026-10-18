"""Text-to-SQL 안전장치 테스트. LLM 대신 정해진 SQL을 돌려주는 가짜 생성기를 쓴다."""
import sqlite3
from datetime import date

import pytest

from pmo.agent import text_to_sql
from pmo.agent.text_to_sql import check_select, run_select
from pmo.db import connection
from pmo.demo import seed_demo
from pmo.errors import ValidationError
from pmo.service import Service
from pmo.tools import call_tool

TODAY = date(2026, 10, 18)


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "sql.db", clock=lambda: TODAY)
    seed_demo(s, TODAY)
    return s


@pytest.fixture
def fake_sql(monkeypatch):
    """query_database가 LlamaIndex 대신 이 SQL을 쓰게 한다."""
    holder = {"sql": "", "seen": []}

    class Fake:
        def __init__(self, path, today):
            holder["seen"].append((path, today))

        def generate(self, question):
            return holder["sql"]

    monkeypatch.setattr(text_to_sql, "SQL_GENERATOR_FACTORY", Fake)
    return holder


def counts(service):
    with connection(service.path) as con:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("people", "assignments", "contracts", "checklist")}


@pytest.mark.parametrize("sql", [
    "SELECT name FROM people",
    "  select count(*) as n from contracts;  ",
    "```sql\nSELECT name FROM people LIMIT 5\n```",
    "SQLQuery: SELECT name FROM people",
    "WITH c AS (SELECT * FROM contracts) SELECT COUNT(*) FROM c",
    "SELECT replace(name, '가상', '') FROM people",
    "<think>사람 수를 세면 된다</think>SELECT COUNT(*) FROM people",
])
def test_select_is_accepted(sql):
    assert check_select(sql).lower().startswith(("select", "with"))


@pytest.mark.parametrize("sql, message", [
    ("DELETE FROM people", "SELECT"),
    ("UPDATE contracts SET end_date='2030-01-01'", "SELECT"),
    ("SELECT 1; DROP TABLE people", "한 문장"),
    ("SELECT * FROM people -- 주석", "주석"),
    ("SELECT * FROM people /* x */", "주석"),
    ("WITH x AS (DELETE FROM people RETURNING *) SELECT * FROM x", "DELETE"),
    ("PRAGMA table_info(people)", "SELECT"),
    ("", "SQL이 없습니다"),
])
def test_unsafe_sql_is_rejected(sql, message):
    with pytest.raises(ValidationError, match=message):
        check_select(sql)


def test_readonly_connection_blocks_writes_even_if_check_is_bypassed(service):
    before = counts(service)
    con = text_to_sql.readonly_connect(service.path)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM people")
    con.close()
    assert counts(service) == before


def test_authorizer_blocks_internal_tables(service):
    for table in ("requests", "run_log", "audit", "sqlite_master"):
        with pytest.raises(ValidationError, match="실행하지 못했습니다"):
            run_select(service.path, f"SELECT * FROM {table}")


def test_run_select_returns_rows_and_limits(service):
    result = run_select(service.path, "SELECT name, company FROM people ORDER BY id")
    assert result["columns"] == ["name", "company"] and result["rows"][0]["name"] == "김가상"
    many = run_select(service.path, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n "
                                    "WHERE x < 500) SELECT x FROM n", max_rows=50)
    assert len(many["rows"]) == 50 and many["truncated"] is True


def test_query_database_tool_end_to_end(service, fake_sql):
    fake_sql["sql"] = "SELECT COUNT(*) AS n FROM people WHERE category = '협력'"
    result = call_tool(service, "query_database", {"question": "협력 인력 몇 명이야?"})
    assert result["ok"] and result["data"]["rows"] == [{"n": 3}]
    assert result["data"]["sql"].startswith("SELECT COUNT")
    assert fake_sql["seen"][0][1] == TODAY
    assert service.run_log()[0]["operation"] == "tool:query_database"


def test_query_database_tool_refuses_write_sql(service, fake_sql):
    before = counts(service)
    fake_sql["sql"] = "DELETE FROM contracts"
    result = call_tool(service, "query_database", {"question": "계약 다 지워"})
    assert result["ok"] is False and "SELECT" in result["error"]
    assert counts(service) == before


def test_llamaindex_missing_gives_clear_message(service, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_llama(name, *args, **kwargs):
        if name.startswith(("llama_index", "sqlalchemy")):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_llama)
    result = call_tool(service, "query_database", {"question": "인력 수"})
    assert result["ok"] is False and "LlamaIndex" in result["error"]
