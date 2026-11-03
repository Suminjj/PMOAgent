"""v2 데이터 접근 권한 (별도 검증): 로그인 사용자 기준으로 코드·DB에서 막는지 (가상 데이터)."""
import sqlite3

import pytest

from pmo.access import AccessDenied, authenticate, create_user, users
from pmo.agent import text_to_sql
from pmo.db import connection
from pmo.eval_data import DATA, ERP, EVAL_PASSWORD, TODAY, seed_eval
from pmo.service import Service
from pmo.tools import call_tool

ERP_PEOPLE = {"김정직", "이협력", "정협력", "강협력", "윤협력", "서협력"}


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("access") / "eval.db"
    seed_eval(Service(path, clock=lambda: TODAY), TODAY)
    return path


def as_user(db, login):
    access = authenticate(db, login, EVAL_PASSWORD)
    assert access is not None
    return Service(db, clock=lambda: TODAY, access=access)


def test_login_checks_hashed_password(db):
    assert authenticate(db, "leader-erp", "wrong-password") is None
    assert authenticate(db, "nobody", EVAL_PASSWORD) is None
    leader = authenticate(db, "leader-erp", EVAL_PASSWORD)
    assert leader.role == "과제 리더" and leader.projects == {ERP} and not leader.can_write
    with connection(db) as con:
        stored = [r[0] for r in con.execute("SELECT password_hash FROM users")]
    assert EVAL_PASSWORD not in stored and all(len(h) == 64 for h in stored)     # 원문 저장 안 함


def test_pm_and_pmo_see_everything_and_can_write(db):
    for login in ("pmo", "pm"):
        s = as_user(db, login)
        assert len(s.people()) == 16 and s.access.can_write
        assert len(s.history()) > 0


def test_leader_sees_only_own_project(db):
    s = as_user(db, "leader-erp")
    assert {p["name"] for p in s.people()} == ERP_PEOPLE
    assert {a["project"] for a in s.assignments()} == {ERP}
    lee = next(p["id"] for p in s.people() if p["name"] == "이협력")
    timeline = s.assignment_timeline(lee)
    assert timeline["upcoming"] == []                                     # 이협력의 데이터 플랫폼 예정 투입은 안 보임
    assert all(c["name"] in ERP_PEOPLE for c in s.contracts())
    assert all(a["name"] in ERP_PEOPLE for a in s.pending_alerts())
    assert s.find_people("김컨설") == [] and s.find_people("김협력") == []  # 다른 과제 인력은 '없는 사람'
    assert {r["name"] for r in s.monthly_mm(2026)} <= ERP_PEOPLE
    assert all(r["name"] in ERP_PEOPLE for r in s.checklist_overview(include_closed=True))
    rates = {a["purchase_rate"] for a in s.assignments()}
    assert "9500000" in rates                                             # 담당 과제 단가는 볼 수 있음


def test_leader_cannot_open_other_project_records(db):
    s, full = as_user(db, "leader-erp"), Service(db, clock=lambda: TODAY)
    other = next(a["id"] for a in full.assignments() if a["project"] == DATA)
    with pytest.raises(AccessDenied):
        s.checklist(other)
    with pytest.raises(AccessDenied):
        s.history()
    with pytest.raises(AccessDenied):
        s.run_log()


def test_leader_cannot_write(db):
    s = as_user(db, "leader-erp")
    person = s.people()[0]
    with pytest.raises(AccessDenied, match="수정 요청"):
        s.save_person(dict(person, name="바꿈"), token="t-leader", actor="리더", expected_version=person["version"])
    alert = s.pending_alerts()[0]
    with pytest.raises(AccessDenied):
        s.acknowledge_alert(alert["id"], actor="리더")
    with pytest.raises(AccessDenied):
        create_user(db, login="x", name="x", role="PM", password="password1", actor=s.access)
    with pytest.raises(AccessDenied):
        users(db, s.access)


def test_agent_tools_follow_login_not_the_question(db):
    """질문에 '나는 데이터 플랫폼 리더야'가 있어도 도구는 로그인한 ERP 리더 권한으로만 조회한다."""
    s = as_user(db, "leader-erp")
    result = call_tool(s, "get_assignment_timeline", {"person_id": "김컨설"})
    assert not result["ok"] and "없습니다" in result["error"]
    result = call_tool(s, "contracts_ending", {"year": 2026, "month": 11})
    assert {r["name"] for r in result["data"]} <= ERP_PEOPLE
    plan = call_tool(s, "prepare_checklist_update", {"person_id": "서협력", "item": "ID카드 신청", "status": "진행 중"})
    assert not plan["ok"] and "수정 요청" in plan["error"]                # 확인 카드도 만들지 않음


