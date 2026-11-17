"""라우터 멀티에이전트 처리 흐름을 LangGraph StateGraph로 실행한다(11/17 담당자: 평가 기준에 LangGraph 포함).

노드와 분기(판단 내용은 router.py·runner.py의 기존 함수를 그대로 부른다):

    START → prepare ─→ route ─┬─(되묻기: 투입/계약 모호)──→ clarify ──────────────→ END
                              └─(Agent 1개 이상)──────→ agent ⟲(남은 Agent가 있으면 다시 agent)
                                                         └→ merge ─┬─(다른 업무라고 답함)→ reroute ─→ finalize → END
                                                                   └──────────────────────────────→ finalize → END

- prepare : 되물음 대답('계약'·'투입')이면 원래 질문에 붙인다.
- route   : 사전 가드레일 + 키워드 점수(애매하면 LLM 분류)로 맡을 Agent와 방법을 정한다(router.route).
- clarify : 끝나는 날이 투입인지 계약인지 모르면 Agent를 부르지 않고 되묻는다(LLM 호출 없음).
- agent   : 전문 Agent를 한 번에 하나씩 부른다. 여러 업무 질문이면 Agent마다 자기 몫의 질문을 받고, 이 노드를 다시 돈다.
- merge   : 여러 Agent의 답을 Agent 이름 머리말로 합친다.
- reroute : 자동 배정된 Agent가 '다른 카테고리로 요청하라'고 답하면 맞는 Agent에게 한 번 다시 맡긴다.
- finalize: 토큰·시간·라우팅 정보를 정리하고 실행 기록(run_log)에 남긴다.

확인 카드(사람 승인)는 지금처럼 화면이 처리한다. 그래프는 계획만 돌려주고 저장하지 않는다.
"""
import time
from datetime import date, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from pmo.agent import router, trace
from pmo.agent.agents import AGENTS, AUTO
from pmo.config import TIMEZONE

TOKEN_FIELDS = ("prompt", "completion", "llm_calls")


class RoutedState(TypedDict, total=False):
    # 입력(실행 중 바뀌지 않음)
    service: Any
    client: Any
    history: list[dict] | None
    previous: list[str] | None
    today: date | None
    # 처리 중 채워지는 값
    question: str
    clarified: bool
    t0: float
    started: str
    tokens: dict
    decision: dict
    split: dict | None
    pending: list[str]
    parts: list[dict]
    result: dict
    path: list[str]


def _visit(state: RoutedState, node: str) -> list[str]:
    return list(state.get("path") or []) + [node]


def prepare(state: RoutedState) -> dict:
    question = state["question"]
    original = router.clarified_question(question, state.get("history"))
    if original:
        question = original          # 되물음 대답('계약')을 원래 질문에 붙여 처리한다
    trace.log("USER", trace.short(question, 90) + ("  (되물음 대답을 원래 질문에 붙임)" if original else ""))
    return {"question": question, "clarified": bool(original), "path": _visit(state, "prepare")}


def route(state: RoutedState) -> dict:
    service, question, tokens = state["service"], state["question"], dict(state["tokens"])
    timer = trace.Timer()
    decision = router.route(service, question, router.router_client(state["client"]), state.get("history"),
                            state.get("previous"), tokens, clarified=state.get("clarified", False))
    trace.log("ROUTER", router._route_text(decision, timer.ms))
    router._log_plan(service, question, decision)
    keys = list(decision["agents"])
    split = router.split_question(service, question, keys) if len(keys) > 1 else None
    return {"decision": decision, "tokens": tokens, "pending": keys, "parts": [], "split": split,
            "path": _visit(state, "route")}


def after_route(state: RoutedState) -> str:
    return "clarify" if state["decision"]["method"] == "clarify" else "agent"


def clarify(state: RoutedState) -> dict:
    from pmo.agent.runner import _finish, get_agent
    service, question, decision = state["service"], state["question"], state["decision"]
    result = _finish(service, state["client"], get_agent(AUTO), question, state["t0"], state["started"],
                     router.CLARIFY_END, [], None, state["tokens"], "clarify")
    result.update(agent_label=router.ROUTER_LABEL, routing=dict(decision, labels=[]))
    service.record_run("router", (question,), {"agents": [], "method": "clarify"}, True, "clarify", None,
                       state["t0"], state["started"])
    return {"result": result, "path": _visit(state, "clarify")}


