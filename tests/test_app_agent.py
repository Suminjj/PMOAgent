"""홈 화면(AI Agent 챗봇 + 수행 결과) 테스트 (Streamlit AppTest + 가짜 Ollama 서버, 가상 데이터)."""
import json
import os
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
    assert app.session_state["nav_page"] == "홈"
    assert not app.sidebar.radio                                         # 메뉴는 라디오가 아니라 버튼
    assert [b.key for b in app.sidebar.button][:7] == [f"nav_{i}" for i in range(7)]
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


def test_agent_request_shows_confirm_card_and_saves_only_after_confirm(app):
    REPLIES[:] = [tool("prepare_checklist_update", person_id="이가상", item="ID카드 신청", status="진행 중"),
                  say("화면 오른쪽 확인 카드에서 '확인 후 저장'을 눌러 주세요.")]
    app.chat_input[0].set_value("이가상 ID카드 신청 진행 중으로 바꿔줘").run()
    assert not app.exception
    service = Service(Path(os.environ["PMO_DB_PATH"]))
    aid = service.assignments("DEMO-002")[0]["id"]
    assert service.checklist(aid)[1]["status"] == "미진행"            # 카드만으로는 저장 안 됨
    rows = app.table[0].value
    assert list(rows["변경"])[3] == "진행 중"                          # 현재 → 변경 카드
    assert not [s for s in app.selectbox if s.key and s.key.startswith("act")]  # 값을 고르는 양식이 없음
    app.button(key="act0_1_confirm").click().run()
    assert not app.exception
    assert service.checklist(aid)[1]["status"] == "진행 중"


def test_cancel_does_not_save(app):
    REPLIES[:] = [tool("prepare_account_id", person_id="DEMO-002", account_id="lee.fake"), say("확인해 주세요.")]
    app.chat_input[0].set_value("이가상 계정 ID lee.fake로 넣어줘").run()
    app.button(key="act0_1_cancel").click().run()
    assert not app.exception
    service = Service(Path(os.environ["PMO_DB_PATH"]))
    assert next(p for p in service.people() if p["id"] == "DEMO-002")["account_id"] is None
    assert any("취소했습니다" in i.value for i in app.info)


def test_category_selector_limits_agent_tools(app):
    assert app.radio(key="home_agent").value == "auto"
    assert [o for o in app.radio(key="home_agent").options] == [
        "자동(전체)", "인력관리", "근무 환경 준비", "구매계약", "검수"]
    app.radio(key="home_agent").set_value("contract").run()
    assert any("구매계약" in c.value for c in app.caption)               # 예시 질문 · 구매계약
    REPLIES[:] = [tool("pending_contract_alerts"), say("미확인 알림은 2건입니다.")]
    app.chat_input[0].set_value("처리 안 한 계약 만료 알림 있어?").run()
    assert not app.exception
    sent = {t["function"]["name"] for t in RECEIVED[0]["tools"]}
    assert "pending_contract_alerts" in sent and "checklist_status" not in sent
    assert "[업무 지침: contract-expiry]" in RECEIVED[0]["messages"][0]["content"]
    assert any(c.value == "구매계약 Agent" for c in app.caption)


def test_auto_category_keeps_all_tools(app):
    REPLIES[:] = [say("안녕하세요.")]
    app.chat_input[0].set_value("안녕").run()
    assert len(RECEIVED[0]["tools"]) == 14
    assert any(c.value == "자동(전체) Agent" for c in app.caption)
