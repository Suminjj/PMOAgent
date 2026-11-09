"""로그인 화면 (11/09 담당자: 가운데 좁은 칸 + AI Agent 그림). 가상 데이터·테스트 계정만 쓴다."""
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from pmo.access import create_user  # noqa: E402
from pmo.service import Service  # noqa: E402
from tests.conftest import TEST_PASSWORD  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app.py"


def test_login_screen_is_centered_with_agent_picture(monkeypatch, tmp_path):
    path = tmp_path / "login.db"
    Service(path)
    create_user(path, login="test-pmo", name="테스트 PMO", role="PMO", password=TEST_PASSWORD)
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    hero = next(m.value for m in app.markdown if 'class="pmo-auth-hero"' in m.value)   # CSS 블록이 아닌 실제 그림
    assert "<svg" in hero and "PMO Agent" in hero and "로그인" in hero      # AI Agent 그림 + 제목
    assert len(app.columns) == 3                                          # 가운데 칸에만 로그인 폼
    app.text_input[0].set_value("test-pmo")
    app.text_input[1].set_value(TEST_PASSWORD)
    app.button[0].click().run()
    assert not app.exception and app.session_state["user_id"]
