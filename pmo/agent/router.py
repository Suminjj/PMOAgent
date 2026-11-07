"""v3.1 자동 라우터 멀티에이전트: 질문을 보고 맞는 전문 Agent를 고른다(11/04·11/08 담당자).

사용자가 카테고리를 고르지 않아도 질문에 맞는 Agent가 처리한다.
1. 사전 검사(가드레일)에 걸리는 질문(범위 밖·지시 무시 등)은 라우팅하지 않고 정해진 문장으로 답한다.
2. 되물음에 대한 짧은 대답은 직전 질문을 처리한 Agent가 이어서 받는다.
3. 키워드 점수(guardrails.OWNER_KEYWORDS)로 판정한다.
   - 한 업무만 점수가 있거나 1등이 하나면 그 Agent.
   - 두 업무 이상에 점수가 있고 '그리고·와/과·랑·,' 같은 연결 말이 있으면 여러 업무 질문 → Agent를 차례로 불러 답을 합친다.
4. 키워드로 정하지 못하면(점수 없음·동점) LLM에 카테고리만 묻는다(도구 없이 한 번). 그래도 모르면 자동(전체) 단일 Agent.

라우팅 결과(고른 Agent·방법·점수)는 답변과 함께 돌려주고 화면·평가·실행 기록에 남긴다.
"""
import re
import time
from datetime import date, datetime

from pmo.agent import guardrails
from pmo.agent.agents import AGENTS, AUTO
from pmo.agent.llm import ChatClient, LLMError
from pmo.config import TIMEZONE
from pmo.service import Service

SPECIALISTS = ("staff", "contract", "inspection")
ROUTER_LABEL = "자동 배정"
# 여러 업무를 한 문장에 묻는 연결 말. '과제'의 '과'처럼 낱말 안의 글자는 띄어쓰기로 거른다.
JOINERS = re.compile(r"그리고|및|하고\s|이랑\s|랑\s|,|와\s|과\s|도\s*(같이|함께)|또한?\s|이고\s|이며\s")
FOLLOW_UP_CHARS = 20

CLASSIFY_PROMPT = """너는 질문 분류기다. 아래 질문을 처리할 업무 카테고리 하나를 고른다. 설명 없이 카테고리 이름만 답한다.
- 인력관리: 인력 찾기, 투입 과제·기간·매출귀속 프로젝트·등급·MM 조회, 기간 MM 계산, 계정 ID 입력, 투입 변경·과제 이동·인력 교체
- 구매계약: 구매계약 기간·종료 예정·만료 알림 조회, 계약 등록·수정·삭제, 알림 확인
- 검수: 업체·계약번호의 월 검수 금액(원)·계약 총액
- 없음: 위 업무가 아님
답: 인력관리 / 구매계약 / 검수 / 없음 중 하나"""
_LABEL_TO_KEY = {AGENTS[k].label: k for k in SPECIALISTS}


def _positions(question: str) -> dict[str, int]:
    """업무별로 질문에서 키워드가 처음 나오는 위치(여러 업무 질문의 답 순서)."""
    q = question.casefold()
    found = {}
    for key, words in guardrails.OWNER_KEYWORDS.items():
        hits = [q.find(w) for w in words if w in q]
        if hits:
            found[key] = min(hits)
    return found


def classify_with_llm(client: ChatClient, question: str, tokens: dict) -> str | None:
    """키워드로 정하지 못한 질문만 LLM에 카테고리를 묻는다. 고르지 못하면 None."""
    reply = client.chat([{"role": "system", "content": CLASSIFY_PROMPT},
                         {"role": "user", "content": question}], [])
    usage = reply.get("usage") or {}
    tokens["llm_calls"] += 1
    tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
    tokens["completion"] += int(usage.get("completion_tokens") or 0)
    text = reply.get("content") or ""
    hits = [key for label, key in _LABEL_TO_KEY.items() if label in text]
    return hits[0] if len(hits) == 1 else None


