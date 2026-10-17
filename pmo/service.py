"""Validated business operations shared by forms and future LLM tools."""
import functools
import hashlib
import json
import sqlite3
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from pmo.config import TIMEZONE, grades, today as kst_today
from pmo.contracts import (
    CONTRACT_CATEGORIES, expiry_state, month_range, notify_from, validate_contract,
)
from pmo.db import connection, initialize
from pmo.errors import ConflictError, ValidationError  # 기존 import 경로(pmo.service) 유지
from pmo.mm import calculate_full_time

__all__ = ["Service", "ValidationError", "ConflictError", "validate_person",
           "validate_assignment", "validate_intake", "validate_contract"]


def required(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{label}을(를) 입력하세요.")
    return text


def money(value: object, label: str) -> str:
    try:
        number = Decimal(required(value, label))
    except InvalidOperation as exc:
        raise ValidationError(f"{label}은(는) 숫자로 입력하세요.") from exc
    if not number.is_finite() or number < 0:
        raise ValidationError(f"{label}은(는) 0 이상의 유한한 숫자여야 합니다.")
    return format(number, "f")


def validate_person(data: dict) -> dict:
    result = {k: required(data.get(k), label) for k, label in (
        ("name", "이름"), ("category", "분류")
    )}
    if "id" in data:
        result["id"] = required(data["id"], "내부 관리번호")
    result["company"] = str(data.get("company") or "").strip()
    result["account_id"] = str(data.get("account_id") or "").strip() or None
    if result["category"] not in ("정", "협력", "컨설팅"):
        raise ValidationError("분류는 정·협력·컨설팅 중 선택하세요.")
    return result


def validate_assignment(data: dict) -> dict:
    result = {k: required(data.get(k), label) for k, label in (
        ("person_id", "내부 관리번호"), ("grade", "등급"),
    )}
    for key, label in (("start_date", "시작일"), ("end_date", "종료일")):
        try:
            result[key] = date.fromisoformat(str(data.get(key))).isoformat()
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{label}을(를) 올바르게 입력하세요.") from exc
    if result["end_date"] < result["start_date"]:
        raise ValidationError("종료일은 시작일보다 빠를 수 없습니다.")
    if result["grade"] not in grades():
        raise ValidationError("허용된 등급을 선택하세요.")
    result["project"] = str(data.get("project") or "").strip() or None
    # 투입 과제와 매출귀속 프로젝트는 다를 수 있다. 배분 규칙은 TODO.
    result["revenue_project"] = str(data.get("revenue_project") or "").strip() or None
    result["rate_unit"] = str(data.get("rate_unit") or "").strip() or None
    for key, label in (("sales_rate", "매출단가"), ("purchase_rate", "매입단가")):
        value = data.get(key)
        result[key] = None if value is None or str(value).strip() == "" else money(str(value), label)
    if any(result[k] is not None for k in ("sales_rate", "purchase_rate")) and not result["rate_unit"]:
        raise ValidationError("단가를 입력하면 단가 기준 단위도 입력하세요.")
    assignment_type = data.get("assignment_type")
    if assignment_type not in ("Full time", "Part time"):
        raise ValidationError("투입 Type을 선택하세요.")
    result["assignment_type"] = assignment_type
    if assignment_type == "Full time":
        try:
            detail = calculate_full_time(date.fromisoformat(result["start_date"]),
                                         date.fromisoformat(result["end_date"]))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        result["mm"] = detail["mm"]
    else:
        result["mm"] = money(str(data.get("mm")) if data.get("mm") is not None else "", "투입 MM")
        detail = {"mode": "manual", "mm": result["mm"]}
    result["mm_details"] = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    return result


def validate_intake(data: dict) -> dict:
    """Validate the information known before a company account is issued."""
    person = validate_person(data["person"])
    person.pop("id", None)
    person["company"] = required(person["company"], "업체명")
    assignment = validate_assignment(dict(data["assignment"], person_id="new-person"))
    assignment.pop("person_id")
    return {"person": person, "assignment": assignment}


def _summary(value: object) -> str:
    if isinstance(value, list):
        return f"{len(value)}건"
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 300 else text[:300] + "…"


def logged(operation: str):
    """업무 함수 실행 기록(성공 여부·소요 시간·오류)을 run_log에 남긴다.

    기록 실패가 업무 처리 결과를 바꾸지 않도록 기록 오류는 무시한다.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            started, t0 = datetime.now(TIMEZONE).isoformat(), time.perf_counter()
            try:
                result = fn(self, *args, **kwargs)
            except Exception as exc:
                self.record_run(operation, args, kwargs, False, None,
                             f"{type(exc).__name__}: {exc}", t0, started)
                raise
            self.record_run(operation, args, kwargs, True, _summary(result), None, t0, started)
            return result
        return wrapper
    return decorator


class Service:
    def __init__(self, path: Path, clock=None):
        """clock: 오늘 날짜(Asia/Seoul)를 돌려주는 함수. 테스트에서 날짜를 고정할 때 사용한다."""
        self.path = Path(path)
        self.clock = clock or kst_today
        initialize(self.path)

    def people(self) -> list[dict]:
        with connection(self.path) as con:
            return [dict(r) for r in con.execute("SELECT * FROM people ORDER BY name, id")]

    def assignments(self, person_id: str | None = None) -> list[dict]:
        sql = """SELECT a.*, p.name, p.company, p.category FROM assignments a
                 JOIN people p ON p.id=a.person_id"""
        params = ()
        if person_id is not None:
            sql += " WHERE a.person_id=?"
            params = (person_id,)
        with connection(self.path) as con:
            return [dict(r) for r in con.execute(sql + " ORDER BY a.start_date, a.id", params)]

    def history(self) -> list[dict]:
        with connection(self.path) as con:
            return [dict(r) for r in con.execute("SELECT * FROM audit ORDER BY id DESC")]

    def overlaps(self, data: dict, exclude_id: str | None = None) -> list[dict]:
        data = validate_assignment(data)
        with connection(self.path) as con:
            return self._overlaps(con, data, exclude_id)

    @staticmethod
    def _overlaps(con, data, exclude_id):
        return [dict(r) for r in con.execute(
            """SELECT * FROM assignments WHERE person_id=? AND start_date<=?
               AND end_date>=? AND (? IS NULL OR id<>?)""",
            (data["person_id"], data["end_date"], data["start_date"], exclude_id, exclude_id)
        )]

    @logged("save_person")
    def save_person(self, data: dict, *, token: str, actor: str,
                    expected_version: int | None = None) -> str:
        return self._save("people", validate_person(data), token, actor, expected_version)

    @logged("save_intake")
    def save_intake(self, data: dict, *, token: str, actor: str) -> str:
        """Atomically register a person, first assignment, checklist and audit."""
        data = validate_intake(data)
        token, actor = required(token, "저장 요청 ID"), required(actor, "변경 주체")
        fingerprint = self._fingerprint(["intake", data, actor])
        try:
            with connection(self.path) as con:
                con.execute("BEGIN IMMEDIATE")
                previous = self._previous_request(con, token, fingerprint)
                if previous is not None:
                    return previous
                person_id = self._write(con, "people", data["person"], actor, None)
                self._write(con, "assignments", dict(data["assignment"], person_id=person_id), actor, None)
                con.execute("INSERT INTO requests VALUES (?,?,?)", (token, fingerprint, person_id))
                return person_id
        except sqlite3.IntegrityError as exc:
            raise ValidationError("인력과 투입 정보 저장에 실패했습니다. 연결 관계를 확인하세요.") from exc

    @staticmethod
    def _fingerprint(value: object) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True
        ).encode()).hexdigest()

    @staticmethod
    def _previous_request(con, token, fingerprint) -> str | None:
        existing = con.execute("SELECT * FROM requests WHERE token=?", (token,)).fetchone()
        if existing:
            if existing["payload_hash"] != fingerprint:
                raise ConflictError("같은 저장 요청 ID에 다른 내용이 있습니다. 다시 미리보기를 만드세요.")
            return existing["result_id"]
        return None

    @staticmethod
    def _write(con, table, data, actor, expected_version) -> str:
        entity_id = data.get("id") or str(uuid4())
        data = dict(data, id=entity_id)
        before = con.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        if expected_version is None:
            if before:
                raise ValidationError("이미 등록된 관리번호입니다. 수정 화면을 사용하세요.")
            version = 1
        else:
            if not before or before["version"] != expected_version:
                raise ConflictError("데이터가 변경되었습니다. 최신 내용을 다시 조회하고 확인하세요.")
            version = expected_version + 1
        after = dict(data, version=version)
        if before:
            keys = [k for k in after if k != "id"]
            con.execute(
                f"UPDATE {table} SET " + ",".join(f"{k}=?" for k in keys) + " WHERE id=?",
                tuple(after[k] for k in keys) + (entity_id,)
            )
        else:
            keys = list(after)
            con.execute(
                f"INSERT INTO {table} (" + ",".join(keys) + ") VALUES (" +
                ",".join("?" for _ in keys) + ")", tuple(after[k] for k in keys)
            )
            if table == "assignments":
                con.executemany(
                    "INSERT INTO checklist(assignment_id,item) VALUES (?,?)",
                    [(entity_id, item) for item in ("좌석 배정", "ID카드 신청", "OA기기 신청", "방문예약")]
                )
        Service._audit(con, table, entity_id, dict(before) if before else None, after, actor)
        return entity_id

    @staticmethod
    def _audit(con, entity: str, entity_id: str, before: dict | None, after: dict, actor: str) -> None:
        con.execute(
            """INSERT INTO audit(entity,entity_id,before_json,after_json,changed_at,actor)
               VALUES (?,?,?,?,?,?)""",
            (entity, entity_id, json.dumps(before, ensure_ascii=False) if before else None,
             json.dumps(after, ensure_ascii=False),
             datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), actor)
        )

    @logged("save_assignment")
    def save_assignment(self, data: dict, *, token: str, actor: str,
                        assignment_id: str | None = None,
                        expected_version: int | None = None,
                        overlap_reviewed: bool = False) -> str:
        result = validate_assignment(data)
        # New UUID is allocated only inside the transaction after duplicate-token lookup.
        if assignment_id:
            result["id"] = assignment_id
        return self._save("assignments", result, token, actor, expected_version, overlap_reviewed)

    def _save(self, table: str, data: dict, token: str, actor: str,
              expected_version: int | None, overlap_reviewed: bool = False) -> str:
        token, actor = required(token, "저장 요청 ID"), required(actor, "변경 주체")
        fingerprint = self._fingerprint(
            [table, data, actor, expected_version, overlap_reviewed],
        )
        try:
            with connection(self.path) as con:
                con.execute("BEGIN IMMEDIATE")
                previous = self._previous_request(con, token, fingerprint)
                if previous is not None:
                    return previous
                if table == "assignments":
                    if not con.execute("SELECT 1 FROM people WHERE id=?", (data["person_id"],)).fetchone():
                        raise ValidationError("등록된 인력을 선택하세요.")
                    if self._overlaps(con, data, data.get("id")) and not overlap_reviewed:
                        raise ValidationError("투입 기간 중복을 검토한 뒤 저장하세요.")
                if table == "contracts":
                    self._check_contract_links(con, data)
                entity_id = self._write(con, table, data, actor, expected_version)
                if table == "contracts":
                    # 종료일 변경 시 알림 기준일을 같은 트랜잭션에서 다시 계산한다.
                    self._sync_alerts(con, self.clock())
                con.execute("INSERT INTO requests VALUES (?,?,?)", (token, fingerprint, entity_id))
                return entity_id
        except sqlite3.IntegrityError as exc:
            raise ValidationError("저장할 정보의 중복 또는 연결 관계를 확인하세요.") from exc

    # ------------------------------------------------------------------
    # 실행 기록 (하네스)
    # ------------------------------------------------------------------
    def record_run(self, operation, args, kwargs, ok, result, error, t0, started) -> None:
        try:
            payload = json.dumps({"args": list(args), "kwargs": {
                k: v for k, v in kwargs.items() if k != "token"}},
                ensure_ascii=False, default=str)
            with connection(self.path) as con:
                con.execute(
                    """INSERT INTO run_log(operation,actor,input_json,ok,result,error,duration_ms,started_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (operation, kwargs.get("actor"), payload, int(ok), result, error,
                     round((time.perf_counter() - t0) * 1000), started))
        except sqlite3.Error:
            pass

    def run_log(self, limit: int = 200) -> list[dict]:
        with connection(self.path) as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------
    # 인력·투입 조회
    # ------------------------------------------------------------------
    def find_people(self, name: str) -> list[dict]:
        """이름 일부로 후보를 찾는다. 이름만으로 확정하지 않고 관리번호로 대상을 고른다."""
        keyword = required(name, "이름")
        with connection(self.path) as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM people WHERE name LIKE ? ORDER BY name, id",
                (f"%{keyword}%",))]

    def assignment_timeline(self, person_id: str, today: date | None = None) -> dict:
        """현재 투입·과거 투입·예정 투입을 구분해 돌려준다."""
        today_text = (today or self.clock()).isoformat()
        rows = self.assignments(required(person_id, "내부 관리번호"))
        return {
            "today": today_text,
            "current": [r for r in rows if r["start_date"] <= today_text <= r["end_date"]],
            "past": sorted((r for r in rows if r["end_date"] < today_text),
                           key=lambda r: r["start_date"], reverse=True),
            "upcoming": [r for r in rows if r["start_date"] > today_text],
        }

    # ------------------------------------------------------------------
    # 구매계약
    # ------------------------------------------------------------------
    _CONTRACT_SELECT = """SELECT c.*, p.name, p.category, p.company AS person_company,
        EXISTS(SELECT 1 FROM contracts n WHERE n.previous_contract_id=c.id) AS has_successor
        FROM contracts c JOIN people p ON p.id=c.person_id"""

    def contracts(self, person_id: str | None = None, today: date | None = None) -> list[dict]:
        sql, params = self._CONTRACT_SELECT, ()
        if person_id is not None:
            sql, params = sql + " WHERE c.person_id=?", (person_id,)
        with connection(self.path) as con:
            rows = [dict(r) for r in con.execute(sql + " ORDER BY c.end_date, p.name, c.id", params)]
        return self._with_state(rows, today)

    def contracts_ending(self, year: int, month: int, company: str | None = None,
                         today: date | None = None) -> list[dict]:
        """특정 월(·업체)에 계약이 종료되는 인력 목록."""
        first, last = month_range(int(year), int(month))
        sql = self._CONTRACT_SELECT + " WHERE c.end_date BETWEEN ? AND ?"
        params: tuple = (first, last)
        company = str(company or "").strip()
        if company:
            sql, params = sql + " AND lower(trim(c.company))=lower(?)", params + (company,)
        with connection(self.path) as con:
            rows = [dict(r) for r in con.execute(sql + " ORDER BY c.end_date, p.name", params)]
        return self._with_state(rows, today)

    def _with_state(self, rows: list[dict], today: date | None) -> list[dict]:
        day = today or self.clock()
        for row in rows:
            row.update(expiry_state(row["end_date"], day))
            row["has_successor"] = bool(row["has_successor"])
        return rows

    @logged("save_contract")
    def save_contract(self, data: dict, *, token: str, actor: str,
                      contract_id: str | None = None,
                      expected_version: int | None = None) -> str:
        result = validate_contract(data)
        if contract_id:
            result["id"] = contract_id
        return self._save("contracts", result, token, actor, expected_version)

    @staticmethod
    def _check_contract_links(con, data: dict) -> None:
        person = con.execute("SELECT category FROM people WHERE id=?", (data["person_id"],)).fetchone()
        if not person:
            raise ValidationError("등록된 인력을 선택하세요.")
        if person["category"] not in CONTRACT_CATEGORIES:
            raise ValidationError("구매계약은 협력·컨설팅 인력만 등록할 수 있습니다.")
        if data.get("assignment_id"):
            owner = con.execute("SELECT person_id FROM assignments WHERE id=?",
                                (data["assignment_id"],)).fetchone()
            if not owner or owner["person_id"] != data["person_id"]:
                raise ValidationError("연결할 투입 건은 같은 인력의 투입이어야 합니다.")
        previous_id = data.get("previous_contract_id")
        if previous_id:
            if previous_id == data.get("id"):
                raise ValidationError("자기 자신을 이전 계약으로 지정할 수 없습니다.")
            previous = con.execute("SELECT person_id FROM contracts WHERE id=?", (previous_id,)).fetchone()
            if not previous or previous["person_id"] != data["person_id"]:
                raise ValidationError("이전(연장 전) 계약은 같은 인력의 계약이어야 합니다.")

    # ------------------------------------------------------------------
    # 계약 종료 알림
    # ------------------------------------------------------------------
    @staticmethod
    def _sync_alerts(con, today: date) -> dict:
        """알림 기간(종료 14일 전~)에 들어온 계약의 알림을 한 번만 만든다.

        종료일이 바뀐 계약의 미확인 알림은 '기준일 변경'으로 닫고 새 종료일 기준으로 다시 만든다.
        """
        created = superseded = 0
        for row in con.execute("SELECT id, end_date FROM contracts").fetchall():
            superseded += con.execute(
                """UPDATE contract_alerts SET status='기준일 변경'
                   WHERE contract_id=? AND kind='expiry' AND status='미확인' AND end_date<>?""",
                (row["id"], row["end_date"])).rowcount
            start = notify_from(row["end_date"])
            if today.isoformat() >= start:
                created += con.execute(
                    """INSERT OR IGNORE INTO contract_alerts
                       (contract_id,kind,end_date,notify_from,created_at) VALUES (?,?,?,?,?)""",
                    (row["id"], "expiry", row["end_date"], start,
                     datetime.now(TIMEZONE).isoformat())).rowcount
        return {"created": created, "superseded": superseded}

    def sync_contract_alerts(self, today: date | None = None) -> dict:
        with connection(self.path) as con:
            con.execute("BEGIN IMMEDIATE")
            return self._sync_alerts(con, today or self.clock())

    def pending_alerts(self, today: date | None = None) -> list[dict]:
        """앱 실행 시 호출: 알림을 갱신하고 미확인 만료 대상을 돌려준다."""
        day = today or self.clock()
        self.sync_contract_alerts(day)
        with connection(self.path) as con:
            rows = [dict(r) for r in con.execute(
                """SELECT a.*, c.person_id, c.company, c.contract_no, c.status AS contract_status,
                          c.start_date AS contract_start, p.name, p.category,
                          EXISTS(SELECT 1 FROM contracts n WHERE n.previous_contract_id=c.id) AS has_successor
                   FROM contract_alerts a JOIN contracts c ON c.id=a.contract_id
                   JOIN people p ON p.id=c.person_id
                   WHERE a.status='미확인' ORDER BY a.end_date, p.name""")]
        return self._with_state(rows, day)

    @logged("acknowledge_alert")
    def acknowledge_alert(self, alert_id: int, *, actor: str) -> int:
        """알림 확인만 기록한다. 계약 갱신 완료와는 별도 상태다."""
        actor = required(actor, "변경 주체")
        with connection(self.path) as con:
            con.execute("BEGIN IMMEDIATE")
            before = con.execute("SELECT * FROM contract_alerts WHERE id=?", (alert_id,)).fetchone()
            if not before or before["status"] != "미확인":
                raise ConflictError("이미 처리됐거나 기준일이 바뀐 알림입니다. 목록을 새로 확인하세요.")
            now = datetime.now(TIMEZONE).isoformat()
            con.execute("""UPDATE contract_alerts SET status='확인', acknowledged_at=?, acknowledged_by=?
                           WHERE id=?""", (now, actor, alert_id))
            after = dict(before, status="확인", acknowledged_at=now, acknowledged_by=actor)
            self._audit(con, "contract_alerts", str(alert_id), dict(before), after, actor)
            return int(alert_id)
