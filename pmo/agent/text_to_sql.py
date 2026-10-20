"""자연어 조회용 Text-to-SQL (LlamaIndex) 과 읽기 전용 실행.

규칙 (AGENTS.md 5.5)
- LLM이 만든 SQL은 데이터를 바꾸지 않는 SELECT만 실행한다.
- SQL 생성은 LlamaIndex `NLSQLTableQueryEngine(sql_only=True)`로 하고, LlamaIndex가 직접 실행하지 않게 한다.
- 실행은 이 모듈의 `run_select`만 한다. 겹겹이 막는다.
    1) 문장 검사: 주석·여러 문장 금지, SELECT/WITH로 시작, 쓰기 명령어 금지
    2) 읽기 전용 연결: SQLite `mode=ro` + `PRAGMA query_only=ON`
    3) 권한 검사(authorizer): 허용한 테이블 읽기·함수만 허용, 그 밖의 모든 동작 거부
    4) 결과 행 수·실행 시간 제한
- 등록·수정·삭제는 정해진 업무 함수로만 한다(LLM은 값만 채워 제안, pmo/tools.py의 prepare_* 도구).
"""
import os
import re
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

from pmo.agent.llm import LLMError
from pmo.errors import ValidationError

# Text-to-SQL이 볼 수 있는 테이블. 저장 요청 토큰·실행 기록·변경 이력 원문은 제외한다.
SAFE_TABLES = ("people", "assignments", "contracts", "contract_alerts", "checklist")
MAX_ROWS = 200
TIME_LIMIT_SECONDS = 5.0

TABLE_CONTEXT = {
    "people": "인력. id=내부 관리번호, name=이름, company=업체명, category=분류(정/협력/컨설팅), "
              "account_id=회사 계정 ID(발급 전이면 NULL), job_role=직무.",
    "assignments": "투입. person_id=people.id, project=투입 과제, revenue_project=매출귀속 프로젝트, "
                   "revenue_team=매출귀속팀, start_date/end_date=투입 기간(YYYY-MM-DD, 양 끝 포함), "
                   "grade=등급(컨설팅은 NULL일 수 있음), "
                   "assignment_type=Full time/Part time, mm=투입 MM(문자열 숫자), "
                   "sales_rate/purchase_rate=매출·매입단가(문자열 숫자), rate_unit=단가 단위.",
    "contracts": "구매계약. person_id=people.id, assignment_id=assignments.id, company=업체명, "
                 "contract_no=계약번호(준비 상태면 NULL), start_date/end_date=계약 기간, "
                 "status=준비/확정, previous_contract_id=연장 전 계약 id.",
    "contract_alerts": "계약 종료 알림. contract_id=contracts.id, end_date=기준 종료일, "
                       "notify_from=알림 시작일(종료 14일 전), status=미확인/확인/기준일 변경.",
    "checklist": "근무 환경 준비. assignment_id=assignments.id, item=좌석 배정/ID카드 신청/OA기기 신청/방문예약, "
                 "status=미진행/진행 중/완료/해당 없음, reason=사유, applied_at=실제 신청 완료 시각.",
}

_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex|analyze|"
    r"trigger|begin|commit|rollback|savepoint|release|upsert)\b", re.I)


def context_prefix(today: date) -> str:
    return (f"오늘 날짜(Asia/Seoul)는 {today.isoformat()}이다. 날짜는 'YYYY-MM-DD' 문자열로 비교한다. "
            "SQLite의 SELECT 문 하나만 작성한다. 데이터를 바꾸는 문장은 절대 쓰지 않는다. "
            f"결과는 최대 {MAX_ROWS}행이 되도록 LIMIT을 붙인다. 이름은 people.name으로 찾고, "
            "같은 이름이 여러 명일 수 있으니 id·company도 함께 조회한다.")


def check_select(sql: str) -> str:
    """LLM이 만든 SQL이 읽기 전용 SELECT 한 문장인지 검사하고 정리된 SQL을 돌려준다."""
    text = re.sub(r"<think>.*?</think>", "", str(sql or ""), flags=re.S).strip()
    text = re.sub(r"^```(?:sql)?\s*|\s*```$", "", text, flags=re.I).strip()
    if text.lower().startswith("sqlquery:"):
        text = text[len("sqlquery:"):].strip()
    text = text.rstrip().rstrip(";").strip()
    if not text:
        raise ValidationError("만들어진 SQL이 없습니다.")
    if "--" in text or "/*" in text:
        raise ValidationError("주석이 들어간 SQL은 실행하지 않습니다.")
    if ";" in text:
        raise ValidationError("SQL은 한 문장만 실행합니다.")
    if not re.match(r"^(select|with)\b", text, re.I):
        raise ValidationError("조회(SELECT) 문만 실행합니다.")
    found = _WRITE_WORDS.search(text)
    if found:
        raise ValidationError(f"데이터를 바꿀 수 있는 명령어({found.group(1).upper()})가 있어 실행하지 않습니다.")
    return text


