"""Validated business operations shared by forms and future LLM tools."""
import contextlib
import functools
import hashlib
import json
import sqlite3
import tempfile
import time
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from pmo.checklist import CLOSED, ITEMS as CHECKLIST_ITEMS, make_draft, validate_change
from pmo.config import TIMEZONE, grades, today as kst_today
from pmo.contracts import (
    CONTRACT_CATEGORIES, expiry_state, month_range, notify_from, validate_contract,
)
from pmo.db import connection, initialize
from pmo.access import AccessDenied
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
    result["job_role"] = str(data.get("job_role") or "").strip() or None
    if result["category"] not in ("정", "협력", "컨설팅"):
        raise ValidationError("분류는 정·협력·컨설팅 중 선택하세요.")
    return result


def validate_assignment(data: dict) -> dict:
    result = {"person_id": required(data.get("person_id"), "내부 관리번호")}
    for key, label in (("start_date", "시작일"), ("end_date", "종료일")):
        try:
            result[key] = date.fromisoformat(str(data.get(key))).isoformat()
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{label}을(를) 올바르게 입력하세요.") from exc
    if result["end_date"] < result["start_date"]:
        raise ValidationError("종료일은 시작일보다 빠를 수 없습니다.")
    # 등급은 분류에 따라 필수(정·협력) 또는 선택(컨설팅). 분류 검사는 저장 시 check_grade가 한다.
    result["grade"] = str(data.get("grade") or "").strip() or None
    if result["grade"] is not None and result["grade"] not in grades():
        raise ValidationError("허용된 등급을 선택하세요.")
    result["project"] = str(data.get("project") or "").strip() or None
    # 투입 과제와 매출귀속 프로젝트는 다를 수 있다. 배분 규칙은 TODO.
    result["revenue_project"] = str(data.get("revenue_project") or "").strip() or None
    result["revenue_team"] = str(data.get("revenue_team") or "").strip() or None
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


GRADE_OPTIONAL = ("컨설팅",)   # 등급 체계(L7·L6·L5)가 없는 분류 (10/20 담당자 확인)


def check_grade(category: str | None, grade: str | None) -> None:
    if grade is None and category not in GRADE_OPTIONAL:
        raise ValidationError("등급을 입력하세요. 등급은 컨설팅 인력만 비워 둘 수 있습니다.")


def validate_intake(data: dict) -> dict:
    """Validate the information known before a company account is issued."""
    person = validate_person(data["person"])
    person.pop("id", None)
    person["company"] = required(person["company"], "업체명")
    assignment = validate_assignment(dict(data["assignment"], person_id="new-person"))
    assignment.pop("person_id")
    check_grade(person["category"], assignment["grade"])
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


