"""LLM 제공자 연결. 제공자마다 ChatClient 하나만 구현하면 Agent·평가 코드는 그대로 쓴다."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol


class LLMError(RuntimeError):
    """LLM 서버 연결·응답 오류. 화면·평가에서 사용자에게 그대로 안내한다."""


class ChatClient(Protocol):
    name: str

    def check(self) -> None:
        """서버와 모델을 쓸 수 있는지 확인한다. 문제가 있으면 LLMError."""
        ...

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """대화와 도구 목록을 보내고 {"content": str, "tool_calls": [{"name", "arguments"}]}를 돌려준다.

        "usage": {"prompt_tokens", "completion_tokens"}가 있으면 토큰 수를 기록한다(없어도 된다).
        """
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
                 timeout: float = 300.0):
        self.model = model or os.environ.get("PMO_OLLAMA_MODEL", "qwen3:8b")
        self.base_url = (base_url or os.environ.get("PMO_OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.timeout = timeout
        self.num_ctx = int(os.environ.get("PMO_OLLAMA_NUM_CTX", "8192"))
        self.name = f"ollama:{self.model}"

    def check(self) -> None:
        """서버가 켜져 있고 모델을 받아 두었는지 확인한다. 문제가 있으면 LLMError."""
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=10) as response:
                tags = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise LLMError(f"Ollama 서버에 연결할 수 없습니다({self.base_url}). "
                           "Ollama 앱을 실행하거나 'ollama serve'로 서버를 켜세요.") from exc
        names = {m.get("name") for m in tags.get("models", [])} | {m.get("model") for m in tags.get("models", [])}
        wanted = {self.model, f"{self.model}:latest"}
        if not names & wanted:
            raise LLMError(f"모델 {self.model}이(가) 없습니다. 'ollama pull {self.model}'로 받으세요.")

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/api/chat", data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 400 and "think" in detail.lower() and "think" in payload:
                raise _ThinkUnsupported(detail) from exc
            raise LLMError(f"Ollama 오류({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Ollama 서버에 연결할 수 없습니다({self.base_url}). "
                           "Ollama가 실행 중인지, 모델을 받았는지 확인하세요.") from exc

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {
            "model": self.model, "messages": messages, "stream": False,
            # num_ctx: Ollama 기본 문맥(4096)은 지시문+도구 설명+조회 결과를 다 담지 못해 앞부분이 잘릴 수 있다.
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
            }} for t in tools],
        }
        # 추론 모드: 평가 결과(qwen3:8b, 질문 7개) 끈 상태에서도 정답률 7/7, 응답은 약 4배 빠름 → 기본 끔.
        think = os.environ.get("PMO_OLLAMA_THINK", "false").strip().lower()
        if think in ("true", "false"):
            payload["think"] = think == "true"
        try:
            body = self._post(payload)
        except _ThinkUnsupported:
            # 추론 모드가 없는 모델은 think 값을 받지 않으므로 빼고 다시 보낸다.
            payload.pop("think", None)
            body = self._post(payload)
        message = body.get("message") or {}
        # Ollama가 응답마다 주는 입력(prompt_eval_count)·출력(eval_count) 토큰 수. 평가·실행 기록용.
        usage = {"prompt_tokens": body.get("prompt_eval_count"), "completion_tokens": body.get("eval_count")}
        return {
            "usage": usage,
            "content": _THINK.sub("", message.get("content") or "").strip(),
            "tool_calls": [{"name": (c.get("function") or {}).get("name", ""),
                            "arguments": _arguments((c.get("function") or {}).get("arguments"))}
                           for c in message.get("tool_calls") or []],
        }


class _ThinkUnsupported(Exception):
    pass


class OpenAIClient:
    """OpenAI API(/chat/completions, 11/14 담당자 요청). 질문과 도구 결과가 PC 밖(OpenAI 서버)으로 나간다.

    기본은 여전히 로컬 Ollama다. PMO_LLM_PROVIDER=openai로 켤 때만 쓰고, 화면은 실제 데이터 DB(data/pmo.db)에서는
    막는다(`external_blocked`). 키는 환경변수 OPENAI_API_KEY로만 받고 코드·로그에 남기지 않는다.
    PMO_OPENAI_BASE_URL을 바꾸면 OpenAI 형식을 따르는 다른 주소(사내 게이트웨이 등)도 쓸 수 있다.
    PMO_OPENAI_API_VERSION을 주면 Azure OpenAI 방식으로 부른다(11/14 지원받은 키의 게이트웨이):
    {BASE_URL}/openai/deployments/{모델}/chat/completions?api-version=…, 인증 헤더 api-key.
    """

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 timeout: float = 120.0):
        self.model = model or os.environ.get("PMO_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        self.base_url = (base_url or os.environ.get("PMO_OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self._key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self.timeout = timeout
        # 추론 모델(gpt-5 이후)의 생각 정도: none·minimal·low·medium·high. 비우면 모델 기본값(보내지 않음)
        self.reasoning = os.environ.get("PMO_OPENAI_REASONING", "").strip().lower()
        self.api_version = os.environ.get("PMO_OPENAI_API_VERSION", "").strip()   # 있으면 Azure OpenAI 방식
        self.name = f"openai:{self.model}"

    def _request(self, path: str, payload: dict | None = None) -> dict:
        if not self._key:
            raise LLMError("OpenAI API 키가 없습니다. 환경변수 OPENAI_API_KEY에 키를 넣고 다시 실행하세요.")
        if self.api_version:      # Azure OpenAI: 모델(배포) 이름이 주소에, api-version이 쿼리에 들어간다
            url = (f"{self.base_url}/openai/deployments/{urllib.parse.quote(self.model, safe='')}{path}"
                   f"?api-version={urllib.parse.quote(self.api_version)}")
            auth = {"api-key": self._key}
        else:
            url, auth = f"{self.base_url}{path}", {"Authorization": f"Bearer {self._key}"}
        request = urllib.request.Request(
            url, data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", **auth},
            method="GET" if payload is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            hint = {401: "API 키가 맞지 않습니다(OPENAI_API_KEY 확인).",
                    403: "이 키로 쓸 수 없는 모델이거나 권한이 없습니다.",
                    404: f"모델(배포) {self.model}을(를) 찾을 수 없습니다(PMO_OPENAI_MODEL·PMO_OPENAI_BASE_URL 확인).",
                    429: "사용 한도(요청 수·크레딧)를 넘었습니다. 잠시 뒤 다시 하거나 한도를 확인하세요."}.get(exc.code, "")
            raise LLMError(f"OpenAI 오류({exc.code}). {hint} {detail}".strip()) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise LLMError(f"OpenAI API에 연결할 수 없습니다({self.base_url}). 인터넷·회사 프록시 설정을 확인하세요.") from exc

    def check(self) -> None:
        """키와 모델을 쓸 수 있는지 확인한다(모델 목록 조회, 토큰을 쓰지 않음)."""
        if self.api_version:      # Azure 방식은 모델 목록 주소가 없어 짧은 질문 한 번으로 확인한다
            self._request("/chat/completions", {"model": self.model, "messages": [{"role": "user", "content": "ping"}]})
            return
        body = self._request("/models")
        names = {m.get("id") for m in body.get("data") or []}
        if names and self.model not in names:
            chat = sorted(n for n in names if n and n.startswith("gpt"))
            raise LLMError(f"이 키로 쓸 수 있는 모델에 {self.model}이(가) 없습니다. 쓸 수 있는 모델: {', '.join(chat[:15])}")

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {"model": self.model, "messages": to_openai_messages(messages)}
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
            }} for t in tools]
        if self.reasoning:
            payload["reasoning_effort"] = self.reasoning
        try:
            body = self._request("/chat/completions", payload)
        except LLMError as exc:
            # 11/14: gpt-6.1-sol은 /chat/completions에서 도구와 추론(reasoning_effort)을 함께 쓰지 못한다
            # ("set reasoning_effort to 'none'"). 이 오류면 추론을 끄고 한 번 다시 보낸다.
            if "reasoning_effort" not in str(exc) or payload.get("reasoning_effort") == "none":
                raise
            payload["reasoning_effort"] = "none"
            body = self._request("/chat/completions", payload)
        message = ((body.get("choices") or [{}])[0]).get("message") or {}
        usage = body.get("usage") or {}
        return {
            "usage": {"prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens")},
            "content": (message.get("content") or "").strip(),
            "tool_calls": [{"name": (c.get("function") or {}).get("name", ""),
                            "arguments": _arguments((c.get("function") or {}).get("arguments"))}
                           for c in message.get("tool_calls") or []],
        }


DEFAULT_OPENAI_MODEL = "gpt-4.1"   # 11/15 담당자: 평가 세트 54문항 4개 모델 비교에서 오답 없이 빠름(AGENTS.md 3장)


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Agent 실행 루프의 메시지(Ollama 모양)를 OpenAI 모양으로 바꾼다.

    OpenAI는 도구 호출마다 id가 있어야 하고, 도구 결과는 그 id(tool_call_id)로 답해야 하며, 인자는 JSON 문자열이다.
    실행 루프는 도구 호출 직후 호출 순서대로 결과를 붙이므로 순서대로 id를 맞춘다.
    """
    out, waiting, n = [], [], 0
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                n += 1
                f = c.get("function") or {}
                calls.append({"id": f"call_{n}", "type": "function", "function": {
                    "name": f.get("name", ""), "arguments": json.dumps(f.get("arguments") or {}, ensure_ascii=False)}})
            waiting = [c["id"] for c in calls]
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": calls})
        elif role == "tool":
            if waiting:
                out.append({"role": "tool", "tool_call_id": waiting.pop(0), "content": str(m.get("content") or "")})
            else:                                  # 짝이 없는 도구 결과는 일반 정보로 넘긴다
                out.append({"role": "user", "content": f"[도구 결과 {m.get('tool_name', '')}] {m.get('content') or ''}"})
        else:
            out.append({"role": role, "content": str(m.get("content") or "")})
    return out


