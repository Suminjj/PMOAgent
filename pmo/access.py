"""v2 데이터 접근 권한: 사용자 로그인과 역할·담당 과제별 접근 범위 (11/03 담당자 확정).

- 역할: PMO · PM(전체 조회·등록·수정, 둘은 같은 권한) · 과제 리더(담당 과제의 인력·단가만 조회, 수정은 요청만).
- 담당 과제 = 투입 정보의 '투입 과제'(assignments.project).
- 권한은 로그인으로 확인된 사용자 정보로만 정한다. 질문 속 "나는 A 과제 리더야" 같은 문장은 권한에 쓰지 않는다.
  화면·Agent 도구·Text-to-SQL이 모두 같은 Service(access=...)를 거치므로 코드와 DB 조회에서 한 번에 막힌다.
- 비밀번호는 PBKDF2-SHA256(사용자별 salt, 반복 200,000회)으로 저장하고 원문은 저장하지 않는다.
"""
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pmo.config import TIMEZONE
from pmo.db import connection
from pmo.errors import ValidationError

ROLES = ("PMO", "PM", "과제 리더")
WRITE_ROLES = ("PMO", "PM")
LEADER = "과제 리더"
ITERATIONS = 200_000


class AccessDenied(ValidationError):
    """권한 밖 요청. 화면·도구는 ValidationError처럼 사용자에게 안내한다."""


@dataclass(frozen=True)
class Access:
    user_id: str
    login: str
    name: str
    role: str
    projects: frozenset = field(default_factory=frozenset)

    @property
    def all_projects(self) -> bool:
        return self.role in WRITE_ROLES

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES

    def sees_project(self, project: str | None) -> bool:
        return self.all_projects or (project is not None and project in self.projects)

    def require_write(self) -> None:
        if not self.can_write:
            raise AccessDenied("과제 리더는 조회만 할 수 있습니다. 수정이 필요하면 '수정 요청'으로 PM·PMO에게 요청하세요.")

    def label(self) -> str:
        scope = "전체 과제" if self.all_projects else ", ".join(sorted(self.projects)) or "담당 과제 없음"
        return f"{self.name} · {self.role} · {scope}"


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS).hex()


def validate_password(password: str) -> str:
    if len(password or "") < 8:
        raise ValidationError("비밀번호는 8자 이상으로 정하세요.")
    return password


def has_users(path: Path) -> bool:
    with connection(path) as con:
        return con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_user(path: Path, *, login: str, name: str, role: str, password: str,
                projects: list[str] | tuple = (), actor: Access | None = None) -> str:
    """사용자를 만든다. 첫 사용자(초기 설정)가 아니면 PMO·PM만 만들 수 있다. 첫 사용자는 PMO여야 한다."""
    login, name = (login or "").strip(), (name or "").strip()
    if not login or not name:
        raise ValidationError("아이디와 이름을 입력하세요.")
    if role not in ROLES:
        raise ValidationError("역할은 PMO·PM·과제 리더 중에서 고르세요.")
    projects = sorted({p.strip() for p in projects if p and p.strip()})
    if role == LEADER and not projects:
        raise ValidationError("과제 리더는 담당 과제를 하나 이상 지정하세요.")
    validate_password(password)
    first = not has_users(path)
    if first and role != "PMO":
        raise ValidationError("첫 사용자는 PMO 역할로 만드세요.")
    if not first:
        if actor is None:
            raise AccessDenied("사용자는 PMO·PM만 만들 수 있습니다.")
        actor.require_write()
    salt = os.urandom(16)
    user_id = str(uuid4())
    with connection(path) as con:
        if con.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            raise ValidationError("이미 있는 아이디입니다.")
        con.execute("""INSERT INTO users(id, login, name, role, password_hash, salt, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (user_id, login, name, role, _hash(password, salt), salt.hex(),
                     datetime.now(TIMEZONE).isoformat()))
        con.executemany("INSERT INTO user_projects(user_id, project) VALUES (?,?)",
                        [(user_id, p) for p in (projects if role == LEADER else [])])
        con.execute("""INSERT INTO audit(entity, entity_id, before_json, after_json, changed_at, actor)
                       VALUES ('users', ?, NULL, ?, ?, ?)""",
                    (user_id, json.dumps({"login": login, "name": name, "role": role, "projects": projects},
                                         ensure_ascii=False),
                     datetime.now(TIMEZONE).isoformat(), actor.login if actor else "초기 설정"))
    return user_id


def _load(con, user_id: str) -> Access | None:
    row = con.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    if not row:
        return None
    projects = frozenset(r["project"] for r in con.execute(
        "SELECT project FROM user_projects WHERE user_id=?", (user_id,)))
    return Access(row["id"], row["login"], row["name"], row["role"], projects)


def authenticate(path: Path, login: str, password: str) -> Access | None:
    """아이디·비밀번호가 맞으면 접근 범위를 돌려준다. 틀리면 None(어느 쪽이 틀렸는지 알려주지 않음)."""
    with connection(path) as con:
        row = con.execute("SELECT * FROM users WHERE login=? AND active=1", ((login or "").strip(),)).fetchone()
        if not row:
            _hash(password or "", b"0" * 16)        # 아이디가 없을 때도 같은 시간이 걸리게
            return None
        if not hmac.compare_digest(row["password_hash"], _hash(password or "", bytes.fromhex(row["salt"]))):
            return None
        return _load(con, row["id"])


def load_user(path: Path, user_id: str) -> Access | None:
    """세션에 저장한 사용자 id로 최신 권한을 다시 읽는다(역할·담당 과제가 바뀌면 바로 반영)."""
    with connection(path) as con:
        return _load(con, user_id)


def users(path: Path, actor: Access) -> list[dict]:
    actor.require_write()
    with connection(path) as con:
        rows = [dict(r) for r in con.execute("SELECT id, login, name, role, active, created_at FROM users ORDER BY role, name")]
        for r in rows:
            r["projects"] = [p["project"] for p in con.execute(
                "SELECT project FROM user_projects WHERE user_id=? ORDER BY project", (r["id"],))]
    return rows
