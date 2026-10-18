"""자연어 조회 화면 테스트 (Streamlit AppTest + 가짜 Ollama 서버, 가상 데이터)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from pmo.config import today  # noqa: E402
from pmo.demo import seed_demo  # noqa: E402
from pmo.service import Service  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app.py"
REPLIES = []


class FakeOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        data = json.dumps(REPLIES.pop(0), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def app(monkeypatch, tmp_path):
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    path = tmp_path / "nl.db"
    seed_demo(Service(path), today())
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    monkeypatch.setenv("PMO_OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    test_app = AppTest.from_file(str(APP), default_timeout=30).run()
    test_app.sidebar.radio[0].set_value("자연어 조회").run()
    yield test_app
    server.shutdown()


def test_natural_language_question_uses_tools(app):
    REPLIES[:] = [
        {"message": {"content": "", "tool_calls": [
            {"function": {"name": "pending_contract_alerts", "arguments": {}}}]}},
        {"message": {"content": "미확인 알림은 2건입니다."}},
    ]
    app.text_input(key="nl_question").set_value("처리 안 한 계약 만료 알림 있어?")
    app.button(key="nl_ask").click().run()
    assert not app.exception
    assert any("미확인 알림은 2건" in m.value for m in app.markdown)


def test_llm_unavailable_shows_error_and_fallback(app, monkeypatch):
    monkeypatch.setenv("PMO_OLLAMA_URL", "http://127.0.0.1:9")
    app.text_input(key="nl_question").set_value("알림 있어?")
    app.button(key="nl_ask").click().run()
    assert not app.exception
    assert any("연결할 수 없습니다" in e.value for e in app.error)