def _real_db(db_file) -> bool:
    """실제 데이터 DB(data/pmo.db)인지. 모르면(None) 앱 설정의 DB(PMO_DB_PATH, 없으면 data/pmo.db)로 본다."""
    from pathlib import Path
    if db_file is None:
        from pmo.config import db_path
        db_file = db_path()
    return Path(db_file).name == "pmo.db"


def provider(db_file=None) -> str:
    """쓸 LLM 제공자. 11/14 담당자 확정: 시연·평가(가상 데이터 DB)는 API, 실제 데이터 DB는 로컬 Ollama.

    PMO_LLM_PROVIDER를 주면 그대로 따른다. 없으면 API 키(OPENAI_API_KEY)가 있고 가상 데이터 DB일 때 openai, 아니면 ollama.
    """
    chosen = os.environ.get("PMO_LLM_PROVIDER", "").strip().lower()
    if chosen:
        return chosen
    return "openai" if os.environ.get("OPENAI_API_KEY") and not _real_db(db_file) else "ollama"


def is_external(db_file=None) -> bool:
    """질문·조회 결과가 PC 밖으로 나가는 제공자인지."""
    return provider(db_file) != "ollama"


def _provider_of_model(model: str) -> str | None:
    """모델 이름으로 제공자를 알 수 있으면(평가에서 qwen3:8b,gpt-6-luna를 한 번에 비교) 그 제공자."""
    name = model.strip().lower()
    if name.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    if ":" in name or name.startswith(("qwen", "gemma", "llama", "mistral", "exaone")):
        return "ollama"
    return None