def agent(state: RoutedState) -> dict:
    keys, pending = state["decision"]["agents"], list(state["pending"])
    key = pending.pop(0)
    part = router.ask_one(state["service"], state["question"], state["client"], key, keys, state.get("split"),
                          state.get("history"), state.get("today"))
    return {"pending": pending, "parts": list(state["parts"]) + [part], "path": _visit(state, f"agent:{key}")}


def after_agent(state: RoutedState) -> str:
    return "agent" if state["pending"] else "merge"


def merge(state: RoutedState) -> dict:
    return {"result": router.merge_parts(state["decision"]["agents"], state["parts"]),
            "path": _visit(state, "merge")}


def _reroute_target(state: RoutedState) -> str | None:
    keys = state["decision"]["agents"]
    if len(keys) != 1:
        return None
    return router.redirect_target(state["result"].get("answer") or "", keys[0])


def after_merge(state: RoutedState) -> str:
    return "reroute" if _reroute_target(state) else "finalize"


def reroute(state: RoutedState) -> dict:
    # 11/09: 자동 배정에서는 사용자가 카테고리를 고르지 않는다. 고른 Agent가 '다른 카테고리를 골라 다시 요청하라'고
    # 답하면 사용자에게 넘기지 않고 그 Agent에게 한 번 다시 맡긴다('12월 구매해야 하는 인력'을 인력관리가 받음).
    target, first, decision = _reroute_target(state), state["result"], state["decision"]
    result = router.ask_agents(state["service"], state["question"], state["client"], [target],
                               state.get("history"), state.get("today"))
    result["tokens"] = {f: result["tokens"][f] + first["tokens"][f] for f in TOKEN_FIELDS}
    result["duration_ms"] += first["duration_ms"]
    result["calls"] = first["calls"] + result["calls"]
    decision = dict(decision, agents=[target], rerouted_from=decision["agents"][0])
    trace.log("ROUTER", f"{AGENTS[decision['rerouted_from']].label} Agent가 다른 업무라고 답함 → "
                        f"{AGENTS[target].label} Agent에게 다시 맡김")
    return {"result": result, "decision": decision, "path": _visit(state, f"reroute:{target}")}


def finalize(state: RoutedState) -> dict:
    service, question, decision, tokens = state["service"], state["question"], state["decision"], state["tokens"]
    result = dict(state["result"])
    result["tokens"] = {f: result["tokens"][f] + tokens[f] for f in TOKEN_FIELDS}
    route_ms = round((time.perf_counter() - state["t0"]) * 1000) - result["duration_ms"]
    result["duration_ms"] += max(route_ms, 0)
    result["routing"] = dict(decision, labels=[AGENTS[k].label for k in decision["agents"]])
    service.record_run("router", (question,), {"agents": decision["agents"], "method": decision["method"]},
                       True, f"{'+'.join(decision['agents'])} ({decision['method']})", None,
                       state["t0"], state["started"])
    return {"result": result, "path": _visit(state, "finalize")}


def build_graph():
    graph = StateGraph(RoutedState)
    for name, fn in (("prepare", prepare), ("route", route), ("clarify", clarify), ("agent", agent),
                     ("merge", merge), ("reroute", reroute), ("finalize", finalize)):
        graph.add_node(name, fn)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "route")
    graph.add_conditional_edges("route", after_route, {"clarify": "clarify", "agent": "agent"})
    graph.add_edge("clarify", END)
    graph.add_conditional_edges("agent", after_agent, {"agent": "agent", "merge": "merge"})
    graph.add_conditional_edges("merge", after_merge, {"reroute": "reroute", "finalize": "finalize"})
    graph.add_edge("reroute", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


_GRAPH = None


def routed_graph():
    """한 번 만든 그래프를 다시 쓴다(노드는 상태만 받으므로 세션 사이에 공유해도 된다)."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def run_routed(service, question: str, client, history: list[dict] | None = None,
               previous: list[str] | None = None, today: date | None = None) -> dict:
    state: RoutedState = {
        "service": service, "client": client, "history": history, "previous": previous, "today": today,
        "question": question, "t0": time.perf_counter(), "started": datetime.now(TIMEZONE).isoformat(),
        "tokens": {f: 0 for f in TOKEN_FIELDS}, "path": [],
    }
    final = routed_graph().invoke(state)
    trace.log("GRAPH", "LangGraph 경로: " + " → ".join(final.get("path") or []))
    return final["result"]
