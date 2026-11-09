"""홈 화면(AI Agent 채팅: 질문·답변·수행 결과를 한 화면에) 테스트 (Streamlit AppTest + 가짜 Ollama 서버, 가상 데이터)."""
from datetime import date
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
from tests.conftest import login_app  # noqa: E402

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
    test_app = login_app(AppTest.from_file(str(APP), default_timeout=30), os.environ["PMO_DB_PATH"]).run()
    yield test_app
    server.shutdown()


def test_home_is_first_page_with_chat_and_results(app):
    assert app.session_state["nav_page"] == "홈"
    assert not app.sidebar.radio                                         # 메뉴는 라디오가 아니라 버튼
    assert [b.key for b in app.sidebar.button][:7] == [f"nav_{i}" for i in range(7)]
    assert any("pmo-agent-title" in m.value and "PMO Agent" in m.value for m in app.markdown)   # 11/09 시안 A 머리띠
    assert "수행 결과" not in [s.value for s in app.subheader]          # 11/08: 오른쪽 결과 패널 없음
    assert app.chat_input


def test_question_shows_answer_and_tool_results(app):
    REPLIES[:] = [tool("pending_contract_alerts"), say("미확인 알림은 2건입니다.")]
    app.chat_input[0].set_value("처리 안 한 계약 만료 알림 있어?").run()
    assert not app.exception
    assert any("미확인 알림은 2건" in m.value for m in app.markdown)
    assert any("미확인 계약 종료 알림" in m.value for m in app.markdown)   # 답변 아래 조회 결과의 도구 이름
    assert app.dataframe and len(app.dataframe[-1].value) == 2
    # 11/08 담당자: 채팅 화면에는 도구 입력(함수 모양)·JSON 원본을 보이지 않는다(실행 기록에만 남김)
    assert not app.json
    assert not any("pending_contract_alerts(" in c.value for c in app.caption)


def test_follow_up_keeps_conversation(app):
    REPLIES[:] = [tool("find_people", name="김가상"), say("김가상이 2명입니다. 어느 분인가요?"),
                  tool("find_people", name="김가상"), say("알파솔루션 김가상으로 볼게요.")]
    app.chat_input[0].set_value("김가상 투입 과제 알려줘").run()
    app.chat_input[0].set_value("알파솔루션").run()
    assert not app.exception
    roles = [m["role"] for m in RECEIVED[-2]["messages"]]
    assert roles[:4] == ["system", "user", "assistant", "user"]
    assert not any(s.label == "결과를 볼 질문" for s in app.selectbox)   # 11/08: 질문 고르기 없이 한 화면 채팅
    results = [e for e in app.expander if "조회 결과" in e.label]
    assert len(results) == 2                                           # 답변마다 그 아래에 조회 결과


def test_intro_examples_and_llm_unavailable(app, monkeypatch):
    """11/09 담당자: 예시 질문 버튼은 없애고 첫 안내 말풍선에 예시 두 개(업체명·인력명은 회색)만 적는다."""
    assert not [b for b in app.button if (b.key or "").startswith("home_example")]
    intro = next(m.value for m in app.markdown if "예시 질문" in m.value)
    assert ":gray[업체명]" in intro and "검수 금액 알려줘" in intro and ":gray[인력명] 투입 중인 과제 알려줘" in intro
    monkeypatch.setenv("PMO_OLLAMA_URL", "http://127.0.0.1:9")
    app.chat_input[0].set_value("김가상 투입 중인 과제 알려줘").run()
    assert not app.exception
    assert any("연결할 수 없습니다" in e.value for e in app.error)


def contract_end(service):
    return next(c for c in service.contracts("DEMO-002") if c["contract_no"] == "DEMO-C-200")["end_date"]


def test_agent_request_shows_confirm_card_and_saves_only_after_confirm(app):
    REPLIES[:] = [tool("prepare_contract_save", person_id="이가상", end_date="2027-12-31"),
                  say("답변 아래 확인 카드에서 '확인 후 저장'을 눌러 주세요.")]
    app.chat_input[0].set_value("이가상 계약 종료일 2027년 12월 31일로 바꿔줘").run()
    assert not app.exception
    service = Service(Path(os.environ["PMO_DB_PATH"]))
    before = contract_end(service)
    assert before != "2027-12-31"                                     # 카드만으로는 저장 안 됨
    rows = app.table[0].value
    card = dict(zip(rows["항목"], rows["변경"]))                       # 현재 → 변경 카드 (항목 이름으로 확인)
    assert card["계약 종료일"] == "2027-12-31" and "계약 상태" not in card   # 11/09 계약 상태 줄 없음
    assert not [s for s in app.selectbox if s.key and s.key.startswith("act")]  # 값을 고르는 양식이 없음
    app.button(key="act0_1_confirm").click().run()
    assert not app.exception
    assert contract_end(service) == "2027-12-31"


def test_cancel_does_not_save(app):
    REPLIES[:] = [tool("prepare_account_id", person_id="DEMO-002", account_id="lee.fake"), say("확인해 주세요.")]
    app.chat_input[0].set_value("이가상 계정 ID lee.fake로 넣어줘").run()
    app.button(key="act0_1_cancel").click().run()
    assert not app.exception
    service = Service(Path(os.environ["PMO_DB_PATH"]))
    assert next(p for p in service.people() if p["id"] == "DEMO-002")["account_id"] is None
    assert any("취소했습니다" in i.value for i in app.info)