def route(service: Service, question: str, client: ChatClient | None = None,
          history: list[dict] | None = None, previous: list[str] | None = None,
          tokens: dict | None = None) -> dict:
    """{"agents": [Agent 키…], "method": "precheck"|"follow_up"|"keyword"|"multi"|"llm"|"fallback", "scores": {...}}"""
    tokens = tokens if tokens is not None else {"prompt": 0, "completion": 0, "llm_calls": 0}
    q = question.strip()
    labels = {k: a.label for k, a in AGENTS.items()}
    if guardrails.precheck(service, q, AUTO, labels, history):
        return {"agents": [AUTO], "method": "precheck", "scores": {}}
    score = guardrails.scores(q)
    positive = [k for k in SPECIALISTS if score.get(k, 0) > 0]
    if history and previous and len(q) <= FOLLOW_UP_CHARS and len(positive) <= 1 \
            and (not positive or positive[0] in previous):
        return {"agents": list(previous), "method": "follow_up", "scores": score}
    if len(positive) >= 2 and JOINERS.search(q):
        order = _positions(q)
        return {"agents": sorted(positive, key=lambda k: order.get(k, 999)), "method": "multi", "scores": score}
    best = guardrails.owner_of(q)
    if best in SPECIALISTS:
        return {"agents": [best], "method": "keyword", "scores": score}
    if client is not None:
        try:
            picked = classify_with_llm(client, q, tokens)
        except LLMError:
            picked = None
        if picked:
            return {"agents": [picked], "method": "llm", "scores": score}
    return {"agents": [AUTO], "method": "fallback", "scores": score}


def focus_note(key: str, keys: list[str]) -> str:
    """여러 업무 질문에서 한 Agent가 자기 부분만 답하게 하는 지시."""
    others = ", ".join(AGENTS[k].label for k in keys if k != key)
    return (f"이 질문은 여러 업무에 걸쳐 있어 Agent 여럿이 나눠 답한다. 너는 {AGENTS[key].label} 업무 부분만 도구로 확인해 답한다. "
            f"{others} 업무 부분은 다른 Agent가 답하므로 답하지도, 다른 카테고리로 안내하지도 않는다.")


def ask_agents(service: Service, question: str, client: ChatClient, keys: list[str],
               history: list[dict] | None = None, today: date | None = None) -> dict:
    """정해진 Agent들에게 차례로 묻고 답을 합친다(Agent가 하나면 그 결과 그대로)."""
    from pmo.agent.runner import ask
    if len(keys) == 1:
        return ask(service, question, client, today=today, history=history, agent=keys[0])
    parts = [ask(service, question, client, today=today, history=history, agent=k, focus=focus_note(k, keys))
             for k in keys]
    answer = "\n\n".join(f"**{AGENTS[k].label}**\n\n{p['answer'] or p['error'] or ''}".strip()
                         for k, p in zip(keys, parts))
    errors = [p["error"] for p in parts if p["error"]]
    tokens = {f: sum(p["tokens"][f] for p in parts) for f in ("prompt", "completion", "llm_calls")}
    return {"ok": all(p["ok"] for p in parts), "answer": answer,
            "calls": [c for p in parts for c in p["calls"]],
            "error": "; ".join(errors) or None, "duration_ms": sum(p["duration_ms"] for p in parts),
            "model": parts[0]["model"], "tokens": tokens,
            "agent": "+".join(keys), "agent_label": " + ".join(AGENTS[k].label for k in keys),
            "guardrail": next((p["guardrail"] for p in parts if p["guardrail"]), None)}


def ask_routed(service: Service, question: str, client: ChatClient, history: list[dict] | None = None,
               previous: list[str] | None = None, today: date | None = None) -> dict:
    """라우터로 Agent를 고르고 처리한다. 결과에 routing({"agents", "method", "scores", "labels"})을 붙인다."""
    t0, started = time.perf_counter(), datetime.now(TIMEZONE).isoformat()
    tokens = {"prompt": 0, "completion": 0, "llm_calls": 0}
    decision = route(service, question, client, history, previous, tokens)
    result = ask_agents(service, question, client, decision["agents"], history, today)
    result["tokens"] = {f: result["tokens"][f] + tokens[f] for f in tokens}
    route_ms = round((time.perf_counter() - t0) * 1000) - result["duration_ms"]
    result["duration_ms"] += max(route_ms, 0)
    result["routing"] = dict(decision, labels=[AGENTS[k].label for k in decision["agents"]])
    service.record_run("router", (question,), {"agents": decision["agents"], "method": decision["method"]},
                       True, f"{'+'.join(decision['agents'])} ({decision['method']})", None, t0, started)
    return result
