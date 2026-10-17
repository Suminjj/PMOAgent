"""Agent 실행 루프·Ollama 연결·LLM 평가 하네스 테스트 (실제 LLM 없이 가짜 클라이언트 사용)."""
import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pmo.agent.llm import LLMError, OllamaClient
from pmo.agent.runner import ask
from pmo.demo import seed_demo
from pmo.harness import AgentPlanner, run
from pmo.service import Service

TODAY = date(2026, 10, 17)


class ScriptedClient:
    """정해진 응답을 차례로 돌려주는 가짜 LLM. 받은 메시지를 기록한다."""
    name = "fake:scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def chat(self, messages, tools):
        self.seen.append((list(messages), tools))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def tool_call(tool, **arguments):
    return {"content": "", "tool_calls": [{"name": tool, "arguments": arguments}]}


def text(content):
    return {"content": content, "tool_calls": []}


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "agent.db", clock=lambda: TODAY)
    seed_demo(s, TODAY)
    return s


def test_agent_runs_tools_and_returns_answer(service):
    client = ScriptedClient([
        tool_call("find_people", name="이가상"),
        tool_call("get_assignment_timeline", person_id="DEMO-002"),
        text("이가상(베타컨설팅)은 가상 과제 Y에 투입 중입니다."),
    ])
    result = ask(service, "이가상 지금 무슨 과제야?", client)
    assert result["ok"] and "가상 과제 Y" in result["answer"]
    assert [c["tool"] for c in result["calls"]] == ["find_people", "get_assignment_timeline"]
    system, user = client.seen[0][0][:2]
    assert "2026-10-17" in system["content"] and user["content"] == "이가상 지금 무슨 과제야?"
    tool_reply = client.seen[2][0][-1]
    assert tool_reply["role"] == "tool" and "가상 과제 Y" in tool_reply["content"]
    assert service.run_log()[0]["operation"] == "agent:fake:scripted"


def test_agent_sees_planned_tools_and_gets_error(service):
    client = ScriptedClient([tool_call("monthly_purchase_needs", year=2026, month=11),
                             text("구매 필요 판정 기준이 아직 정해지지 않았습니다.")])
    result = ask(service, "11월 구매 필요 인력", client)
    names = {t["name"] for t in client.seen[0][1]}
    assert "monthly_purchase_needs" in names
    assert result["ok"] and result["calls"][0]["ok"] is False
    assert "구현 예정" in client.seen[1][0][-1]["content"]


def test_agent_bad_arguments_are_reported_to_llm(service):
    client = ScriptedClient([tool_call("contracts_ending", year=2026, month="열"),
                             text("월을 확인해 주세요.")])
    result = ask(service, "열월 종료", client)
    assert result["calls"][0]["ok"] is False and "정수" in client.seen[1][0][-1]["content"]


def test_agent_step_limit_and_llm_errors(service):
    looping = ScriptedClient([tool_call("pending_contract_alerts")] * 3)
    result = ask(service, "알림", looping, max_steps=3)
    assert not result["ok"] and "3번" in result["error"]
    broken = ScriptedClient([LLMError("Ollama 서버에 연결할 수 없습니다")])
    result = ask(service, "알림", broken)
    assert not result["ok"] and "연결" in result["error"]
    assert service.run_log()[0]["ok"] == 0


def test_agent_planner_in_eval_harness():
    script = {
        "이가상": [tool_call("find_people", name="이가상"),
                 tool_call("get_assignment_timeline", person_id="DEMO-002"), text("가상 과제 Y")],
    }

    class PerQuestion:
        name = "fake:per-question"

        def __init__(self):
            self.current = []

        def chat(self, messages, tools):
            if len(messages) == 2:  # 새 질문
                question = messages[1]["content"]
                key = next((k for k in script if question.startswith(k)), None)
                self.current = list(script[key]) if key else [text("모르겠습니다.")]
            return self.current.pop(0)

    seen = []
    report = run(AgentPlanner(PerQuestion()), save=False, on_result=lambda r: seen.append(r["id"]))
    assert seen == [r["id"] for r in report["results"]]  # 질문마다 바로 알림
    rows = {r["id"]: r for r in report["results"]}
    assert rows["Q1"]["success"] and rows["Q1"]["answer"] == "가상 과제 Y"
    assert rows["Q1"]["agent_ms"] >= 0
    assert not rows["Q3"]["success"]   # 도구를 안 부르면 실패로 기록
    assert report["planner"] == "fake-per-question"


# --- Ollama HTTP 연결 (로컬 가짜 서버) -------------------------------------------

class FakeOllama(BaseHTTPRequestHandler):
    received = []
    reply = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.received.append((self.path, body))
        data = json.dumps(FakeOllama.reply, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_ollama():
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakeOllama.received.clear()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_ollama_client_request_and_tool_call_parsing(fake_ollama):
    FakeOllama.reply = {"message": {"role": "assistant", "content": "<think>생각</think>",
                                    "tool_calls": [{"function": {"name": "find_people",
                                                                 "arguments": {"name": "김가상"}}}]}}
    client = OllamaClient(model="qwen3:8b", base_url=fake_ollama)
    tools = [{"name": "find_people", "description": "찾기",
              "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}}}]
    reply = client.chat([{"role": "user", "content": "김가상"}], tools)
    assert reply == {"content": "", "tool_calls": [{"name": "find_people", "arguments": {"name": "김가상"}}]}
    path, body = FakeOllama.received[0]
    assert path == "/api/chat" and body["model"] == "qwen3:8b" and body["stream"] is False
    assert body["options"]["num_ctx"] == 8192  # Ollama 기본 4096이면 지시문·도구 설명이 잘릴 수 있다
    assert body["tools"][0]["function"]["parameters"]["properties"]["name"]["type"] == "string"


def test_ollama_client_string_arguments_and_unreachable_server(fake_ollama):
    FakeOllama.reply = {"message": {"content": "", "tool_calls": [
        {"function": {"name": "contracts_ending", "arguments": '{"year": 2026, "month": 10}'}}]}}
    reply = OllamaClient(model="m", base_url=fake_ollama).chat([], [])
    assert reply["tool_calls"][0]["arguments"] == {"year": 2026, "month": 10}
    with pytest.raises(LLMError, match="연결할 수 없습니다"):
        OllamaClient(model="m", base_url="http://127.0.0.1:9", timeout=2).chat([], [])
