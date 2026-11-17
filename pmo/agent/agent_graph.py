"""전문 Agent 하나의 처리 루프(ReAct + 자기 수정)를 LangGraph StateGraph로 실행한다(11/17 담당자: 평가 기준에 LangGraph).

라우터 그래프(graph.py)의 agent 노드가 Agent를 부를 때, 그리고 카테고리를 직접 고른 질문도 이 그래프를 탄다
(runner.ask가 준비만 하고 이 그래프를 실행한다). 노드 안의 판단은 runner.py·guardrails.py의 기존 함수를 그대로 쓴다.

    START → precheck ─┬─(사전 가드레일에 걸림: LLM 없이 정해진 답)─────────────→ END
                      ├─(구현 예정 Agent: 정해진 안내)──────────────────→ verify
                      └→ llm ─┬─(도구 요청)→ tools ─┬→ llm                  (ReAct 반복)
                              │                     └(단계 소진)→ exhausted → verify
                              ├─(도구 없이 답함)→ check_reply ─┬─(다시 시킴: tool_retry·redirect_retry)→ llm
                              │                               ├─(단계 소진)→ exhausted
                              │                               └─(답 확정)→ verify
                              └─(LLM 오류)→ verify
              verify ─┬─(답변에 근거 없는 인력 이름)→ rewrite(한 번 더 쓰게 함, 그래도 있으면 보류) → finish
                      └──────────────────────────────────────────────────────────────→ finish → END

- precheck   : 지시 무시·관리하지 않는 정보·범위 밖·다른 카테고리는 LLM 없이 정해진 문장(여러 업무 질문의 각 Agent는 생략).
- llm        : 대화와 도구 목록을 주고 LLM을 한 번 부른다.
- tools      : 요청한 도구마다 입력을 바로잡거나 막은 뒤(SELF-CORRECT) 실행한다. 변경 준비 도구는 계획만 만든다(SIMULATE).
- check_reply: 도구 없이 답했을 때 — 질문 속 인력을 조회하지 않았거나, 맡은 업무를 다른 카테고리로 보냈으면 한 번 다시 시킨다.
- verify     : 저장 전인데 '완료'라고 쓴 표현을 고치고, 답변 속 인력 이름이 도구 결과에 있는지 본다.
- rewrite    : 근거 없는 이름이 있으면 한 번 더 쓰게 하고(최대 3단계), 그래도 있으면 답변을 보류한다.
- finish     : 내부 관리번호 지우기·표 만들기·실행 기록 등 마무리(runner._finish).
"""
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from pmo.agent import guardrails, trace
from pmo.agent.llm import LLMError
from pmo.tools import call_tool


class AgentState(TypedDict, total=False):
    # 준비(runner.ask가 채움)
    service: Any
    client: Any
    spec: Any
    question: str
    focus: str | None
    history: list[dict] | None
    day: Any
    t0: float
    started: str
    allowed: Any
    tools: list[dict]
    texts: list[str]
    user_texts: list[str]
    max_steps: int
    # 처리 중
    messages: list[dict]
    calls: list[dict]
    tokens: dict
    answer: str
    error: str | None
    guardrail: str | None
    steps: int
    nudged: bool
    tool_nudged: bool
    reply: dict
    made_up: list[str]
    result: dict
    path: list[str]


def _visit(state: AgentState, node: str) -> list[str]:
    return list(state.get("path") or []) + [node]


def _runner():
    from pmo.agent import runner
    return runner


def precheck(state: AgentState) -> dict:
    r = _runner()
    service, spec, question = state["service"], state["spec"], state["question"]
    # v3 사전 검사: 지시 무시·관리하지 않는 정보·업무 범위 밖·다른 카테고리는 LLM 없이 정해진 방식으로 답한다.
    # 여러 업무 질문(라우터)의 각 Agent는 사전 검사를 다시 하지 않는다(v3.1 PC 평가 L04).
    early = None if state.get("focus") else guardrails.precheck(service, question, spec.key, r.LABELS,
                                                                 state.get("history"))
    if early:
        trace.log("GUARD", f"사전 검사: {early['kind']} → 정해진 답(LLM 호출 없음)")
        result = r._finish(service, state["client"], spec, question, state["t0"], state["started"], early["answer"],
                           [], None, state["tokens"], early["kind"])
        return {"result": result, "path": _visit(state, "precheck")}
    trace.reset_corrections()
    trace.log("AGENT", f"{spec.label} Agent 시작 · 도구 {len(state['tools'])}개 · "
                       f"{getattr(state['client'], 'name', 'llm')}"
                       + (" · 자기 몫만 답하라는 지시 포함" if state.get("focus") else ""))
    update = {"path": _visit(state, "precheck"), "steps": 0, "nudged": False, "tool_nudged": False}
    if spec.planned_notice:
        # 쓸 수 있는 도구가 모두 구현 예정인 Agent: 도구 상태만 확인해 기록하고 정해진 안내로 답한다.
        calls = list(state["calls"])
        for name in spec.tools or ():
            result = call_tool(service, name, {})
            calls.append({"tool": name, "arguments": {}, "ok": result["ok"],
                          "data": result.get("data"), "error": result.get("error")})
        update.update(calls=calls, max_steps=0, answer=spec.planned_notice)
    return update


