"""v3.1 자동 라우터 멀티에이전트: 질문을 보고 맞는 전문 Agent를 고른다(11/04·11/08 담당자).

사용자가 카테고리를 고르지 않아도 질문에 맞는 Agent가 처리한다.
1. 사전 검사(가드레일)에 걸리는 질문(범위 밖·지시 무시 등)은 라우팅하지 않고 정해진 문장으로 답한다.
2. 되물음에 대한 짧은 대답은 직전 질문을 처리한 Agent가 이어서 받는다.
3. 키워드 점수(guardrails.OWNER_KEYWORDS)로 판정한다.
   - 한 업무만 점수가 있거나 1등이 하나면 그 Agent.
   - 두 업무 이상에 점수가 있고 '그리고·와/과·랑·,' 같은 연결 말이 있으면 여러 업무 질문 → Agent를 차례로 불러 답을 합친다.
4. 키워드로 정하지 못하면(점수 없음·동점) LLM에 카테고리만 묻는다(도구 없이 한 번). 그래도 모르면 자동(전체) 단일 Agent.
5. 11/08 담당자: 짧고 분명한 질문은 규칙으로 바로, 길게 쓴 질문(40자 이상·문장 둘 이상)이나 판단하기 어려운 질문
   (1·2등 점수 차 1 이하, 긴 여러 업무 질문)은 LLM이 맥락(직전 대화 포함)을 보고 고른다. LLM 답을 알아들을 수 없으면
   규칙 판정을 그대로 쓴다.

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

LONG_CHARS = 40
CLASSIFY_PROMPT = """너는 질문 분류기다. 사용자가 실제로 원하는 것을 보고, 처리할 업무 카테고리를 고른다. 설명 없이 카테고리 이름만 답한다.
- 인력관리: 인력 찾기, 투입 과제·기간·매출귀속 프로젝트·등급·MM 조회, 기간 MM 계산, 계정 ID 입력, 투입 변경·과제 이동·인력 교체
- 구매계약: 구매계약 기간·종료 예정·만료 알림 조회, 계약 등록·수정·삭제, 알림 확인
- 검수: 업체·계약번호의 월 검수 금액(원)·계약 총액
- 없음: 위 업무가 아님
질문 속에 다른 업무의 단어가 나와도 사용자가 원하는 업무만 고른다("과제 말고 계약이 궁금해" → 구매계약).
정말로 두 업무를 함께 물었을 때만 질문에 나온 순서로 쉼표로 이어 답한다(예: 인력관리, 구매계약).
답: 인력관리 / 구매계약 / 검수 / 없음"""
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


def classify_with_llm(client: ChatClient, question: str, tokens: dict,
                      history: list[dict] | None = None) -> list[str] | None:
    """LLM에 카테고리만 묻는다(도구 없음). 고른 업무 목록(답에 나온 순서), '없음'이면 [], 알아들을 수 없으면 None."""
    context = ""
    if history:     # 직전 대화 한 쌍만 맥락으로(되물음에 이어지는 긴 대답)
        last = [m for m in history if m.get("role") in ("user", "assistant")][-2:]
        context = "직전 대화:\n" + "\n".join(f"{'사용자' if m['role'] == 'user' else 'Agent'}: {m['content'][:200]}"
                                           for m in last) + "\n\n분류할 질문:\n"
    reply = client.chat([{"role": "system", "content": CLASSIFY_PROMPT},
                         {"role": "user", "content": context + question}], [])
    usage = reply.get("usage") or {}
    tokens["llm_calls"] += 1
    tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
    tokens["completion"] += int(usage.get("completion_tokens") or 0)
    text = reply.get("content") or ""
    found = sorted((text.find(label), key) for label, key in _LABEL_TO_KEY.items() if label in text)
    if found:
        return [key for _, key in found]
    return [] if "없음" in text else None


def hard_to_judge(question: str, score: dict[str, int], multi: bool) -> str | None:
    """규칙만으로 고르기 어려운 질문이면 그 이유(LLM 분류 대상). 짧고 분명하면 None."""
    # 문장 끝: 마침표·물음표·느낌표·줄바꿈. '0.5MM'의 소수점은 문장 끝이 아니다.
    sentences = [x for x in re.split(r"(?<!\d)[.?!。](?!\d)|\n", question) if x and x.strip()]
    if len(question) >= LONG_CHARS or len(sentences) >= 2:
        return "long"
    top = sorted((v for v in score.values() if v > 0), reverse=True)
    if not top:
        return "no_keyword"
    if len(top) >= 2 and top[0] - top[1] <= 1 and not multi:
        return "close"
    return None


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
    # 규칙 판정(빠른 길)
    multi = len(positive) >= 2 and bool(JOINERS.search(q))
    if multi:
        order = _positions(q)
        rule = {"agents": sorted(positive, key=lambda k: order.get(k, 999)), "method": "multi", "scores": score}
    else:
        best = guardrails.owner_of(q)
        rule = ({"agents": [best], "method": "keyword", "scores": score} if best in SPECIALISTS
                else {"agents": [AUTO], "method": "fallback", "scores": score})
    reason = hard_to_judge(q, score, multi) or (None if rule["method"] != "fallback" else "tie")
    if reason is None or client is None:
        return rule
    # 길거나 판단하기 어려운 질문: LLM이 맥락을 보고 고른다. 알아들을 수 없으면 규칙 판정
    try:
        picked = classify_with_llm(client, q, tokens, history)
    except LLMError:
        picked = None
    if picked:
        return {"agents": picked[:3], "method": "llm", "scores": score, "reason": reason, "rule": rule["agents"]}
    return dict(rule, reason=reason)       # '없음'·알아들을 수 없는 답 → 규칙 판정(사전 검사는 이미 통과)


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
