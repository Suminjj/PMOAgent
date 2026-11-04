"""Agent 실행 루프: LLM이 도구를 고르고, 도구 계층이 업무 함수를 실행한다.

agent(pmo/agent/agents.py)를 주면 그 카테고리의 도구·업무 지침만 쓴다. 없으면 자동(전체) 단일 Agent.
"""
import json
import re
import time
from datetime import date, datetime

from pmo.agent.llm import ChatClient, LLMError
from pmo.agent import guardrails
from pmo.agent.agents import AGENTS, AgentSpec, allowed_tools, get_agent, system_prompt
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
    "prepare_assignment_update": {"revenue_project": "매출귀속 프로젝트", "grade": "등급", "purchase_rate": "매입단가"},
    "prepare_assignment_transfer": {"revenue_project": "매출귀속 프로젝트", "purchase_rate": "매입단가"},
    "prepare_assignment_replace": {"grade": "등급", "revenue_project": "매출귀속 프로젝트", "purchase_rate": "매입단가"},
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
        # 코드 앞부분(30133686-)·금액 쉼표를 붙이거나 뗀 경우도 사용자 값으로 본다
        forms = {value, value.split("-")[-1], value.replace(",", "")}
        if value in names or not any(f.casefold() in t.casefold().replace(",", "") for f in forms for t in texts):
            return (f"{label} '{value}'은(는) 사용자가 말한 값이 아닙니다. 값을 지어내지 마세요. "
                    "사용자가 요청한 작업이 이 도구로 할 일이 맞는지 다시 보고, 맞으면 값을 물어보세요.")
    return None


def _clean_answer(answer: str) -> str:
    """작은 모델이 답변에 도구 이름·내부 표기를 옮겨 적으면 사용자용 말로 바꾼다."""
    for name in sorted(TOOL_LABELS, key=len, reverse=True):
        answer = answer.replace(f"`{name}`", f"'{TOOL_LABELS[name]}'").replace(name, f"'{TOOL_LABELS[name]}'")
    answer = re.sub(r"`?prepare_\*`?\s*도구", "변경 준비 기능", answer)
    # Agent는 계정 ID를 발급하지 않고 입력만 한다(v3 평가 A01 '계정 ID를 발급하려면')
    answer = re.sub(r"계정 ID(를|를 새로)? 발급(하시려면|하려면|을 진행하시려면)", "발급된 계정 ID를 입력하려면", answer)
    return re.sub(r"`?ok\s*:\s*false`?", "실패", answer)


_DONE_WORDS = re.compile(r"(변경|저장|삭제|등록|수정|입력|반영|교체|이동)(되었습니다|됐습니다|했습니다)")
# v3: '확인 처리가 완료되었습니다'(v2 평가 K01)처럼 '완료·처리'로 말하는 경우
_DONE_PHRASE = re.compile(r"\s*(?:처리)?\s*(?:가|를|이|을)?\s*(?:완료(?:되었|됐|했)|처리(?:되었|됐|했))습니다")


def _not_saved_yet(answer: str) -> str:
    """확인 카드만 만든 턴에 '변경되었습니다'처럼 저장된 것처럼 말하면 고친다(저장은 사용자가 확인해야 함)."""
    answer = _DONE_WORDS.sub(r"\1 준비를 마쳤습니다(아직 저장 전)", answer)
    return _DONE_PHRASE.sub(" 준비를 마쳤습니다(아직 저장 전)", answer)


LABELS = {key: spec.label for key, spec in AGENTS.items()}


def _finish(service, client, spec, question, t0, started, answer, calls, error, tokens, guardrail=None) -> dict:
    duration = round((time.perf_counter() - t0) * 1000)
    ok = error is None and bool(answer)
    if error is None and not answer:
        error = "LLM이 빈 답변을 돌려주었습니다."
    note = f"도구 {len(calls)}회 · 토큰 {tokens['prompt']}+{tokens['completion']}"
    service.record_run(f"agent:{getattr(client, 'name', 'llm')}", (question,), {"agent": spec.key}, ok,
                       (note + (f" · 가드레일 {guardrail}" if guardrail else "")) if ok else None,
                       error, t0, started)
    return {"ok": ok, "answer": answer, "calls": calls, "error": error,
            "duration_ms": duration, "model": getattr(client, "name", "llm"), "tokens": tokens,
            "agent": spec.key, "agent_label": spec.label, "guardrail": guardrail}


