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
# 11/10 담당자: 끝나는 날을 묻는데 투입인지 구매계약인지 알 수 없으면 Agent를 부르기 전에 되묻는다(답변 채점 대상 아님)
CLARIFY_END = ("투입 종료일(과제에서 빠지는 날)을 말씀하시는 건가요, 구매계약 종료일을 말씀하시는 건가요? "
               "'투입' 또는 '계약'으로 답해 주세요.")
# 업무 말 없이 사람을 묻는 말('알파솔루션 사람 몇 명이야?'): 인력에 대한 정보는 인력관리(11/10 담당자 경계)
PEOPLE_WORDS = ("인력", "사람", "명단", "몇 명", "누구", "소속", "직원")
ROUTER_MODES = ("rule", "llm")

LONG_CHARS = 40
CLASSIFY_PROMPT = """너는 질문 분류기다. 사용자가 실제로 원하는 것을 보고, 처리할 업무 카테고리를 고른다. 설명 없이 카테고리 이름만 답한다.
- 인력관리: 인력에 대한 정보(이름·업체·분류·직무·계정 ID·소속), 투입 과제·투입 기간(투입 시작일·종료일)·매출귀속 프로젝트·등급·MM·매입단가,
  기간 MM 계산, 계정 ID 입력, 투입 변경·과제 이동·인력 교체
- 구매계약: 구매계약 번호·계약 기간(계약 종료일)·연장·종료 예정·만료 알림·구매 필요 인력, 계약 등록·수정·삭제, 알림 확인
- 검수: 돈 — 업체·계약번호의 월 검수 금액(원)·계약 총액·계약 금액
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
    # 문장 끝: 뒤에 공백·끝이 오는 마침표·물음표·느낌표, 줄바꿈. '0.5MM'·'lee.fake'·'C-2026.1' 안의 점은 문장 끝이 아니다.
    sentences = [x for x in re.split(r"[.?!。](?=\s|$)|\n", question) if x and x.strip()]
    if len(question) >= LONG_CHARS or len(sentences) >= 2:
        return "long"
    top = sorted((v for v in score.values() if v > 0), reverse=True)
    if not top:
        return "no_keyword"
    if len(top) >= 2 and top[0] - top[1] <= 1 and not multi:
        return "close"
    return None


def _about_people(service: Service, q: str) -> bool:
    """업무 말 없이 등록 인력이나 업체의 사람을 묻는 질문('이협력 누구야?', '알파솔루션 사람 몇 명이야?')."""
    if guardrails.mentioned_people(service, q):
        return True
    companies = {p.get("company") for p in service.people() if p.get("company")}
    return any(c in q for c in companies) and any(w in q for w in PEOPLE_WORDS)


def router_mode() -> str:
    """판정 방식(환경변수 PMO_ROUTER_MODE): rule(키워드, 애매하면 LLM — 기본) · llm(가드레일 뒤 항상 LLM 분류)."""
    import os
    mode = os.environ.get("PMO_ROUTER_MODE", "rule").strip().lower()
    return mode if mode in ROUTER_MODES else "rule"


def router_client(client: ChatClient | None) -> ChatClient | None:
    """판정(카테고리 분류)용 LLM. PMO_ROUTER_MODEL을 주면 답변 모델과 다른 모델로 판정한다(예: qwen3:14b)."""
    import os
    model = os.environ.get("PMO_ROUTER_MODEL", "").strip()
    if not model or client is None or getattr(client, "model", None) == model:
        return client
    from pmo.agent.llm import OllamaClient
    return OllamaClient(model=model)


def route(service: Service, question: str, client: ChatClient | None = None,
          history: list[dict] | None = None, previous: list[str] | None = None,
          tokens: dict | None = None, mode: str | None = None, clarified: bool = False) -> dict:
    """{"agents": [Agent 키…], "method": "precheck"|"follow_up"|"clarify"|"keyword"|"people"|"multi"|"llm"|"fallback",
    "scores": {...}}. clarify는 Agent를 부르지 않고 되묻는다(agents는 빈 목록).

    mode: rule(기본) · llm(가드레일·이어지는 대화·되묻기 뒤에는 항상 LLM 분류, 판정 비교용). 없으면 PMO_ROUTER_MODE.
    clarified: 되물음에 대한 대답을 붙인 질문이면 다시 되묻지 않는다.
    """
    tokens = tokens if tokens is not None else {"prompt": 0, "completion": 0, "llm_calls": 0}
    mode = mode or router_mode()
    q = question.strip()
    labels = {k: a.label for k, a in AGENTS.items()}
    if guardrails.precheck(service, q, AUTO, labels, history):
        return {"agents": [AUTO], "method": "precheck", "scores": {}}
    score = guardrails.scores(q)
    positive = [k for k in SPECIALISTS if score.get(k, 0) > 0]
    unclear_end = not clarified and guardrails.end_date_unclear(q)
    # 11/09: 등록 인력 이름과 업무 말이 함께 있는 짧은 질문('이협력 투입 중인 과제 뭐야?')은 되물음의 대답이 아니라
    # 새 질문이다. 되물음 대답('감마테크 김협력')처럼 업무 말이 없으면 그대로 이어 받는다.
    # 11/10: 끝나는 날을 묻는 말('이협력 종료일 12월 31일로 바꿔줘')도 업무 말로 본다(투입·계약 되묻기).
    new_question = (bool(positive) or unclear_end) and bool(guardrails.mentioned_people(service, q))
    if history and previous and len(q) <= FOLLOW_UP_CHARS and len(positive) <= 1 and not new_question \
            and (not positive or positive[0] in previous):
        return {"agents": list(previous), "method": "follow_up", "scores": score}
    if unclear_end:
        # 11/10 담당자: 투입 종료일인지 구매계약 종료일인지 알 수 없으면 먼저 되묻는다
        return {"agents": [], "method": "clarify", "scores": score}
    # 규칙 판정(빠른 길)
    multi = len(positive) >= 2 and bool(JOINERS.search(q))
    if multi:
        order = _positions(q)
        rule = {"agents": sorted(positive, key=lambda k: order.get(k, 999)), "method": "multi", "scores": score}
    else:
        best = guardrails.owner_of(q)
        if best in SPECIALISTS:
            rule = {"agents": [best], "method": "keyword", "scores": score}
        elif not positive and _about_people(service, q):
            rule = {"agents": ["staff"], "method": "people", "scores": score}   # 인력에 대한 정보 → 인력관리
        else:
            rule = {"agents": [AUTO], "method": "fallback", "scores": score}
    reason = (hard_to_judge(q, score, multi) or (None if rule["method"] != "fallback" else "tie")
              if rule["method"] != "people" or len(q) >= LONG_CHARS else None)
    if mode == "llm":
        reason = reason or "mode_llm"
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


_ASKING = re.compile(r"알려|보여|얼마|언제|누구|뭐|무엇|어디|확인|\?")


def split_question(service: Service, question: str, keys: list[str]) -> dict[str, str]:
    """여러 업무 질문을 연결 말로 나눠 Agent마다 자기 몫의 질문을 만든다(v3.1 PC 평가 M01·M02: 두 Agent가 같은 답을 반복).

    '이협력 지금 투입 과제랑 구매계약 종료일 알려줘' → 인력관리 '이협력 지금 투입 과제 알려줘', 구매계약 '이협력 구매계약 종료일 알려줘'.
    조각마다 키워드 1등 업무에 붙이고, 질문 속 인력 이름이 빠진 조각에는 이름을, 묻는 말이 없는 조각에는 끝 조각의 묻는 말을 붙인다.
    Agent마다 조각이 하나 이상 나오지 않으면 빈 사전(원래 질문 + 지시를 그대로 쓴다).
    """
    pieces = [p.strip(" ,") for p in JOINERS.split(question) if p and p.strip(" ,") and not JOINERS.fullmatch(p)]
    owned: dict[str, list[str]] = {k: [] for k in keys}
    for piece in pieces:
        score = guardrails.scores(piece)
        best = max(keys, key=lambda k: score.get(k, 0))
        if score.get(best, 0) > 0:
            owned[best].append(piece)
    if any(not v for v in owned.values()):
        return {}
    names = guardrails.mentioned_people(service, question)
    ending = next((m.group(0) for m in [re.search(r"(알려\S*|보여\S*|\S*\?)$", pieces[-1])] if m), "알려줘")
    out = {}
    for key, parts in owned.items():
        text = " ".join(parts)
        missing = [n for n in names if n not in text]
        if missing:
            text = " ".join(missing) + " " + text
        if not _ASKING.search(text):
            text += " " + ending
        out[key] = text
    return out


def ask_agents(service: Service, question: str, client: ChatClient, keys: list[str],
               history: list[dict] | None = None, today: date | None = None) -> dict:
    """정해진 Agent들에게 차례로 묻고 답을 합친다(Agent가 하나면 그 결과 그대로)."""
    from pmo.agent.runner import ask
    if len(keys) == 1:
        return ask(service, question, client, today=today, history=history, agent=keys[0])
    split = split_question(service, question, keys)
    parts = [ask(service, split.get(k, question), client, today=today, history=history, agent=k,
                 focus=focus_note(k, keys)) for k in keys]
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


def redirect_target(answer: str, current: str) -> str | None:
    """답이 '다른 카테고리를 골라 다시 요청하라'는 안내면 그 카테고리(전문 Agent 키). 아니면 None."""
    if not guardrails.is_redirect(answer):
        return None
    named = guardrails.NAMED_CATEGORY.search(answer or "")
    key = _LABEL_TO_KEY.get(named.group(1)) if named else None
    return key if key and key != current else None


def clarified_question(question: str, history: list[dict] | None) -> str | None:
    """직전 답이 투입·계약 되묻기였고 이번 질문이 짧은 대답이면 '원래 질문 (대답)'. 아니면 None."""
    msgs = [m for m in (history or []) if m.get("role") in ("user", "assistant")]
    if len(msgs) < 2 or msgs[-1]["role"] != "assistant" or msgs[-1].get("content") != CLARIFY_END:
        return None
    reply = question.strip()
    if len(reply) > FOLLOW_UP_CHARS or msgs[-2]["role"] != "user":
        return None
    return f"{msgs[-2]['content']} ({reply} 기준)"


def ask_routed(service: Service, question: str, client: ChatClient, history: list[dict] | None = None,
               previous: list[str] | None = None, today: date | None = None) -> dict:
    """라우터로 Agent를 고르고 처리한다. 결과에 routing({"agents", "method", "scores", "labels"})을 붙인다."""
    t0, started = time.perf_counter(), datetime.now(TIMEZONE).isoformat()
    tokens = {"prompt": 0, "completion": 0, "llm_calls": 0}
    original = clarified_question(question, history)
    if original:
        question = original          # 되물음 대답('계약')을 원래 질문에 붙여 처리한다
    decision = route(service, question, router_client(client), history, previous, tokens, clarified=bool(original))
    if decision["method"] == "clarify":
        from pmo.agent.runner import _finish, get_agent
        result = _finish(service, client, get_agent(AUTO), question, t0, started, CLARIFY_END, [], None, tokens, "clarify")
        result.update(agent_label=ROUTER_LABEL, routing=dict(decision, labels=[]))
        service.record_run("router", (question,), {"agents": [], "method": "clarify"}, True, "clarify", None, t0, started)
        return result
    result = ask_agents(service, question, client, decision["agents"], history, today)
    if len(decision["agents"]) == 1:
        # 11/09: 자동 배정에서는 사용자가 카테고리를 고르지 않는다. 고른 Agent가 '다른 카테고리를 골라 다시 요청하라'고
        # 답하면 사용자에게 넘기지 않고 그 Agent에게 한 번 다시 맡긴다('12월 구매해야 하는 인력'을 인력관리가 받음).
        target = redirect_target(result.get("answer") or "", decision["agents"][0])
        if target:
            first = result
            result = ask_agents(service, question, client, [target], history, today)
            result["tokens"] = {f: result["tokens"][f] + first["tokens"][f] for f in tokens}
            result["duration_ms"] += first["duration_ms"]
            result["calls"] = first["calls"] + result["calls"]
            decision = dict(decision, agents=[target], rerouted_from=decision["agents"][0])
    result["tokens"] = {f: result["tokens"][f] + tokens[f] for f in tokens}
    route_ms = round((time.perf_counter() - t0) * 1000) - result["duration_ms"]
    result["duration_ms"] += max(route_ms, 0)
    result["routing"] = dict(decision, labels=[AGENTS[k].label for k in decision["agents"]])
    service.record_run("router", (question,), {"agents": decision["agents"], "method": decision["method"]},
                       True, f"{'+'.join(decision['agents'])} ({decision['method']})", None, t0, started)
    return result