def make_client(model: str | None = None, db_file=None) -> ChatClient:
    """제공자에 맞는 클라이언트. model을 주면 그 모델로(이름으로 제공자를 알 수 있으면 그 제공자)."""
    name = (_provider_of_model(model) if model else None) or provider(db_file)
    if name == "ollama":
        return OllamaClient(model=model)
    if name == "openai":
        return OpenAIClient(model=model)
    raise LLMError(f"지원하지 않는 LLM 제공자입니다: {name} (ollama 또는 openai)")


def client_from_env() -> ChatClient:
    return make_client()


def model_label(db_file=None) -> str:
    """화면 표시용 모델 이름."""
    if provider(db_file) == "openai":
        return os.environ.get("PMO_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    return os.environ.get("PMO_OLLAMA_MODEL", "qwen3:8b")


def external_blocked(client, db_file) -> str | None:
    """외부 API 클라이언트를 실제 데이터 DB에 쓰려 하면 막는 이유. 괜찮으면 None.

    AGENTS.md: 실제 인력·계약 데이터는 PC 밖으로 보내지 않는다. 외부 API는 가상 데이터 DB(PMO_DB_PATH)로 시연·평가할 때만 쓴다.
    """
    if isinstance(client, OpenAIClient) and _real_db(db_file):
        return ("외부 LLM API는 실제 데이터 DB(data\\pmo.db)에서 쓸 수 없습니다. "
                "가상 데이터 DB(PMO_DB_PATH)로 실행하거나 PMO_LLM_PROVIDER=openai 설정을 지우고 로컬 Ollama를 쓰세요.")
    return None
