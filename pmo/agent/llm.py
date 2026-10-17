"""LLM 제공자 연결. 제공자마다 ChatClient 하나만 구현하면 Agent·평가 코드는 그대로 쓴다."""
import json
import os
import re
import urllib.error
import urllib.request
from typing import Protocol


class LLMError(RuntimeError):
    """LLM 서버 연결·응답 오류. 화면·평가에서 사용자에게 그대로 안내한다."""


class ChatClient(Protocol):
    name: str

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """대화와 도구 목록을 보내고 {"content": str, "tool_calls": [{"name", "arguments"}]}를 돌려준다."""
        ...


_THINK = re.compile(r"<think>.*?</think>", re.S)


def _arguments(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {"_raw": str(value)}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


class OllamaClient:
    """로컬 Ollama 서버(/api/chat). 네트워크 밖으로 데이터를 보내지 않는다."""

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 timeout: float = 120.0):
        self.model = model or os.environ.get("PMO_OLLAMA_MODEL", "qwen3:8b")
        self.base_url = (base_url or os.environ.get("PMO_OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.timeout = timeout
        self.name = f"ollama:{self.model}"

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {
            "model": self.model, "messages": messages, "stream": False,
            "options": {"temperature": 0},
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
            }} for t in tools],
        }
        think = os.environ.get("PMO_OLLAMA_THINK")
        if think in ("true", "false"):
            payload["think"] = think == "true"
        request = urllib.request.Request(
            f"{self.base_url}/api/chat", data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"Ollama 오류({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Ollama 서버에 연결할 수 없습니다({self.base_url}). "
                           "Ollama가 실행 중인지, 모델을 받았는지 확인하세요.") from exc
        message = body.get("message") or {}
        return {
            "content": _THINK.sub("", message.get("content") or "").strip(),
            "tool_calls": [{"name": (c.get("function") or {}).get("name", ""),
                            "arguments": _arguments((c.get("function") or {}).get("arguments"))}
                           for c in message.get("tool_calls") or []],
        }


def client_from_env() -> ChatClient:
    provider = os.environ.get("PMO_LLM_PROVIDER", "ollama")
    if provider == "ollama":
        return OllamaClient()
    raise LLMError(f"지원하지 않는 LLM 제공자입니다: {provider}")