def _readonly_uri(path: Path) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def readonly_connect(path: Path) -> sqlite3.Connection:
    """읽기 전용 연결. 파일이 없으면 만들지 않고 실패한다."""
    con = sqlite3.connect(_readonly_uri(path), uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only = ON")
    return con


def _authorizer(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if arg1 in SAFE_TABLES else sqlite3.SQLITE_DENY
    if action == getattr(sqlite3, "SQLITE_RECURSIVE", -1):  # WITH RECURSIVE
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def run_select(path: Path, sql: str, max_rows: int = MAX_ROWS) -> dict:
    """검사를 통과한 SELECT를 읽기 전용으로 실행한다."""
    sql = check_select(sql)
    con = readonly_connect(path)
    started = time.monotonic()
    con.set_progress_handler(lambda: int(time.monotonic() - started > TIME_LIMIT_SECONDS), 10_000)
    con.set_authorizer(_authorizer)
    try:
        cursor = con.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(max_rows + 1)
    except sqlite3.DatabaseError as exc:
        raise ValidationError(f"SQL을 실행하지 못했습니다: {exc}") from exc
    finally:
        con.close()
    return {"sql": sql, "columns": columns,
            "rows": [dict(zip(columns, r)) for r in rows[:max_rows]],
            "truncated": len(rows) > max_rows}


class SQLGenerator(Protocol):
    def generate(self, question: str) -> str: ...


class LlamaIndexSQLGenerator:
    """LlamaIndex NLSQLTableQueryEngine으로 SQL만 만든다(sql_only=True, 실행하지 않음)."""

    def __init__(self, path: Path, today: date, model: str | None = None, base_url: str | None = None):
        try:
            from sqlalchemy import create_engine
            from llama_index.core import SQLDatabase
            from llama_index.core.embeddings import MockEmbedding
            from llama_index.core.query_engine import NLSQLTableQueryEngine
            from llama_index.llms.ollama import Ollama
        except ImportError as exc:
            raise LLMError("LlamaIndex가 설치되지 않아 자유 조회를 할 수 없습니다. "
                           "'pip install -r requirements.txt'로 설치하세요.") from exc
        uri = _readonly_uri(path)
        # LlamaIndex가 스키마를 읽을 때도 읽기 전용 연결만 쓴다.
        engine = create_engine("sqlite://", creator=lambda: readonly_connect(path))
        database = SQLDatabase(engine, include_tables=list(SAFE_TABLES), sample_rows_in_table_info=2)
        think = os.environ.get("PMO_OLLAMA_THINK", "false").strip().lower()
        llm_kwargs = dict(
            model=model or os.environ.get("PMO_OLLAMA_MODEL", "qwen3:8b"),
            base_url=(base_url or os.environ.get("PMO_OLLAMA_URL", "http://localhost:11434")).rstrip("/"),
            request_timeout=300.0, temperature=0.0,
            context_window=int(os.environ.get("PMO_OLLAMA_NUM_CTX", "8192")))
        if think in ("true", "false"):
            llm_kwargs["thinking"] = think == "true"
        self._uri = uri
        self._engine = NLSQLTableQueryEngine(
            sql_database=database, tables=list(SAFE_TABLES), llm=Ollama(**llm_kwargs),
            context_query_kwargs=TABLE_CONTEXT, context_str_prefix=context_prefix(today),
            sql_only=True, synthesize_response=False,
            embed_model=MockEmbedding(embed_dim=1),  # 기본 파서는 임베딩을 쓰지 않는다(외부 모델 호출 방지)
        )

    def generate(self, question: str) -> str:
        try:
            response = self._engine.query(question)
        except Exception as exc:  # noqa: BLE001 - LlamaIndex·Ollama 오류를 사용자 메시지로 바꾼다
            raise LLMError(f"Text-to-SQL로 SQL을 만들지 못했습니다: {type(exc).__name__}: {exc}"[:300]) from exc
        return str((response.metadata or {}).get("sql_query") or "")


# 테스트에서 가짜 생성기로 바꿀 수 있게 둔다: factory(path, today) -> SQLGenerator
SQL_GENERATOR_FACTORY: Callable[[Path, date], SQLGenerator] = LlamaIndexSQLGenerator


def query_database(path: Path, today: date, question: str) -> dict:
    """자연어 질문 → (LlamaIndex) SQL 생성 → 안전 검사 → 읽기 전용 실행."""
    question = str(question or "").strip()
    if not question:
        raise ValidationError("조회할 내용을 입력하세요.")
    sql = SQL_GENERATOR_FACTORY(path, today).generate(question)
    return dict(run_select(path, sql), question=question)
