"""화면 테스트 공통: v2 로그인. 앱은 로그인한 사용자만 쓸 수 있으므로 테스트 계정으로 세션을 연다(가상 데이터)."""
from pathlib import Path

TEST_PASSWORD = "test-pass-1234"


def login_app(app, db_path, role="PMO", projects=()):
    """DB에 테스트 계정을 만들고(없으면) AppTest 세션을 그 계정으로 로그인시킨다. 첫 계정은 PMO."""
    from pmo.access import authenticate, create_user, has_users
    from pmo.service import Service
    db_path = Path(db_path)
    Service(db_path)
    login = {"PMO": "test-pmo", "PM": "test-pm"}.get(role, "test-leader")
    if not has_users(db_path):
        create_user(db_path, login="test-pmo", name="테스트 PMO", role="PMO", password=TEST_PASSWORD)
    found = authenticate(db_path, login, TEST_PASSWORD)
    if found is None:
        admin = authenticate(db_path, "test-pmo", TEST_PASSWORD)
        create_user(db_path, login=login, name=f"테스트 {role}", role=role, password=TEST_PASSWORD,
                    projects=projects, actor=admin)
        found = authenticate(db_path, login, TEST_PASSWORD)
    app.session_state["user_id"] = found.user_id
    return app