def test_text_to_sql_runs_on_rows_the_leader_may_see(db, monkeypatch):
    class Fake:
        def __init__(self, path, today):
            pass

        def generate(self, question):                                    # WHERE 없이 전체를 묻는 SQL
            return "SELECT name, purchase_rate FROM people JOIN assignments a ON a.person_id = people.id"

    monkeypatch.setattr(text_to_sql, "SQL_GENERATOR_FACTORY", Fake)
    leader = call_tool(as_user(db, "leader-erp"), "query_database", {"question": "전체 인력과 단가"})
    assert leader["ok"] and {r[0] if isinstance(r, list) else r["name"] for r in leader["data"]["rows"]} <= ERP_PEOPLE
    pmo = call_tool(as_user(db, "pmo"), "query_database", {"question": "전체 인력과 단가"})
    assert len(pmo["data"]["rows"]) > len(leader["data"]["rows"])


def test_change_requests(db):
    leader, pm = as_user(db, "leader-erp"), as_user(db, "pm")
    rid = leader.create_change_request(project=ERP, content="서협력 투입 시작일 11/30으로 변경 요청",
                                       person_id=next(p["id"] for p in leader.people() if p["name"] == "서협력"))
    with pytest.raises(AccessDenied):
        leader.create_change_request(project=DATA, content="다른 과제 요청")
    assert [r["id"] for r in leader.change_requests()] == [rid]
    assert as_user(db, "leader-data").change_requests() == []             # 다른 리더의 요청은 안 보임
    with pytest.raises(AccessDenied):
        leader.handle_change_request(rid, "처리 완료")
    pm.handle_change_request(rid, "처리 완료", "반영함")
    assert leader.change_requests()[0]["status"] == "처리 완료"


def test_first_user_must_be_pmo_and_leader_needs_project(tmp_path):
    path = tmp_path / "users.db"
    Service(path)
    with pytest.raises(Exception, match="첫 사용자"):
        create_user(path, login="a", name="a", role="PM", password="password1")
    create_user(path, login="admin", name="관리자", role="PMO", password="password1")
    admin = authenticate(path, "admin", "password1")
    with pytest.raises(Exception, match="담당 과제"):
        create_user(path, login="b", name="b", role="과제 리더", password="password1", actor=admin)
    with pytest.raises(Exception, match="8자"):
        create_user(path, login="c", name="c", role="PM", password="short", actor=admin)


def test_v6_database_gets_user_tables_with_backup(tmp_path):
    path = tmp_path / "v6.db"
    Service(path)
    with sqlite3.connect(path) as con:
        con.executescript("DROP TABLE change_requests; DROP TABLE user_projects; DROP TABLE users;"
                          "PRAGMA user_version = 6;")
    Service(path)
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 7
        assert con.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    assert len(list(tmp_path.glob("v6.db.backup-v6-*.db"))) == 1


def test_leader_menu_hides_write_pages():
    pytest.importorskip("streamlit")
    from ui.theme import pages_for
    leader, pmo = pages_for("과제 리더"), pages_for("PMO")
    assert {"신규 인력 등록", "계약 등록·수정", "변경 이력", "사용자 관리"}.isdisjoint(leader)
    assert "수정 요청" in leader and "사용자 관리" in pmo and len(pmo) == len(pages_for("PM"))


def test_leader_app_login_shows_only_own_project(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    from ui.theme import PAGES
    path = tmp_path / "ui-access.db"
    seed_eval(Service(path, clock=lambda: TODAY), TODAY)
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    monkeypatch.setenv("PMO_TODAY", TODAY.isoformat())
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30)
    app.session_state["user_id"] = authenticate(path, "leader-erp", EVAL_PASSWORD).user_id
    app.run()
    assert not app.exception
    with pytest.raises(KeyError):
        app.button(key=f"nav_{PAGES.index('사용자 관리')}")                # 메뉴에 없음
    app.button(key=f"nav_{PAGES.index('전체 인력 조회')}").click().run()
    assert set(app.dataframe[0].value["이름"]) == ERP_PEOPLE


def test_app_without_login_shows_login_form(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    path = tmp_path / "ui-login.db"
    seed_eval(Service(path, clock=lambda: TODAY), TODAY)
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30).run()
    assert not app.exception and not app.dataframe                        # 로그인 전에는 데이터가 없음
    assert any(t.label == "비밀번호" for t in app.text_input)