def writes(fn):
    """등록·수정·삭제 업무 함수: 로그인 사용자가 PMO·PM이 아니면 실행하지 않는다(v2 권한)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        self._require_write()
        return fn(self, *args, **kwargs)
    return wrapper


class Service:
    def __init__(self, path: Path, clock=None, access=None):
        """clock: 오늘 날짜(Asia/Seoul)를 돌려주는 함수. 테스트에서 날짜를 고정할 때 사용한다.

        access: 로그인한 사용자의 접근 범위(pmo.access.Access). 화면·Agent는 반드시 넘긴다(v2 권한).
        None은 명령어·테스트·평가처럼 사람이 로그인하지 않는 내부 실행용(전체 접근)이다.
        """
        self.path = Path(path)
        self.clock = clock or kst_today
        self.access = access
        initialize(self.path)

    # ------------------------------------------------------------------
    # v2 데이터 접근 권한: 과제 리더는 담당 과제(투입 과제)의 투입과 그 인력·계약만 본다
    # ------------------------------------------------------------------
    def _scoped(self) -> bool:
        return self.access is not None and not self.access.all_projects

    def _require_write(self) -> None:
        if self.access is not None:
            self.access.require_write()

    def _visible(self) -> tuple[set, set]:
        """(볼 수 있는 투입 id, 볼 수 있는 인력 id). 전체 접근이면 호출하지 않는다."""
        with connection(self.path) as con:
            rows = con.execute("SELECT id, person_id, project FROM assignments").fetchall()
        mine = [r for r in rows if self.access.sees_project(r["project"])]
        return {r["id"] for r in mine}, {r["person_id"] for r in mine}

    def _filter_people(self, rows: list[dict], key: str = "id") -> list[dict]:
        if not self._scoped():
            return rows
        _, persons = self._visible()
        return [r for r in rows if r[key] in persons]

    def _filter_contracts(self, rows: list[dict], assignment_key: str = "assignment_id") -> list[dict]:
        """연결 투입이 있으면 그 투입이 담당 과제일 때만, 연결이 없으면 인력이 담당 과제에 있을 때만 보인다."""
        if not self._scoped():
            return rows
        assignments, persons = self._visible()
        def visible(r):
            return r[assignment_key] in assignments if r.get(assignment_key) else r["person_id"] in persons
        return [r for r in rows if visible(r)]

    def require_assignment(self, assignment_id: str) -> None:
        if self._scoped() and assignment_id not in self._visible()[0]:
            raise AccessDenied("조회 권한이 없는 투입입니다.")

    def people(self) -> list[dict]:
        with connection(self.path) as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM people ORDER BY name, id")]
        return self._filter_people(rows)

    @contextlib.contextmanager
    def read_snapshot(self, tables=("people", "assignments", "contracts", "contract_alerts", "checklist")):
        """Text-to-SQL용 DB 경로. 전체 접근이면 원래 DB, 과제 리더면 볼 수 있는 행만 담은 임시 DB.

        LLM이 만든 SQL이 WHERE를 빠뜨려도 권한 밖 행은 아예 없는 DB에서 실행되므로 보이지 않는다(행 단위 권한).
        """
        if not self._scoped():
            yield self.path
            return
        assignments, persons = self._visible()
        with connection(self.path) as con:
            schema = {r["name"]: r["sql"] for r in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'") if r["name"] in tables}
            data = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t}")] for t in schema}
        contracts = {c["id"] for c in self._filter_contracts(data.get("contracts", []))}
        keep = {"people": lambda r: r["id"] in persons,
                "assignments": lambda r: r["id"] in assignments,
                "contracts": lambda r: r["id"] in contracts,
                "contract_alerts": lambda r: r["contract_id"] in contracts,
                "checklist": lambda r: r["assignment_id"] in assignments}
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "scoped.db"
            with contextlib.closing(sqlite3.connect(snapshot)) as out:
                for table, ddl in schema.items():
                    out.execute(ddl)
                    rows = [r for r in data[table] if keep[table](r)]
                    if rows:
                        cols = list(rows[0])
                        out.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                        [tuple(r[c] for c in cols) for r in rows])
                out.commit()
            yield snapshot

    # ------------------------------------------------------------------
    # 과제 리더의 수정 요청 (리더는 조회만, 수정은 PM·PMO에게 요청)
    # ------------------------------------------------------------------
    REQUEST_STATUSES = ("요청", "처리 완료", "반려")

    def create_change_request(self, *, project: str, content: str, person_id: str | None = None) -> int:
        """로그인 사용자가 수정 요청을 남긴다. 과제 리더는 담당 과제에 대해서만."""
        if self.access is None:
            raise AccessDenied("로그인한 사용자만 수정 요청을 남길 수 있습니다.")
        project, content = required(project, "과제"), required(content, "요청 내용")
        if not self.access.sees_project(project):
            raise AccessDenied("담당 과제에 대해서만 수정 요청을 남길 수 있습니다.")
        if person_id and person_id not in {r["id"] for r in self.people()}:
            raise AccessDenied("조회 권한이 없는 인력입니다.")
        with connection(self.path) as con:
            cur = con.execute("""INSERT INTO change_requests(requester_id, requester_name, person_id, project,
                                 content, created_at) VALUES (?,?,?,?,?,?)""",
                              (self.access.user_id, self.access.name, person_id or None, project, content,
                               datetime.now(TIMEZONE).isoformat()))
            return int(cur.lastrowid)

    def change_requests(self, status: str | None = None) -> list[dict]:
        """PM·PMO는 전체, 과제 리더는 자기가 남긴 요청만."""
        sql = """SELECT r.*, p.name AS person_name FROM change_requests r
                 LEFT JOIN people p ON p.id=r.person_id"""
        where, params = [], []
        if self.access is not None and not self.access.can_write:
            where.append("r.requester_id=?")
            params.append(self.access.user_id)
        if status:
            where.append("r.status=?")
            params.append(status)
        sql += (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.status='요청' DESC, r.id DESC"
        with connection(self.path) as con:
            return [dict(r) for r in con.execute(sql, params)]

    @writes
    def handle_change_request(self, request_id: int, status: str, note: str = "") -> None:
        if status not in ("처리 완료", "반려"):
            raise ValidationError("처리 결과는 '처리 완료' 또는 '반려'입니다.")
        handler = self.access.name if self.access else "내부"
        with connection(self.path) as con:
            done = con.execute("""UPDATE change_requests SET status=?, note=?, handled_by=?, handled_at=?
                                  WHERE id=? AND status='요청'""",
                               (status, str(note or "").strip(), handler, datetime.now(TIMEZONE).isoformat(),
                                int(request_id))).rowcount
        if not done:
            raise ConflictError("이미 처리된 요청입니다. 목록을 새로 확인하세요.")

    def assignments(self, person_id: str | None = None) -> list[dict]:
        sql = """SELECT a.*, p.name, p.company, p.category FROM assignments a
                 JOIN people p ON p.id=a.person_id"""
        params = ()
        if person_id is not None:
            sql += " WHERE a.person_id=?"
            params = (person_id,)
        with connection(self.path) as con:
            rows = [dict(r) for r in con.execute(sql + " ORDER BY a.start_date, a.id", params)]
        return [r for r in rows if self.access.sees_project(r["project"])] if self._scoped() else rows

    def history(self) -> list[dict]:
        if self._scoped():
            raise AccessDenied("변경 이력은 PM·PMO만 볼 수 있습니다.")
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
    @writes
    def save_person(self, data: dict, *, token: str, actor: str,
                    expected_version: int | None = None) -> str:
        return self._save("people", validate_person(data), token, actor, expected_version)

    @logged("save_intake")
    @writes
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
                    [(entity_id, item) for item in CHECKLIST_ITEMS]
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
    @writes
    def save_assignment(self, data: dict, *, token: str, actor: str,
                        assignment_id: str | None = None,
                        expected_version: int | None = None,
                        overlap_reviewed: bool = False) -> str:
        result = validate_assignment(data)
        # New UUID is allocated only inside the transaction after duplicate-token lookup.
        if assignment_id:
            result["id"] = assignment_id
        return self._save("assignments", result, token, actor, expected_version, overlap_reviewed)

    @logged("apply_assignment_changes")
    @writes
    def apply_assignment_changes(self, changes: list[dict], *, token: str, actor: str) -> list[str]:
        """투입·계약 여러 건을 한 트랜잭션으로 저장한다(전부 또는 없음). Agent의 교체·이동·투입 나누기용.

        changes: 순서대로 실행할 변경. 각 항목은
          {"table": "assignments"|"contracts", "data": 저장할 값, "expected_version": 수정이면 현재 버전,
           "checklist_from": (새 투입만) 근무 환경 준비 상태를 복사할 기존 투입 id}
        새 행은 data["id"]를 미리 정해 둘 수 있다(같은 묶음의 계약이 새 투입을 가리킬 때).
        기간을 줄이는 수정을 새 투입보다 먼저 두면 기간 중복 검사가 저장 순서대로 맞게 동작한다.
        """
        token, actor = required(token, "저장 요청 ID"), required(actor, "변경 주체")
        if not changes:
            raise ValidationError("저장할 변경이 없습니다.")
        prepared = []
        for change in changes:
            table, raw = change.get("table"), dict(change.get("data") or {})
            if table == "assignments":
                data = validate_assignment(raw)
            elif table == "contracts":
                data = validate_contract(raw)
            else:
                raise ValidationError("투입·계약만 함께 저장할 수 있습니다.")
            if raw.get("id"):
                data["id"] = raw["id"]
            prepared.append((table, data, change.get("expected_version"), change.get("checklist_from")))
        fingerprint = self._fingerprint(["assignment_changes", prepared, actor])
        try:
            with connection(self.path) as con:
                con.execute("BEGIN IMMEDIATE")
                previous = self._previous_request(con, token, fingerprint)
                if previous is not None:
                    return json.loads(previous)
                ids = []
                for table, data, expected_version, checklist_from in prepared:
                    if table == "assignments":
                        person = con.execute("SELECT category FROM people WHERE id=?",
                                             (data["person_id"],)).fetchone()
                        if not person:
                            raise ValidationError("등록된 인력을 선택하세요.")
                        check_grade(person["category"], data.get("grade"))
                        if self._overlaps(con, data, data.get("id")):
                            raise ValidationError("투입 기간이 같은 인력의 다른 투입과 겹칩니다. 메뉴 화면에서 검토하세요.")
                    else:
                        self._check_contract_links(con, data)
                    entity_id = self._write(con, table, data, actor, expected_version)
                    if table == "assignments" and checklist_from and expected_version is None:
                        self._copy_checklist(con, checklist_from, entity_id, actor)
                    ids.append(entity_id)
                self._sync_alerts(con, self.clock())
                con.execute("INSERT INTO requests VALUES (?,?,?)", (token, fingerprint, json.dumps(ids)))
                return ids
        except sqlite3.IntegrityError as exc:
            raise ValidationError("저장할 정보의 중복 또는 연결 관계를 확인하세요.") from exc

    @staticmethod
    def _copy_checklist(con, source_id: str, target_id: str, actor: str) -> None:
        """같은 사람의 투입을 나누거나 과제를 옮길 때 근무 환경 준비 상태를 새 투입에 이어 쓴다."""
        now = datetime.now(TIMEZONE).isoformat()
        for row in con.execute("SELECT item, status, reason, applied_at FROM checklist WHERE assignment_id=?",
                               (source_id,)).fetchall():
            con.execute("""UPDATE checklist SET status=?, reason=?, applied_at=?, updated_at=?, updated_by=?
                           WHERE assignment_id=? AND item=?""",
                        (row["status"], row["reason"], row["applied_at"], now, actor, target_id, row["item"]))

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
                    person = con.execute("SELECT category FROM people WHERE id=?", (data["person_id"],)).fetchone()
                    if not person:
                        raise ValidationError("등록된 인력을 선택하세요.")
                    check_grade(person["category"], data.get("grade"))
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
        if self._scoped():
            raise AccessDenied("실행 기록은 PM·PMO만 볼 수 있습니다.")
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
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM people WHERE name LIKE ? ORDER BY name, id",
                (f"%{keyword}%",))]
        return self._filter_people(rows)          # 권한 밖 인력은 '없는 사람'처럼 보인다(존재도 알리지 않음)

    @staticmethod
    def fiscal_year_of(day: date) -> int:
        """회계연도: 4월~다음 해 3월. 2026-10-20 → 2026, 2027-02-01 → 2026."""
        return day.year if day.month >= 4 else day.year - 1

    @staticmethod
    def fiscal_months(fiscal_year: int) -> list[str]:
        return [f"{fiscal_year + (m < 4)}-{m:02d}" for m in (4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3)]

    def monthly_mm(self, fiscal_year: int) -> list[dict]:
        """회계연도(4월~다음 해 3월)에 걸친 투입별 월별 MM.

        Full time은 구매시스템 방식(pmo/mm.py)으로 달마다 (투입 근무일 ÷ 그 달 근무일)을 구해 셋째 자리 반올림해 보여준다.
        투입 전체 MM(합계를 한 번 반올림)과 월별 값의 합은 0.001 차이가 날 수 있다.
        Part time은 기간 전체 MM만 있으므로 월별 값을 만들지 않는다(None). 계산 실패도 0이 아니라 None.
        """
        months = self.fiscal_months(fiscal_year)
        first, last = f"{months[0]}-01", f"{months[-1]}-31"
        people = {p["id"]: p for p in self.people()}
        rows = []
        for a in self.assignments():
            if a["end_date"] < first or a["start_date"] > last:
                continue
            values, note = dict.fromkeys(months), ""
            if a["assignment_type"] == "Full time":
                try:
                    detail = calculate_full_time(date.fromisoformat(a["start_date"]), date.fromisoformat(a["end_date"]))
                    for m in detail["months"]:
                        if m["month"] in values and m["assigned_days"]:
                            values[m["month"]] = format(Decimal(m["mm"]).quantize(
                                Decimal("0.001"), rounding=ROUND_HALF_UP), "f")
                except ValueError as exc:
                    note = f"계산 실패: {exc}"
            else:
                note = "Part time은 기간 전체 MM만 입력되어 월별 값이 없습니다."
            person = people.get(a["person_id"], {})
            rows.append({"name": person.get("name"), "company": person.get("company"),
                         "category": person.get("category"), "project": a["project"],
                         "start_date": a["start_date"], "end_date": a["end_date"],
                         "assignment_type": a["assignment_type"], "mm": a["mm"],
                         "months": values, "note": note, "assignment_id": a["id"]})
        return sorted(rows, key=lambda r: (r["company"] or "", r["name"] or "", r["start_date"]))

    def inspection_contracts(self) -> list[dict]:
        """검수 화면의 업체·계약번호 선택 목록. 계약번호가 있는 계약만, 같은 번호는 한 줄(인원 수·전체 기간)."""
        groups: dict[tuple[str, str], dict] = {}
        for c in self.contracts():
            if not c.get("contract_no"):
                continue
            key = (c["company"].strip(), c["contract_no"].strip())
            g = groups.setdefault(key, {"company": key[0], "contract_no": key[1], "start_date": c["start_date"],
                                        "end_date": c["end_date"], "people": set()})
            g["start_date"], g["end_date"] = min(g["start_date"], c["start_date"]), max(g["end_date"], c["end_date"])
            g["people"].add(c["person_id"])
        return sorted((dict(g, people=len(g["people"])) for g in groups.values()),
                      key=lambda g: (g["company"], g["contract_no"]))

    def inspection(self, company: str, contract_no: str, start_month: str, end_month: str) -> dict:
        """업체·계약번호의 매출귀속 프로젝트별 검수 금액 (pmo/inspection.py 규칙).

        계약 총액 = 계약 기간 전체, 검수 금액 = 조회 기간(start_month~end_month, 'YYYY-MM').
        """
        from pmo.contracts import month_range
        from pmo.inspection import RULE, by_project, line_items
        company, contract_no = required(company, "업체명"), required(contract_no, "계약번호")
        rows = [c for c in self.contracts() if c.get("contract_no") and c["contract_no"].strip() == contract_no
                and c["company"].strip().casefold() == company.casefold()]
        if not rows:
            raise ValidationError(f"{company}의 계약번호 {contract_no} 계약이 없습니다.")
        try:
            first = month_range(int(start_month[:4]), int(start_month[5:7]))[0]
            last = month_range(int(end_month[:4]), int(end_month[5:7]))[1]
        except (TypeError, ValueError) as exc:
            raise ValidationError("조회 기간을 'YYYY-MM' 형식으로 입력하세요.") from exc
        if first > last:
            raise ValidationError("조회 시작 월이 종료 월보다 늦습니다.")
        assignments = self.assignments()
        contract_period = (min(r["start_date"] for r in rows), max(r["end_date"] for r in rows))
        contract_lines = line_items(rows, assignments, contract_period)
        period_lines = line_items(rows, assignments, (first, last))
        return {"company": company, "contract_no": contract_no, "contract_period": contract_period,
                "period": (first, last), "people": len({r["person_id"] for r in rows}),
                "contract": dict(by_project(contract_lines), lines=contract_lines),
                "selected": dict(by_project(period_lines), lines=period_lines), "rule": RULE}

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
        return self._with_state(self._filter_contracts(rows), today)

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
        return self._with_state(self._filter_contracts(rows), today)

    def _with_state(self, rows: list[dict], today: date | None) -> list[dict]:
        day = today or self.clock()
        for row in rows:
            row.update(expiry_state(row["end_date"], day))
            row["has_successor"] = bool(row["has_successor"])
            # 계약 종료 예정 = 종료 14일 전 ~ 종료일 당일, 연장 계약 미등록 (전체 계약 조회의 붉은 줄·필터)
            row["ending_soon"] = row["in_window"] and row["days_left"] >= 0 and not row["has_successor"]
        return rows

    @logged("save_contract")
    @writes
    def save_contract(self, data: dict, *, token: str, actor: str,
                      contract_id: str | None = None,
                      expected_version: int | None = None) -> str:
        result = validate_contract(data)
        if contract_id:
            result["id"] = contract_id
        return self._save("contracts", result, token, actor, expected_version)

    def imported_before(self, file_hash: str) -> dict | None:
        """같은 파일(내용 해시)을 이미 가져왔으면 그때 결과(건수)를 돌려준다. 가져오기 토큰은 import:<해시>."""
        with connection(self.path) as con:
            row = con.execute("SELECT result_id FROM requests WHERE token=?", (f"import:{file_hash}",)).fetchone()
        return json.loads(row["result_id"]) if row else None

    @writes
    def import_batch(self, plan: dict, *, token: str, actor: str) -> dict:
        """실행 기록에는 파일 내용 대신 건수만 남긴다(_import_batch 참고)."""
        started, t0 = datetime.now(TIMEZONE).isoformat(), time.perf_counter()
        summary = {"file_hash": plan.get("file_hash", "")[:12], **plan.get("summary", {}), "actor": actor}
        try:
            result = self._import_batch(plan, token=token, actor=actor)
        except Exception as exc:
            self.record_run("import_batch", (), summary, False, None, f"{type(exc).__name__}: {exc}", t0, started)
            raise
        self.record_run("import_batch", (), summary, True, _summary(result), None, t0, started)
        return result

    def _import_batch(self, plan: dict, *, token: str, actor: str) -> dict:
        """엑셀 가져오기 계획(pmo/excel_io.build_import)을 한 트랜잭션으로 저장한다.

        전부 저장되거나 하나도 저장되지 않는다. 같은 토큰으로 다시 실행해도 중복 저장하지 않는다.
        계획에 오류가 있으면 저장하지 않는다. 투입 기간 중복은 미리보기에서 경고로 보여준 뒤 확인한 것으로 본다.
        """
        if plan.get("errors"):
            raise ValidationError("오류가 있는 파일은 가져올 수 없습니다. 오류를 고친 뒤 다시 올리세요.")
        token, actor = required(token, "저장 요청 ID"), required(actor, "변경 주체")
        fingerprint = self._fingerprint(["import", plan["file_hash"], plan["summary"], actor])
        try:
            with connection(self.path) as con:
                con.execute("BEGIN IMMEDIATE")
                previous = self._previous_request(con, token, fingerprint)
                if previous is not None:
                    return json.loads(previous)
                ids = {}
                for item in plan["people"]:
                    ids[item["key"]] = self._write(con, "people", dict(item["data"]), actor, None)
                person = lambda key: ids.get(key, key)   # noqa: E731 - 새 인력은 새 관리번호, 기존은 그대로
                new_assignments = [
                    self._write(con, "assignments", dict(item["data"], person_id=person(item["key"])), actor, None)
                    for item in plan["assignments"]]
                new_contracts = []

                def ref(value, created):
                    if not value:
                        return None
                    kind, target = value
                    return created[target] if kind == "new" else target

                for item in plan["contracts"]:
                    data = dict(item["data"], person_id=person(item["key"]),
                                assignment_id=ref(item["assignment"], new_assignments),
                                previous_contract_id=ref(item["previous"], new_contracts))
                    self._check_contract_links(con, data)
                    new_contracts.append(self._write(con, "contracts", data, actor, None))
                self._sync_alerts(con, self.clock())
                result = {"인력": len(ids), "투입": len(new_assignments), "구매계약": len(new_contracts)}
                con.execute("INSERT INTO requests VALUES (?,?,?)",
                            (token, fingerprint, json.dumps(result, ensure_ascii=False)))
                return result
        except sqlite3.IntegrityError as exc:
            raise ValidationError("가져오기 저장에 실패했습니다. 중복이나 연결 관계를 확인하세요.") from exc

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
                          c.assignment_id AS contract_assignment_id,
                          c.start_date AS contract_start, p.name, p.category,
                          EXISTS(SELECT 1 FROM contracts n WHERE n.previous_contract_id=c.id) AS has_successor
                   FROM contract_alerts a JOIN contracts c ON c.id=a.contract_id
                   JOIN people p ON p.id=c.person_id
                   WHERE a.status='미확인' ORDER BY a.end_date, p.name""")]
        return self._with_state(self._filter_contracts(rows, "contract_assignment_id"), day)

    @logged("acknowledge_alert")
    @writes
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

    # ------------------------------------------------------------------
    # 근무 환경 준비 체크리스트
    # ------------------------------------------------------------------
    def checklist(self, assignment_id: str) -> list[dict]:
        """투입 건의 4개 항목(방문예약·계정 신청·ID카드 신청·OA기기 신청)을 정해진 순서로 돌려준다.

        예전 데이터에 빠진 항목이 있으면 exists=False인 '미진행'으로 보여주고, 처음 저장할 때 만든다.
        """
        assignment_id = required(assignment_id, "투입 관리번호")
        self.require_assignment(assignment_id)
        with connection(self.path) as con:
            rows = {r["item"]: dict(r) for r in con.execute(
                "SELECT * FROM checklist WHERE assignment_id=?", (assignment_id,))}
        result = []
        for item in CHECKLIST_ITEMS:
            row = rows.get(item) or {"assignment_id": assignment_id, "item": item, "status": "미진행",
                                     "reason": "", "version": 0, "draft": None, "drafted_at": None,
                                     "applied_at": None, "updated_at": None, "updated_by": None}
            result.append(dict(row, exists=item in rows))
        return result

    def checklist_overview(self, include_closed: bool = False, today: date | None = None) -> list[dict]:
        """투입 건별 준비 현황. 기본은 끝나지 않은 투입 중 미처리 항목이 남은 건만, 시작일 순."""
        day = (today or self.clock()).isoformat()
        with connection(self.path) as con:
            assignments = [dict(r) for r in con.execute(
                """SELECT a.id, a.person_id, a.project, a.start_date, a.end_date,
                          p.name, p.company, p.category, p.account_id
                   FROM assignments a JOIN people p ON p.id=a.person_id
                   WHERE a.end_date >= ? ORDER BY a.start_date, p.name""", (day,))]
            rows = {}
            for r in con.execute("SELECT assignment_id, item, status FROM checklist"):
                rows.setdefault(r["assignment_id"], {})[r["item"]] = r["status"]
        if self._scoped():
            assignments = [a for a in assignments if self.access.sees_project(a["project"])]
        result = []
        for a in assignments:
            statuses = {item: rows.get(a["id"], {}).get(item, "미진행") for item in CHECKLIST_ITEMS}
            open_items = [i for i, st in statuses.items() if st not in CLOSED]
            if open_items or include_closed:
                # 남은 항목 중 미진행이 아닌 것은 상태를 붙인다(예: "ID카드 신청(진행 중)").
                open_labels = [i if statuses[i] == "미진행" else f"{i}({statuses[i]})" for i in open_items]
                result.append(dict(a, statuses=statuses, open_items=open_items, open_labels=open_labels,
                                   done=len(CHECKLIST_ITEMS) - len(open_items),
                                   total=len(CHECKLIST_ITEMS),
                                   days_to_start=(date.fromisoformat(a["start_date"])
                                                  - date.fromisoformat(day)).days))
        return result

    @staticmethod
    def _checklist_target(con, assignment_id: str, item: str):
        assignment = con.execute(
            """SELECT a.*, p.name, p.company, p.category, p.account_id FROM assignments a
               JOIN people p ON p.id=a.person_id WHERE a.id=?""", (assignment_id,)).fetchone()
        if not assignment:
            raise ValidationError("등록된 투입 건을 선택하세요.")
        con.execute("INSERT OR IGNORE INTO checklist(assignment_id,item,version) VALUES (?,?,0)",
                    (assignment_id, item))
        row = con.execute("SELECT * FROM checklist WHERE assignment_id=? AND item=?",
                          (assignment_id, item)).fetchone()
        return dict(assignment), dict(row)

    @logged("update_checklist_item")
    @writes
    def update_checklist_item(self, assignment_id: str, item: str, status: str, reason: str = "", *,
                              actual_done: bool = False, token: str, actor: str,
                              expected_version: int) -> str:
        """항목 상태를 바꾼다. 완료는 실제 신청·예약을 마쳤다는 확인(actual_done)이 있어야 한다."""
        change = validate_change(item, status, reason, actual_done)
        assignment_id = required(assignment_id, "투입 관리번호")
        token, actor = required(token, "저장 요청 ID"), required(actor, "변경 주체")
        fingerprint = self._fingerprint(["checklist", assignment_id, item, change, actual_done,
                                         actor, expected_version])
        with connection(self.path) as con:
            con.execute("BEGIN IMMEDIATE")
            previous = self._previous_request(con, token, fingerprint)
            if previous is not None:
                return previous
            _, before = self._checklist_target(con, assignment_id, item)
            if before["version"] != expected_version:
                raise ConflictError("체크리스트가 변경되었습니다. 최신 내용을 다시 조회하고 확인하세요.")
            now = datetime.now(TIMEZONE).isoformat()
            applied_at = (before["applied_at"] or now) if change["status"] == "완료" else None
            after = dict(before, **change, applied_at=applied_at, updated_at=now, updated_by=actor,
                         version=before["version"] + 1)
            con.execute("""UPDATE checklist SET status=?, reason=?, applied_at=?, updated_at=?,
                           updated_by=?, version=? WHERE assignment_id=? AND item=?""",
                        (after["status"], after["reason"], applied_at, now, actor, after["version"],
                         assignment_id, item))
            entity_id = f"{assignment_id}:{item}"
            self._audit(con, "checklist", entity_id, before, after, actor)
            con.execute("INSERT INTO requests VALUES (?,?,?)", (token, fingerprint, entity_id))
            return entity_id

    @logged("create_checklist_draft")
    @writes
    def create_checklist_draft(self, assignment_id: str, item: str, *, actor: str) -> str:
        """신청 초안을 만들어 저장한다. 상태는 바꾸지 않는다(초안 ≠ 실제 신청)."""
        actor = required(actor, "변경 주체")
        with connection(self.path) as con:
            con.execute("BEGIN IMMEDIATE")
            assignment, before = self._checklist_target(con, required(assignment_id, "투입 관리번호"), item)
            draft = make_draft(item, assignment, assignment)
            now = datetime.now(TIMEZONE).isoformat()
            # 초안은 상태와 무관하므로 버전을 올리지 않는다(작성 중인 상태 변경 미리보기를 깨지 않게).
            after = dict(before, draft=draft, drafted_at=now)
            con.execute("UPDATE checklist SET draft=?, drafted_at=? WHERE assignment_id=? AND item=?",
                        (draft, now, assignment_id, item))
            self._audit(con, "checklist", f"{assignment_id}:{item}", before, after, actor)
            return draft

    # ------------------------------------------------------------------
    # 구매계약 삭제 (정해진 업무 함수로만 삭제한다)
    # ------------------------------------------------------------------
    @logged("delete_contract")
    @writes
    def delete_contract(self, contract_id: str, *, token: str, actor: str, expected_version: int) -> str:
        """계약 한 건과 그 알림을 삭제한다. 연장(후속) 계약이 가리키는 계약은 삭제하지 않는다.

        삭제 전 값은 변경 이력(audit)에 남긴다.
        """
        contract_id = required(contract_id, "계약 관리번호")
        token, actor = required(token, "저장 요청 ID"), required(actor, "변경 주체")
        fingerprint = self._fingerprint(["delete_contract", contract_id, actor, expected_version])
        with connection(self.path) as con:
            con.execute("BEGIN IMMEDIATE")
            previous = self._previous_request(con, token, fingerprint)
            if previous is not None:
                return previous
            before = con.execute("SELECT * FROM contracts WHERE id=?", (contract_id,)).fetchone()
            if not before:
                raise ConflictError("이미 삭제됐거나 없는 계약입니다. 목록을 새로 확인하세요.")
            if before["version"] != expected_version:
                raise ConflictError("계약이 변경되었습니다. 최신 내용을 다시 조회하고 확인하세요.")
            if con.execute("SELECT 1 FROM contracts WHERE previous_contract_id=?", (contract_id,)).fetchone():
                raise ValidationError("이 계약을 이전 계약으로 지정한 연장 계약이 있어 삭제할 수 없습니다. "
                                      "연장 계약을 먼저 정리하세요.")
            con.execute("DELETE FROM contract_alerts WHERE contract_id=?", (contract_id,))
            con.execute("DELETE FROM contracts WHERE id=?", (contract_id,))
            self._audit(con, "contracts", contract_id, dict(before), {"deleted": True}, actor)
            con.execute("INSERT INTO requests VALUES (?,?,?)", (token, fingerprint, contract_id))
            return contract_id
