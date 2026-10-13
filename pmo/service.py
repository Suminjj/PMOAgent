"""Validated business operations shared by forms and future LLM tools."""
import hashlib
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from pmo.config import grades
from pmo.db import connection, initialize


class ValidationError(ValueError):
    pass


class ConflictError(ValueError):
    pass


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
    result["rate_unit"] = str(data.get("rate_unit") or "").strip() or None
    for key, label in (("sales_rate", "매출단가"), ("purchase_rate", "매입단가")):
        value = data.get(key)
        result[key] = None if value is None or str(value).strip() == "" else money(str(value), label)
    if any(result[k] is not None for k in ("sales_rate", "purchase_rate")) and not result["rate_unit"]:
        raise ValidationError("단가를 입력하면 단가 기준 단위도 입력하세요.")
    return result


def validate_intake(data: dict) -> dict:
    """Validate the information known before a company account is issued."""
    person = validate_person(data["person"])
    person.pop("id", None)
    person["company"] = required(person["company"], "업체명")
    assignment = validate_assignment(dict(data["assignment"], person_id="new-person"))
    assignment.pop("person_id")
    return {"person": person, "assignment": assignment}


class Service:
    def __init__(self, path: Path):
        self.path = Path(path)
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

    def save_person(self, data: dict, *, token: str, actor: str,
                    expected_version: int | None = None) -> str:
        return self._save("people", validate_person(data), token, actor, expected_version)

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
        con.execute(
            """INSERT INTO audit(entity,entity_id,before_json,after_json,changed_at,actor)
               VALUES (?,?,?,?,?,?)""",
            (table, entity_id, json.dumps(dict(before), ensure_ascii=False) if before else None,
             json.dumps(after, ensure_ascii=False),
             datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), actor)
        )
        return entity_id

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
                entity_id = self._write(con, table, data, actor, expected_version)
                con.execute("INSERT INTO requests VALUES (?,?,?)", (token, fingerprint, entity_id))
                return entity_id
        except sqlite3.IntegrityError as exc:
            raise ValidationError("저장할 정보의 중복 또는 연결 관계를 확인하세요.") from exc