def _execute(service, call, allowed, spec, question, user_texts, texts, day, calls) -> dict:
    """도구 호출 하나를 가드레일을 거쳐 실행하고 calls에 남긴다. LLM에게 돌려줄 tool 메시지를 반환한다."""
    args = guardrails.tidy_args(call["name"], call["arguments"] or {}, question, user_texts, day)
    call["arguments"] = _fill_person(service, call["name"], args, user_texts)
    problem = (guardrails.missing_target(call["name"], call["arguments"])
               or _ungrounded(service, call["name"], call["arguments"], user_texts)
               or guardrails.ungrounded_person(service, call["name"], call["arguments"], texts,
                                               guardrails.seen_text(calls)))
    blocked = True
    if allowed is not None and call["name"] not in allowed:
        result = {"ok": False, "tool": call["name"],
                  "error": f"{spec.label} Agent가 쓸 수 없는 도구입니다. 맡은 업무 밖이면 다른 카테고리를 안내하세요."}
    elif problem:
        result = {"ok": False, "tool": call["name"], "error": problem}
    else:
        result, blocked = call_tool(service, call["name"], call["arguments"]), False
    # blocked: 코드가 막아 실행하지 않은 호출(평가의 도구 순서 채점에서 뺀다)
    calls.append({"tool": call["name"], "arguments": call["arguments"], "ok": result["ok"],
                  "data": result.get("data"), "error": result.get("error"),
                  **({"blocked": True} if blocked else {})})
    return _tool_message(call["name"], result)


