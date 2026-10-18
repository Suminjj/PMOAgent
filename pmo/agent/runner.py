"""Agent 실행 루프: LLM이 도구를 고르고, 도구 계층이 업무 함수를 실행한다.

agent(pmo/agent/agents.py)를 주면 그 카테고리의 도구·업무 지침만 쓴다. 없으면 자동(전체) 단일 Agent.
"""
import json
import re
import time
from datetime import date, datetime

from pmo.agent.llm import ChatClient, LLMError
from pmo.agent.agents import AgentSpec, allowed_tools, get_agent, system_prompt
from pmo.config import TIMEZONE
from pmo.service import Service
from pmo.tools import TOOL_LABELS, call_tool, tool_specs

MAX_STEPS = 6
# 챗봇 대화에서 이전 질문·답변을 몇 개까지 넘길지 (동명이인 되물음 등 이어지는 대화용)
MAX_HISTORY = 8
# 도구 결과가 길면 LLM 입력에서 잘라낸다(로컬 모델의 문맥 길이 보호).
MAX_TOOL_CHARS = 6000


def _tool_message(name: str, result: dict) -> dict:
    text = json.dumps({k: v for k, v in result.items() if k != "duration_ms"},
                      ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + " …(결과가 길어 잘림)"
    return {"role": "tool", "tool_name": name, "content": text}


def _fill_person(service: Service, tool: str, args: dict, texts: list[str]) -> dict:
    """prepare_* 도구에 대상 인력이 빠졌으면 사용자 문장에 나온 등록 인력 이름으로 채운다.

    작은 로컬 모델은 find_people을 건너뛰고 person_id를 비워 보내는 일이 있다. 이번 질문을 먼저 보고,
    없으면 최근 사용자 메시지를 거슬러 본다. 동명이인은 이름만 넣어 두고 변경 계획 단계에서 되묻는다.
    """
    if not tool.startswith("prepare_") or str(args.get("person_id") or "").strip():
        return args
    names = {p["name"] for p in service.people()}
    for text in texts:
        found = sorted({n for n in names if n and n in text}, key=len, reverse=True)
        found = [n for n in found if not any(n != m and n in m for m in found)]  # 긴 이름 우선
        if len(found) == 1:
            return dict(args, person_id=found[0])
        if found:
            return args   # 여러 사람이 언급되면 추측하지 않는다
    return args


# 사용자가 직접 말해야 하는 값. 도구 결과로 알 수 없어서 모델이 지어내기 쉬운 값만 둔다.
GROUNDED_FIELDS = {
    "prepare_account_id": {"account_id": "계정 ID"},
    "prepare_contract_save": {"contract_no": "계약번호"},
}


def _ungrounded(service: Service, tool: str, args: dict, texts: list[str]) -> str | None:
    """사용자가 말하지 않은 값을 넣었으면 되물을 문장을 돌려준다.

    작은 로컬 모델이 맡은 도구로 할 수 없는 요청(예: 인력관리 Agent에 계약 삭제)을 받으면 비슷한 저장 도구를
    대신 부르고 값을 지어내는 일이 있다. 이런 값은 사용자 문장(이번 질문·최근 사용자 메시지)에 그대로 있어야 한다.
    """
    names = {p["name"] for p in service.people()}
    for field, label in GROUNDED_FIELDS.get(tool, {}).items():
        value = str(args.get(field) or "").strip()
        if not value:
            continue
        if value in names or not any(value.casefold() in t.casefold() for t in texts):
            return (f"{label} '{value}'은(는) 사용자가 말한 값이 아닙니다. 값을 지어내지 마세요. "
                    "사용자가 요청한 작업이 이 도구로 할 일이 맞는지 다시 보고, 맞으면 값을 물어보세요.")
    return None


def _clean_answer(answer: str) -> str:
    """작은 모델이 답변에 도구 이름·내부 표기를 옮겨 적으면 사용자용 말로 바꾼다."""
    for name in sorted(TOOL_LABELS, key=len, reverse=True):
        answer = answer.replace(f"`{name}`", f"'{TOOL_LABELS[name]}'").replace(name, f"'{TOOL_LABELS[name]}'")
    answer = re.sub(r"`?prepare_\*`?\s*도구", "변경 준비 기능", answer)
    return re.sub(r"`?ok\s*:\s*false`?", "실패", answer)


_DONE_WORDS = re.compile(r"(변경|저장|삭제|등록|수정|입력|반영)(되었습니다|됐습니다|했습니다)")


def _not_saved_yet(answer: str) -> str:
    """확인 카드만 만든 턴에 '변경되었습니다'처럼 저장된 것처럼 말하면 고친다(저장은 사용자가 확인해야 함)."""
    return _DONE_WORDS.sub(r"\1 준비를 마쳤습니다(아직 저장 전)", answer)


def ask(service: Service, question: str, client: ChatClient,
        today: date | None = None, max_steps: int = MAX_STEPS,
        history: list[dict] | None = None, agent: AgentSpec | str | None = None) -> dict:
    """질문 하나를 처리한다. 예외 대신 결과 객체로 성공·실패를 돌려준다.

    history: 이전 대화 [{"role": "user"|"assistant", "content": str}] (최근 MAX_HISTORY개만 사용)
    agent: 전문 Agent(AgentSpec 또는 키). 없으면 자동(전체)
    반환: {"ok", "answer", "calls": [{"tool","arguments","ok","data","error"}], "error", "duration_ms",
          "model", "agent", "agent_label"}
    """
    t0, started = time.perf_counter(), datetime.now(TIMEZONE).isoformat()
    day = today or service.clock()
    spec = agent if isinstance(agent, AgentSpec) else get_agent(agent)
    allowed = allowed_tools(spec)
    tools = tool_specs(include_planned=True, names=allowed)
    messages = [{"role": "system", "content": system_prompt(spec, day.isoformat())},
                *[{"role": m["role"], "content": str(m["content"])}
                  for m in (history or [])[-MAX_HISTORY:] if m.get("role") in ("user", "assistant")],
                {"role": "user", "content": question}]
    calls: list[dict] = []
    answer, error = "", None
    try:
        for _ in range(max_steps):
            reply = client.chat(messages, tools)
            if not reply["tool_calls"]:
                answer = _clean_answer(reply["content"])
                break
            messages.append({"role": "assistant", "content": reply["content"],
                             "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                            for c in reply["tool_calls"]]})
            texts = [question] + [m["content"] for m in reversed(history or []) if m.get("role") == "user"]
            for call in reply["tool_calls"]:
                call["arguments"] = _fill_person(service, call["name"], call["arguments"] or {}, texts)
                problem = _ungrounded(service, call["name"], call["arguments"], texts)
                if allowed is not None and call["name"] not in allowed:
                    result = {"ok": False, "tool": call["name"],
                              "error": f"{spec.label} Agent가 쓸 수 없는 도구입니다. 맡은 업무 밖이면 다른 카테고리를 안내하세요."}
                elif problem:
                    result = {"ok": False, "tool": call["name"], "error": problem}
                else:
                    result = call_tool(service, call["name"], call["arguments"])
                calls.append({"tool": call["name"], "arguments": call["arguments"], "ok": result["ok"],
                              "data": result.get("data"), "error": result.get("error")})
                messages.append(_tool_message(call["name"], result))
        else:
            error = f"{max_steps}번 안에 답을 마치지 못했습니다."
    except LLMError as exc:
        error = str(exc)
    if any(c["ok"] and c["tool"].startswith("prepare_") for c in calls):
        answer = _not_saved_yet(answer)
    duration = round((time.perf_counter() - t0) * 1000)
    ok = error is None and bool(answer)
    if error is None and not answer:
        error = "LLM이 빈 답변을 돌려주었습니다."
    service.record_run(f"agent:{getattr(client, 'name', 'llm')}", (question,), {"agent": spec.key}, ok,
                       f"도구 {len(calls)}회" if ok else None, error, t0, started)
    return {"ok": ok, "answer": answer, "calls": calls, "error": error,
            "duration_ms": duration, "model": getattr(client, "name", "llm"),
            "agent": spec.key, "agent_label": spec.label}
