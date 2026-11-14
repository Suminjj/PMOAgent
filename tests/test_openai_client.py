"""OpenAI API 연결(11/14). 실제 API를 부르지 않고 로컬 가짜 서버로 요청 모양·응답 해석·오류 안내를 확인한다."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pmo.agent import llm
from pmo.agent.llm import LLMError, OpenAIClient, external_blocked, make_client, to_openai_messages


class FakeOpenAI(BaseHTTPRequestHandler):
    received = []
    reply = {}
    status = 200
    models = ["gpt-6.1-sol", "gpt-6-luna"]

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        FakeOpenAI.received.append((self.path, self.headers.get("Authorization"), None))
        self._send(FakeOpenAI.status, {"data": [{"id": m} for m in FakeOpenAI.models]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOpenAI.received.append((self.path, self.headers.get("Authorization"), body))
        self._send(FakeOpenAI.status, FakeOpenAI.reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_openai():
    server = HTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeOpenAI.received.clear()
    FakeOpenAI.status = 200
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


TOOLS = [{"name": "find_people", "description": "찾기",
          "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}}}]


def test_request_shape_and_tool_call_parsing(fake_openai, monkeypatch):
    monkeypatch.delenv("PMO_OPENAI_REASONING", raising=False)
    FakeOpenAI.reply = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_x", "type": "function", "function": {"name": "find_people", "arguments": '{"name": "김가상"}'}}]}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 8}}
    client = OpenAIClient(model="gpt-6.1-sol", base_url=fake_openai, api_key="test-key")
    reply = client.chat([{"role": "user", "content": "김가상 과제"}], TOOLS)
    assert reply["tool_calls"] == [{"name": "find_people", "arguments": {"name": "김가상"}}]
    assert reply["usage"] == {"prompt_tokens": 120, "completion_tokens": 8} and reply["content"] == ""
    path, auth, body = FakeOpenAI.received[-1]
    assert path == "/v1/chat/completions" and auth == "Bearer test-key"
    assert body["model"] == "gpt-6.1-sol" and body["tools"][0]["function"]["parameters"] == TOOLS[0]["input_schema"]
    assert "temperature" not in body and "reasoning_effort" not in body     # 추론 모델이 거절하는 값은 보내지 않는다
    monkeypatch.setenv("PMO_OPENAI_REASONING", "low")
    OpenAIClient(model="gpt-6.1-sol", base_url=fake_openai, api_key="k").chat([], [])
    assert FakeOpenAI.received[-1][2]["reasoning_effort"] == "low" and "tools" not in FakeOpenAI.received[-1][2]


def test_agent_loop_messages_become_openai_messages():
    """실행 루프의 Ollama 모양 메시지: 도구 호출에 id를 붙이고, 인자는 JSON 문자열, 결과는 그 id로 답한다."""
    messages = [{"role": "system", "content": "규칙"}, {"role": "user", "content": "질문"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "find_people", "arguments": {"name": "김가상"}}},
                    {"function": {"name": "list_contracts", "arguments": {"person_id": "김가상"}}}]},
                {"role": "tool", "tool_name": "find_people", "content": "{\"ok\": true}"},
                {"role": "tool", "tool_name": "list_contracts", "content": "{\"ok\": false}"},
                {"role": "system", "content": "다시"}]
    out = to_openai_messages(messages)
    calls = out[2]["tool_calls"]
    assert [c["id"] for c in calls] == ["call_1", "call_2"] and out[2]["content"] is None
    assert json.loads(calls[0]["function"]["arguments"]) == {"name": "김가상"} and calls[0]["type"] == "function"
    assert out[3] == {"role": "tool", "tool_call_id": "call_1", "content": "{\"ok\": true}"}
    assert out[4]["tool_call_id"] == "call_2" and out[5] == {"role": "system", "content": "다시"}


def test_errors_are_explained_without_the_key(fake_openai):
    client = OpenAIClient(model="gpt-6.1-sol", base_url=fake_openai, api_key="secret-key")
    FakeOpenAI.status = 401
    FakeOpenAI.reply = {"error": {"message": "Incorrect API key"}}
    with pytest.raises(LLMError, match="API 키가 맞지 않습니다") as caught:
        client.chat([], [])
    assert "secret-key" not in str(caught.value)
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        OpenAIClient(model="m", base_url=fake_openai, api_key="").chat([], [])
    with pytest.raises(LLMError, match="연결할 수 없습니다"):
        OpenAIClient(model="m", base_url="http://127.0.0.1:9/v1", api_key="k", timeout=2).chat([], [])


def test_check_lists_models(fake_openai):
    OpenAIClient(model="gpt-6-luna", base_url=fake_openai, api_key="k").check()
    with pytest.raises(LLMError, match="gpt-6.1-sol"):
        OpenAIClient(model="gpt-9", base_url=fake_openai, api_key="k").check()


def test_provider_choice_and_real_db_block(monkeypatch, tmp_path):
    monkeypatch.delenv("PMO_LLM_PROVIDER", raising=False)
    assert isinstance(make_client(), llm.OllamaClient) and external_blocked(tmp_path / "pmo.db") is None
    monkeypatch.setenv("PMO_LLM_PROVIDER", "openai")
    monkeypatch.setenv("PMO_OPENAI_MODEL", "gpt-6-luna")
    client = make_client()
    assert isinstance(client, OpenAIClient) and client.name == "openai:gpt-6-luna"
    assert make_client("gpt-6.1-sol").model == "gpt-6.1-sol"
    assert "실제 데이터 DB" in external_blocked(tmp_path / "pmo.db")       # 실제 데이터는 PC 밖으로 보내지 않는다
    assert external_blocked(tmp_path / "demo-show.db") is None              # 가상 데이터 DB는 허용
    monkeypatch.setenv("PMO_LLM_PROVIDER", "gemini")
    with pytest.raises(LLMError, match="지원하지 않는"):
        make_client()