def after_precheck(state: AgentState) -> str:
    if state.get("result"):
        return "end"
    return "llm" if state["max_steps"] > 0 else "verify"


def llm(state: AgentState) -> dict:
    steps = state["steps"] + 1
    try:
        reply = _runner()._chat(state["client"], state["messages"], state["tools"], state["tokens"])
    except LLMError as exc:
        return {"steps": steps, "error": str(exc), "reply": {}, "path": _visit(state, "llm")}
    return {"steps": steps, "reply": reply, "tokens": state["tokens"], "path": _visit(state, "llm")}


def after_llm(state: AgentState) -> str:
    if state.get("error"):
        return "verify"
    return "tools" if state["reply"]["tool_calls"] else "check_reply"


def tools(state: AgentState) -> dict:
    r, reply = _runner(), state["reply"]
    messages, calls = state["messages"], state["calls"]
    messages.append({"role": "assistant", "content": reply["content"],
                     "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                    for c in reply["tool_calls"]]})
    for call in reply["tool_calls"]:
        messages.append(r._execute(state["service"], call, state["allowed"], state["spec"], state["question"],
                                   state["user_texts"], state["texts"], state["day"], calls))
    return {"messages": messages, "calls": calls, "path": _visit(state, "tools")}


def _next_step(state: AgentState) -> str:
    return "llm" if state["steps"] < state["max_steps"] else "exhausted"


def check_reply(state: AgentState) -> dict:
    """도구 없이 답했을 때. 다시 시키면 answer를 비우고 llm으로, 답이 정해지면 verify로 간다(retry 표시)."""
    r = _runner()
    service, spec, question, reply = state["service"], state["spec"], state["question"], state["reply"]
    messages, calls, allowed = state["messages"], state["calls"], state["allowed"]
    answer = r._clean_answer(reply["content"])
    update = {"path": _visit(state, "check_reply")}
    people = guardrails.mentioned_people(service, question)
    if not calls and people and not state["tool_nudged"] and state["tools"] and not guardrails.is_redirect(answer):
        # v3.1 PC 평가 S07·H01·V01: 질문에 등록 인력이 있는데 도구 없이 답함 → 한 번 다시 시켜 도구로 확인하게 한다.
        trace.log("SELF-CORRECT", f"도구 없이 답함 → 다시 시킴(질문 속 인력 {', '.join(people)})")
        messages += [{"role": "assistant", "content": reply["content"]},
                     {"role": "system", "content": f"도구를 부르지 않고 답했다. 질문의 인력({', '.join(people)})은 등록된 인력이다. "
                                                   "맡은 도구로 먼저 조회하고 도구 결과로만 답하라."}]
        return dict(update, tool_nudged=True, guardrail="tool_retry", answer="", messages=messages, reply={"retry": True})
    if state.get("focus") and guardrails.is_redirect(answer):
        # 여러 업무 질문: 다른 Agent가 맡은 부분의 '다른 카테고리로 가라' 안내 문장만 뺀다(v3.1)
        return dict(update, answer=r._drop_redirect(answer) or answer)
    if allowed is not None and guardrails.is_redirect(answer) and not any(not c.get("forced") for c in calls):
        if guardrails.owner_of(question) == spec.key and not state["nudged"]:
            # 맡은 업무인데 다른 카테고리로 보냄(v2 평가 A02): 한 번 다시 시킨다. 질문에 등록 인력이 한 명이면
            # 인력 찾기를 코드가 먼저 실행해 결과를 쥐여 준다(v3 평가 S08 'MM'을 검수로 보냄).
            trace.log("SELF-CORRECT", "맡은 업무를 다른 카테고리로 보냄 → 다시 시킴")
            messages.append({"role": "assistant", "content": reply["content"]})
            if len(people) == 1 and (allowed is None or "find_people" in allowed):
                result = call_tool(service, "find_people", {"name": people[0]})
                calls.append({"tool": "find_people", "arguments": {"name": people[0]}, "ok": result["ok"],
                              "data": result.get("data"), "error": result.get("error"), "forced": True})
                messages += [{"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "find_people", "arguments": {"name": people[0]}}}]},
                    r._tool_message("find_people", result)]
            messages.append({"role": "system", "content": f"이 요청은 {spec.label} 업무다. 다른 카테고리로 안내하지 말고 "
                                                          "도구 결과로 답하거나 맡은 도구를 호출해 처리하라."})
            return dict(update, nudged=True, guardrail="redirect_retry", answer="", messages=messages, calls=calls,
                        reply={"retry": True})
        if guardrails.owner_of(question) == spec.key:
            return dict(update, answer=guardrails.ANSWERS["redirect_failed"].format(here=spec.label),
                        guardrail="redirect_failed")
        fixed = guardrails.fix_redirect(answer, question, spec.key, r.LABELS)
        if fixed != answer:
            return dict(update, answer=fixed, guardrail="redirect_fixed")
    return dict(update, answer=answer)


def after_check_reply(state: AgentState) -> str:
    return _next_step(state) if state["reply"].get("retry") else "verify"


def exhausted(state: AgentState) -> dict:
    update = {"path": _visit(state, "exhausted")}
    if not state.get("answer"):
        update["error"] = f"{state['max_steps']}번 안에 답을 마치지 못했습니다."
    return update


def _prepared(calls: list[dict]) -> bool:
    return any(c["ok"] and c["tool"].startswith("prepare_") for c in calls)


def verify(state: AgentState) -> dict:
    r, service = _runner(), state["service"]
    answer, calls = state.get("answer") or "", state["calls"]
    if _prepared(calls):
        fixed = r._not_saved_yet(answer)
        if fixed != answer:
            trace.log("SELF-CORRECT", "저장 전인데 '완료' 표현 → '준비를 마쳤습니다(아직 저장 전)'로 고침")
        answer = fixed
    made_up = []
    if answer and not state["spec"].planned_notice and state.get("error") is None:
        # 근거 없는 인력 이름이 답변에 있으면 rewrite로(v2 평가 C08 '김협력', v3 평가 C08·G02)
        made_up = guardrails.ungrounded_names_in_answer(service, answer, state["texts"], guardrails.seen_text(calls))
    return {"answer": answer, "made_up": made_up, "path": _visit(state, "verify")}


def after_verify(state: AgentState) -> str:
    return "rewrite" if state.get("made_up") else "finish"


def rewrite(state: AgentState) -> dict:
    r, service, made_up = _runner(), state["service"], state["made_up"]
    answer, calls, messages, error = state["answer"], state["calls"], state["messages"], state.get("error")
    trace.log("SELF-CORRECT", f"근거 없는 인력 이름 {', '.join(made_up)} → 다시 쓰게 함")
    messages += [{"role": "assistant", "content": answer},
                 {"role": "system", "content": f"답변의 인력({', '.join(made_up)})은 도구 결과에 없다. 인력 이름을 지어내지 말고, "
                                               "도구를 호출해 확인하거나 대상이 없으면 누구의 일인지 사용자에게 물어라."}]
    try:
        retry = r._continue(service, state["client"], messages, state["tools"], state["tokens"], calls,
                            state["allowed"], state["spec"], state["question"], state["user_texts"], state["texts"],
                            state["day"], max_steps=3)
    except LLMError as exc:
        retry, error = "", str(exc)
    if retry and not guardrails.ungrounded_names_in_answer(service, retry, state["texts"], guardrails.seen_text(calls)):
        answer, guardrail = retry, "hold_retry"
    else:
        answer, guardrail = guardrails.hold_answer(service, state["question"], made_up)
        trace.log("SELF-CORRECT", f"다시 써도 근거 없음 → 답변 보류({guardrail})")
    if _prepared(calls):
        answer = r._not_saved_yet(answer)
    return {"answer": answer, "guardrail": guardrail, "error": error, "messages": messages, "calls": calls,
            "path": _visit(state, "rewrite")}


def finish(state: AgentState) -> dict:
    result = _runner()._finish(state["service"], state["client"], state["spec"], state["question"], state["t0"],
                               state["started"], state.get("answer") or "", state["calls"], state.get("error"),
                               state["tokens"], state.get("guardrail"))
    return {"result": result, "path": _visit(state, "finish")}


def build_graph():
    graph = StateGraph(AgentState)
    for name, fn in (("precheck", precheck), ("llm", llm), ("tools", tools), ("check_reply", check_reply),
                     ("exhausted", exhausted), ("verify", verify), ("rewrite", rewrite), ("finish", finish)):
        graph.add_node(name, fn)
    graph.add_edge(START, "precheck")
    graph.add_conditional_edges("precheck", after_precheck, {"end": END, "llm": "llm", "verify": "verify"})
    graph.add_conditional_edges("llm", after_llm, {"tools": "tools", "check_reply": "check_reply", "verify": "verify"})
    graph.add_conditional_edges("tools", _next_step, {"llm": "llm", "exhausted": "exhausted"})
    graph.add_conditional_edges("check_reply", after_check_reply,
                                {"llm": "llm", "exhausted": "exhausted", "verify": "verify"})
    graph.add_edge("exhausted", "verify")
    graph.add_conditional_edges("verify", after_verify, {"rewrite": "rewrite", "finish": "finish"})
    graph.add_edge("rewrite", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


_GRAPH = None


def agent_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def run_agent(state: AgentState) -> dict:
    """runner.ask가 준비한 상태로 Agent 그래프를 실행하고 결과(runner._finish 형식)를 돌려준다."""
    state = dict(state, path=[], answer="", error=None, guardrail=None)
    # ReAct 반복이 길어질 수 있어 단계 한도(max_steps)에 맞춰 LangGraph 재귀 한도를 넉넉히 준다
    limit = 4 * max(state.get("max_steps") or 0, 1) + 20
    final = agent_graph().invoke(state, {"recursion_limit": limit})
    trace.log("GRAPH", f"{state['spec'].label} Agent 그래프: " + " → ".join(final.get("path") or []))
    return final["result"]