def test_category_selector_limits_agent_tools(app):
    labels = [app.button(key=f"home_agent_{k}").label for k in ("auto", "staff", "contract", "inspection")]
    assert labels == ["🤖 자동 배정", "👤 인력관리", "📄 구매계약", "💰 검수"]          # v3.1: 자동 = 라우터
    assert app.session_state["home_category"] == "auto"
    app.button(key="home_agent_contract").click().run()
    assert app.session_state["home_category"] == "contract"
    REPLIES[:] = [tool("pending_contract_alerts"), say("미확인 알림은 2건입니다.")]
    app.chat_input[0].set_value("처리 안 한 계약 만료 알림 있어?").run()
    assert not app.exception
    sent = {t["function"]["name"] for t in RECEIVED[0]["tools"]}
    assert "pending_contract_alerts" in sent and "find_people" in sent and "get_assignment_timeline" not in sent
    assert "[업무 지침: contract-expiry]" in RECEIVED[0]["messages"][0]["content"]
    assert any(c.value == "📄 구매계약 Agent" for c in app.caption)


def test_auto_category_routes_to_specialist(app):
    """v3.1: '자동 배정'은 라우터가 질문에 맞는 전문 Agent를 고른다(예전 자동(전체)은 모든 도구를 받던 단일 Agent)."""
    from pmo.agent.agents import AGENTS
    REPLIES[:] = [say("미확인 알림은 없습니다.")]
    app.chat_input[0].set_value("처리 안 한 알림 있어?").run()
    assert not app.exception
    sent = {t["function"]["name"] for t in RECEIVED[0]["tools"]}
    assert sent == set(AGENTS["contract"].tools)                      # '알림' → 구매계약 Agent의 도구만
    assert any("자동 배정" in c.value and "구매계약 Agent" in c.value for c in app.caption)


def test_home_mm_calculator_on_screen_with_one_calendar(app):
    """11/09 담당자: MM 계산기는 모달 없이 홈 왼쪽에서 바로 쓴다. 달력 하나로 기간을 고르고 Enter(계산)."""
    assert not any("계정 ID 미발급" in m.value for m in app.markdown)
    calendar = app.date_input(key="mm_calc_range")                 # 버튼 없이 처음부터 보임
    calendar.set_value((date(2026, 10, 1), date(2026, 10, 10)))
    next(b for b in app.button if b.label == "계산 (Enter)").click().run()
    assert not app.exception
    assert any(">0.250<" in m.value and "근무일 5/20일" in m.value for m in app.markdown)   # 10월 1~10일 = 5/20
    app.date_input(key="mm_calc_range").set_value((date(2026, 10, 1), date(2026, 12, 31)))
    next(b for b in app.button if b.label == "계산 (Enter)").click().run()
    assert any(">3.000<" in m.value and "3개월" in m.value for m in app.markdown)


def test_mm_question_shows_readable_result_not_json(app):
    """11/08: '11월 2일부터 15일까지 MM' → 기간 MM 계산 도구, 결과는 표로(JSON 아님)."""
    REPLIES[:] = [tool("calculate_mm", start_date="2026-11-02", end_date="2026-11-15"), say("0.476 MM입니다.")]
    app.chat_input[0].set_value("11월 2일부터 15일까지 MM 얼마야?").run()
    assert not app.exception and not app.json
    assert any("0.476 MM" in m.value for m in app.markdown)
    assert any("기간 MM 계산" in m.value for m in app.markdown)


def test_home_left_cards_and_agent_on_right(app):
    """11/09 담당자: 홈 제목 없음, 왼쪽에 구매 연장 알림·수정 요청 알림·MM 계산기, 오른쪽에 PMO Agent."""
    texts = " ".join(m.value for m in app.markdown)
    assert "무엇을 도와드릴까요" not in texts
    assert "구매 연장 알림" in texts and "수정 요청 알림" in texts
    assert "미확인 계약 종료 알림" not in texts and "계약 종료 예정" not in texts and "등록 인력" not in texts
    assert not any("로컬 LLM(" in c.value for c in app.caption)
    assert texts.index("오늘의 업무") < texts.index("구매 연장 알림") < texts.index("수정 요청 알림")
    assert "Full time" not in texts and "차례로 고른 뒤" not in " ".join(c.value for c in app.caption)   # MM 계산기 문구 정리
    assert 'class="pmo-mini"' in texts or "확인할 계약 종료 알림이 없습니다" in texts   # 건수 아래 간단 목록
    assert "처리를 기다리는 수정 요청이 없습니다" in texts
    assert "자연어로 물어보세요" not in texts and "저장은 답변 아래 확인 카드" not in texts   # 11/09 문구 정리
    assert not [b for b in app.sidebar.button if b.key == "logout"]                     # 로그인 정보는 오른쪽 위
    assert app.button(key="logout") and any('class="pmo-user"' in m.value for m in app.markdown)
    app.button(key="home_go_requests").click().run()
    assert app.session_state["nav_page"] == "수정 요청"
