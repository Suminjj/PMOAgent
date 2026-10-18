"""홈 화면(AI Agent 챗봇 + 수행 결과) 테스트 (Streamlit AppTest + 가짜 Ollama 서버, 가상 데이터)."""
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
RECEIVED = []


class FakeOllama(BaseHTTPRequestHandler):
    def do_POST(self):
        RECEIVED.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        data = json.dumps(REPLIES.pop(0), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def tool(tool_name, **args):
    return {"message": {"content": "", "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]}}


def say(text):
    return {"message": {"content": text}}


@pytest.fixture
def app(monkeypatch, tmp_path):
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    path = tmp_path / "home.db"
    seed_demo(Service(path), today())
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    monkeypatch.setenv("PMO_OLLAMA_URL", f"http://127.0.0.1:{server.server_port}")
    RECEIVED.clear()
    test_app = AppTest.from_file(str(APP), default_timeout=30).run()
    yield test_app
    server.shutdown()


def test_home_is_first_page_with_chat_and_results(app):
    assert app.sidebar.radio[0].value == "홈"
    assert [s.value for s in app.subheader][:2] == ["AI Agent", "수행 결과"]
    assert app.chat_input


def test_question_shows_answer_and_tool_results(app):
    REPLIES[:] = [tool("pending_contract_alerts"), say("미확인 알림은 2건입니다.")]
    app.chat_input[0].set_value("처리 안 한 계약 만료 알림 있어?").run()
    assert not app.exception
    assert any("미확인 알림은 2건" in m.value for m in app.markdown)
    assert any("미확인 계약 종료 알림" in m.value for m in app.markdown)   # 오른쪽 결과 패널 제목
    assert app.dataframe and len(app.dataframe[0].value) == 2


def test_follow_up_keeps_conversation(app):
    REPLIES[:] = [tool("find_people", name="김가상"), say("김가상이 2명입니다. 어느 분인가요?"),
                  tool("find_people", name="김가상"), say("알파솔루션 김가상으로 볼게요.")]
    app.chat_input[0].set_value("김가상 투입 과제 알려줘").run()
    app.chat_input[0].set_value("알파솔루션").run()
    assert not app.exception
    roles = [m["role"] for m in RECEIVED[-2]["messages"]]
    assert roles[:4] == ["system", "user", "assistant", "user"]
    assert any(s.label == "결과를 볼 질문" for s in app.selectbox)


def test_example_button_and_llm_unavailable(app, monkeypatch):
    monkeypatch.setenv("PMO_OLLAMA_URL", "http://127.0.0.1:9")
    app.button(key="home_example_0").click().run()
    assert not app.exception
    assert any("연결할 수 없습니다" in e.value for e in app.error)
