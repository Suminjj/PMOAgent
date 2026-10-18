"""단일 Agent 실행 루프: LLM이 도구를 고르고, 도구 계층이 업무 함수를 실행한다."""
import json
import time
from datetime import date, datetime

from pmo.agent.llm import ChatClient, LLMError
from pmo.agent.prompts import SYSTEM_PROMPT
from pmo.config import TIMEZONE
from pmo.service import Service
from pmo.tools import call_tool, tool_specs

MAX_STEPS = 6
# 도구 결과가 길면 LLM 입력에서 잘라낸다(로컬 모델의 문맥 길이 보호).
MAX_TOOL_CHARS = 6000


def _tool_message(name: str, result: dict) -> dict:
    text = json.dumps({k: v for k, v in result.items() if k != "duration_ms"},
                      ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + " …(결과가 길어 잘림)"
    return {"role": "tool", "tool_name": name, "content": text}


def ask(service: Service, question: str, client: ChatClient,
        today: date | None = None, max_steps: int = MAX_STEPS) -> dict:
    """질문 하나를 처리한다. 예외 대신 결과 객체로 성공·실패를 돌려준다.

    반환: {"ok", "answer", "calls": [{"tool","arguments","ok"}], "error", "duration_ms", "model"}
    """
    t0, started = time.perf_counter(), datetime.now(TIMEZONE).isoformat()
    day = today or service.clock()
    tools = tool_specs(include_planned=True)
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(today=day.isoformat())},
                {"role": "user", "content": question}]
    calls: list[dict] = []
    answer, error = "", None
    try:
        for _ in range(max_steps):
            reply = client.chat(messages, tools)
            if not reply["tool_calls"]:
                answer = reply["content"]
                break
            messages.append({"role": "assistant", "content": reply["content"],
                             "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                            for c in reply["tool_calls"]]})
            for call in reply["tool_calls"]:
                result = call_tool(service, call["name"], call["arguments"])
                calls.append({"tool": call["name"], "arguments": call["arguments"], "ok": result["ok"]})
                messages.append(_tool_message(call["name"], result))
        else:
            error = f"{max_steps}번 안에 답을 마치지 못했습니다."
    except LLMError as exc:
        error = str(exc)
    duration = round((time.perf_counter() - t0) * 1000)
    ok = error is None and bool(answer)
    if error is None and not answer:
        error = "LLM이 빈 답변을 돌려주었습니다."
    service.record_run(f"agent:{getattr(client, 'name', 'llm')}", (question,), {}, ok,
                       f"도구 {len(calls)}회" if ok else None, error, t0, started)
    return {"ok": ok, "answer": answer, "calls": calls, "error": error,
            "duration_ms": duration, "model": getattr(client, "name", "llm")}