def _continue(service, client, messages, tools, tokens, calls, allowed, spec, question, user_texts, texts, day,
              max_steps: int) -> str:
    """가드레일이 다시 시킬 때 쓰는 짧은 실행 루프. 최종 답변(없으면 빈 문자열)을 돌려준다."""
    for _ in range(max_steps):
        reply = client.chat(messages, tools)
        usage = reply.get("usage") or {}
        tokens["llm_calls"] += 1
        tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
        tokens["completion"] += int(usage.get("completion_tokens") or 0)
        if not reply["tool_calls"]:
            return _clean_answer(reply["content"])
        messages.append({"role": "assistant", "content": reply["content"],
                         "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                        for c in reply["tool_calls"]]})
        for call in reply["tool_calls"]:
            messages.append(_execute(service, call, allowed, spec, question, user_texts, texts, day, calls))
    return ""


def ask(service: Service, question: str, client: ChatClient,
        today: date | None = None, max_steps: int = MAX_STEPS,
        history: list[dict] | None = None, agent: AgentSpec | str | None = None) -> dict:
    """질문 하나를 처리한다. 예외 대신 결과 객체로 성공·실패를 돌려준다.

    history: 이전 대화 [{"role": "user"|"assistant", "content": str}] (최근 MAX_HISTORY개만 사용)
    agent: 전문 Agent(AgentSpec 또는 키). 없으면 자동(전체)
    반환: {"ok", "answer", "calls": [{"tool","arguments","ok","data","error"}], "error", "duration_ms",
          "tokens": {"prompt", "completion", "llm_calls"} (LLM 호출 전체 합),
          "model", "agent", "agent_label"}
    """
    t0, started = time.perf_counter(), datetime.now(TIMEZONE).isoformat()
    day = today or service.clock()
    spec = agent if isinstance(agent, AgentSpec) else get_agent(agent)
    allowed = allowed_tools(spec)
    # 자동(전체)에는 '구현 예정' 도구를 주지 않는다: 이름이 비슷한 검수 금액 도구를 매출귀속 프로젝트 조회에 고르던 문제
    # (v1·v1.2 평가 R01). 구현 예정 기능은 해당 전문 Agent(검수·구매계약)가 정해진 안내로 답한다.
    tools = tool_specs(include_planned=allowed is not None, names=allowed)
    messages = [{"role": "system", "content": system_prompt(spec, day.isoformat())},
                *[{"role": m["role"], "content": str(m["content"])}
                  for m in (history or [])[-MAX_HISTORY:] if m.get("role") in ("user", "assistant")],
                {"role": "user", "content": question}]
    calls: list[dict] = []
    answer, error = "", None
    tokens = {"prompt": 0, "completion": 0, "llm_calls": 0}
    # v3 사전 검사: 지시 무시·관리하지 않는 정보·업무 범위 밖·다른 카테고리·검수 금액은 LLM 없이 정해진 방식으로 답한다
    early = guardrails.precheck(service, question, spec.key, LABELS, history)
    if early:
        answer = early["answer"] or AGENTS["inspection"].planned_notice
        return _finish(service, client, spec, question, t0, started, answer, [], None, tokens, early["kind"])
    texts = [question] + [str(m.get("content") or "") for m in reversed(history or [])]
    guardrail = None
    if spec.planned_notice:
        # 쓸 수 있는 도구가 모두 구현 예정인 Agent: 도구 상태만 확인해 기록하고 정해진 안내로 답한다.
        for name in spec.tools or ():
            result = call_tool(service, name, {})
            calls.append({"tool": name, "arguments": {}, "ok": result["ok"],
                          "data": result.get("data"), "error": result.get("error")})
        max_steps, answer = 0, spec.planned_notice
    user_texts = [question] + [str(m.get("content") or "") for m in reversed(history or []) if m.get("role") == "user"]
    nudged = False
    try:
        steps = 0
        while steps < max_steps:
            steps += 1
            reply = client.chat(messages, tools)
            usage = reply.get("usage") or {}
            tokens["llm_calls"] += 1
            tokens["prompt"] += int(usage.get("prompt_tokens") or 0)
            tokens["completion"] += int(usage.get("completion_tokens") or 0)
            if not reply["tool_calls"]:
                answer = _clean_answer(reply["content"])
                if allowed is not None and guardrails.is_redirect(answer) and not any(not c.get("forced") for c in calls):
                    if guardrails.owner_of(question) == spec.key and not nudged:
                        # 맡은 업무인데 다른 카테고리로 보냄(v2 평가 A02 계정 ID 입력): 한 번 다시 시킨다.
                        # 질문에 등록 인력이 한 명이면 인력 찾기를 코드가 먼저 실행해 결과를 쥐여 준다(v3 평가 S08 'MM'을 검수로 보냄)
                        nudged, guardrail = True, "redirect_retry"
                        messages.append({"role": "assistant", "content": reply["content"]})
                        people = guardrails.mentioned_people(service, question)
                        if len(people) == 1 and (allowed is None or "find_people" in allowed):
                            result = call_tool(service, "find_people", {"name": people[0]})
                            calls.append({"tool": "find_people", "arguments": {"name": people[0]}, "ok": result["ok"],
                                          "data": result.get("data"), "error": result.get("error"), "forced": True})
                            messages += [{"role": "assistant", "content": "", "tool_calls": [
                                {"function": {"name": "find_people", "arguments": {"name": people[0]}}}]},
                                _tool_message("find_people", result)]
                        messages.append({"role": "system", "content": f"이 요청은 {spec.label} 업무다. 다른 카테고리로 안내하지 말고 "
                                                                      "도구 결과로 답하거나 맡은 도구를 호출해 처리하라."})
                        answer = ""
                        continue
                    if guardrails.owner_of(question) == spec.key:
                        answer, guardrail = guardrails.ANSWERS["redirect_failed"].format(here=spec.label), "redirect_failed"
                        break
                    fixed = guardrails.fix_redirect(answer, question, spec.key, LABELS)
                    if fixed != answer:
                        answer, guardrail = fixed, "redirect_fixed"
                break
            messages.append({"role": "assistant", "content": reply["content"],
                             "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                            for c in reply["tool_calls"]]})
            for call in reply["tool_calls"]:
                messages.append(_execute(service, call, allowed, spec, question, user_texts, texts, day, calls))
        else:
            if not answer:
                error = f"{max_steps}번 안에 답을 마치지 못했습니다."
    except LLMError as exc:
        error = str(exc)
    if any(c["ok"] and c["tool"].startswith("prepare_") for c in calls):
        answer = _not_saved_yet(answer)
    if answer and not spec.planned_notice:
        # 근거 없는 인력 이름이 답변에 있으면 한 번 다시 시키고, 그래도 있으면 답변 보류(v2 평가 C08 '김협력').
        # v3 평가 C08·G02(전문): 도구 없이 이름을 지어냄 → 다시 시키면 도구를 부르거나 대상을 되물을 기회가 생긴다.
        made_up = guardrails.ungrounded_names_in_answer(service, answer, texts, guardrails.seen_text(calls))
        if made_up and error is None:
            messages += [{"role": "assistant", "content": answer},
                         {"role": "system", "content": f"답변의 인력({', '.join(made_up)})은 도구 결과에 없다. 인력 이름을 지어내지 말고, "
                                                       "도구를 호출해 확인하거나 대상이 없으면 누구의 일인지 사용자에게 물어라."}]
            try:
                retry = _continue(service, client, messages, tools, tokens, calls, allowed, spec, question, user_texts,
                                  texts, day, max_steps=3)
            except LLMError as exc:
                retry = ""
                error = str(exc)
            if retry and not guardrails.ungrounded_names_in_answer(service, retry, texts, guardrails.seen_text(calls)):
                answer, guardrail = retry, "hold_retry"
            else:
                answer, guardrail = guardrails.hold_answer(service, question, made_up)
            if any(c["ok"] and c["tool"].startswith("prepare_") for c in calls):
                answer = _not_saved_yet(answer)
    return _finish(service, client, spec, question, t0, started, answer, calls, error, tokens, guardrail)
