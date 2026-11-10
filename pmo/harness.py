"""평가 하네스: 질문별 정답 도구·결과·응답 시간·성공 여부를 기록한다.

- oracle: 정답 도구 호출을 그대로 실행해 도구 계층과 데이터가 질문에 답할 수 있는지 검증한다.
- agent: LLM Agent가 실제로 고른 도구 호출을 정답과 비교하고, 답변과 응답 시간을 함께 기록한다.
"""
import json
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from pmo.config import ROOT, TIMEZONE
from pmo.demo import seed_demo
from pmo.service import Service
from pmo.tools import call_tool, tool_specs

QUESTIONS = ROOT / "evals" / "questions.json"
EVAL_SET = ROOT / "evals" / "eval_set.json"
ROUTING_SET = ROOT / "evals" / "routing_set.json"   # 자동 라우터 판정 질문 세트(11/10, Agent를 부르지 않고 판정만 채점)   # 정확도 평가 세트 (기준일 2026-11-15, scripts/build_eval_set.py)
RESULTS_DIR = ROOT / "evals" / "results"


class Planner(Protocol):
    """질문을 받아 실행할(또는 실행한) 도구 호출 목록 [(이름, 입력)]을 돌려준다."""
    name: str

    def plan(self, question: dict, tools: list[dict], service: Service) -> list[tuple[str, dict]]: ...


class OraclePlanner:
    """정답 도구 호출을 그대로 사용한다(LLM 없이 도구·데이터 검증용)."""
    name = "oracle"

    def plan(self, question: dict, tools: list[dict], service: Service) -> list[tuple[str, dict]]:
        return [(s["tool"], s["args"]) for s in question["steps"] or []]


ROUTINGS = ("auto", "category", "router")


def tag_agents(tag) -> list[str]:
    """질문의 정답 카테고리 태그(문자열 또는 여러 업무 질문의 목록)를 목록으로."""
    if not tag:
        return []
    return list(tag) if isinstance(tag, (list, tuple)) else [tag]


class AgentPlanner:
    """LLM Agent에 질문을 그대로 묻고, Agent가 호출한 도구를 돌려준다.

    routing="auto": 자동(전체) 단일 Agent(모든 도구, 업무 지침 미주입)
    routing="category": 질문의 agent 태그에 맞는 전문 Agent(도구 제한 + 업무 지침 주입). 태그가 여럿이면 차례로 부른다
    routing="router": v3.1 자동 라우터가 Agent를 고른다. 고른 Agent를 정답 태그와 비교해 라우팅 정확도를 잰다
    """

    def __init__(self, client, routing: str = "auto"):
        from pmo.agent.runner import ask  # LLM 없이 oracle만 쓸 때 불필요한 import를 피한다
        from pmo.agent.router import ask_agents, ask_routed
        if routing not in ROUTINGS:
            raise ValueError(f"routing은 {', '.join(ROUTINGS)} 중 하나여야 합니다.")
        self._ask, self._ask_agents, self._ask_routed = ask, ask_agents, ask_routed
        self.client = client
        self.routing = routing
        self.name = client.name.replace(":", "-").replace("/", "-") + ("" if routing == "auto" else f"-{routing}")
        self.last: dict = {}

    def plan(self, question: dict, tools: list[dict], service: Service) -> list[tuple[str, dict]]:
        text = question["question"]
        keys = tag_agents(question.get("agent"))
        if self.routing == "router":
            self.last = self._ask_routed(service, text, self.client)
        elif self.routing == "category" and len(keys) > 1:
            self.last = self._ask_agents(service, text, self.client, keys)
        else:
            self.last = self._ask(service, text, self.client,
                                  agent=keys[0] if self.routing == "category" and keys else None)
        # v3: 가드레일이 막아 실행하지 않은 호출(blocked)은 부르지 않은 것으로 본다
        return [_as_called(c) for c in self.last["calls"] if not c.get("blocked")]


def _as_called(call: dict) -> tuple[str, dict]:
    """채점용 (도구, 입력). 조건 조회 업체 칸에 인력 이름을 넣어 도구가 인력 찾기 결과를 돌려준 호출은 인력 찾기로 본다
    (v3.1 PC 평가 R01: search_people(company='신협력')로 정답을 답했는데 도구 순서로 틀림)."""
    data = call.get("data")
    if call["tool"] in ("search_people", "search_contracts") and isinstance(data, dict) and "people" in data:
        return FIND_TOOL, {"name": call["arguments"].get("company")}
    return call["tool"], call["arguments"]


def check(expect: dict, result: dict) -> list[str]:
    """기대 조건과 실제 결과를 비교해 실패 사유 목록을 돌려준다."""
    problems = []
    if result["ok"] != expect.get("ok", True):
        problems.append(f"ok={result['ok']} (기대 {expect.get('ok', True)}) {result.get('error', '')}".strip())
        return problems
    data = result.get("data")
    if "count" in expect and len(data) != expect["count"]:
        problems.append(f"건수 {len(data)} (기대 {expect['count']})")
    rows = data["members"] if isinstance(data, dict) and "members" in data else data   # 과제 인력 조회
    if "names" in expect and sorted({r["name"] for r in rows}) != sorted(expect["names"]):
        problems.append(f"인력 {sorted({r['name'] for r in rows})} (기대 {sorted(expect['names'])})")
    if "names_at_least" in expect:
        missing = sorted(set(expect["names_at_least"]) - {r["name"] for r in rows})
        if missing:
            problems.append(f"빠진 인력 {missing}")
    if "end_dates" in expect and sorted(r["end_date"] for r in data) != sorted(expect["end_dates"]):
        problems.append(f"종료일 {[r['end_date'] for r in data]} (기대 {expect['end_dates']})")
    for key in ("inspection_total_won", "contract_total_won"):     # 검수 금액(원)은 숫자로 비교
        if key in expect and (data or {}).get(key) != expect[key]:
            problems.append(f"{key} {(data or {}).get(key)} (기대 {expect[key]})")
    if "action" in expect and (data or {}).get("plan", {}).get("action") != expect["action"]:
        problems.append(f"변경 계획 {(data or {}).get('plan', {}).get('action')} (기대 {expect['action']})")
    if "current_projects" in expect:
        got = sorted(r["project"] for r in data["current"])
        if got != sorted(expect["current_projects"]):
            problems.append(f"현재 과제 {got} (기대 {expect['current_projects']})")
    return problems


def _attach_answer(row: dict, last: dict | None) -> bool:
    """Agent 답변·오류·응답 시간을 붙인다. Agent 오류가 있으면 True."""
    if not last:
        return False
    row.update(answer=last["answer"], agent_error=last["error"],
               agent_ms=last["duration_ms"], agent_used=last.get("agent"), tokens=last.get("tokens"))
    # 11/10: 오답 원인을 보려고 도구 입력·성공 여부·결과 건수를 남긴다(결과 원본은 남기지 않음)
    row["call_details"] = [{"tool": c["tool"], "arguments": c.get("arguments"), "ok": c.get("ok"),
                            "rows": len(c["data"]) if isinstance(c.get("data"), list) else None,
                            "error": (c.get("error") or "")[:200] or None,
                            **({"blocked": True} if c.get("blocked") else {})}
                           for c in last.get("calls") or []]
    if last.get("routing"):    # v3.1 라우터: 고른 Agent와 정답 카테고리 태그 비교(태그가 없는 질문은 채점하지 않음)
        routed = last["routing"]["agents"]
        row.update(routed=routed, route_method=last["routing"]["method"])
        # 가드레일 사전 검사로 정해진 문장을 답한 질문(범위 밖·관리하지 않는 정보·담당 밖 과제)은 라우팅 채점에서 뺀다
        if row.get("route_tag") and last["routing"]["method"] != "precheck":
            row["route_ok"] = routed == row["route_tag"]
    return bool(last["error"])


def routing_summary(rows: list[dict]) -> dict | None:
    """v3.1 라우팅 정확도: 정답 카테고리 태그가 있는 질문 중 라우터가 같은 Agent(여러 업무면 같은 순서)를 고른 비율."""
    checked = [r for r in rows if "route_ok" in r]
    if not checked:
        return None
    wrong = [f"{r['id']}: {'+'.join(r['routed'])} (정답 {'+'.join(r['route_tag'])})" for r in checked if not r["route_ok"]]
    methods: dict[str, int] = {}
    for r in rows:
        if r.get("route_method"):
            methods[r["route_method"]] = methods.get(r["route_method"], 0) + 1
    return {"checked": len(checked), "correct": len(checked) - len(wrong), "wrong": wrong, "methods": methods}


def token_summary(rows: list[dict]) -> dict | None:
    """질문별 토큰 수 합계·평균(LLM으로 실행했을 때만). 입력 = 시스템 지시문·도구 설명·대화·도구 결과."""
    used = [r["tokens"] for r in rows if r.get("tokens")]
    if not used:
        return None
    prompt, completion = sum(u["prompt"] for u in used), sum(u["completion"] for u in used)
    return {"questions": len(used), "prompt": prompt, "completion": completion,
            "avg_prompt": prompt // len(used), "avg_completion": completion // len(used),
            "avg_llm_calls": round(sum(u["llm_calls"] for u in used) / len(used), 2)}


FIND_TOOL = "find_people"


def _match(step: dict, called: str) -> str:
    """기대 단계와 실제 호출 비교용 이름. 단계에 대체 도구(alt)가 있고 그것을 불렀으면 같은 단계로 본다."""
    return called if called in step.get("alt", ()) else step["tool"]


def _expected_steps(steps: list[dict], called: list[str]) -> list[dict]:
    """채점할 기대 도구 순서.

    - optional 단계(예: 이름을 받는 도구 앞의 find_people)는 실제로 부른 횟수만큼만 기대한다.
    - or_find 단계(투입·계약 조회)는 find_people을 불렀으면 생략해도 된다. find_people 결과의 summary로 답할 수 있다(v1.2).
    """
    from collections import Counter
    spare = Counter(called)
    spare.subtract(Counter(st["tool"] for st in steps if not st.get("optional")))
    kept = []
    for st in steps:
        if st.get("or_find") and st["tool"] not in called and not set(st.get("alt", ())) & set(called) \
                and FIND_TOOL in called:
            continue
        if st.get("optional"):
            if spare[st["tool"]] <= 0:
                continue
            spare[st["tool"]] -= 1
        kept.append(st)
    return kept


def run(planner: Planner | None = None, questions_path: Path = QUESTIONS,
        save: bool = True, on_result=None, only: list[str] | None = None) -> dict:
    """on_result(row): 질문 하나가 끝날 때마다 호출된다(진행 상황 출력용).

    only: 질문 ID 또는 목표 버전(v1·v2·v3·추가 기능)만 골라 실행한다. 예: ["O04", "P01"] 또는 ["v2"].
    """
    planner = planner or OraclePlanner()
    spec = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    if only:
        wanted = {w.strip() for w in only if w.strip()}
        picked = [q for q in spec["questions"]
                  if q["id"] in wanted or (q.get("answer") or {}).get("target") in wanted]
        unknown = wanted - {q["id"] for q in spec["questions"]} - {(q.get("answer") or {}).get("target") for q in spec["questions"]}
        if unknown or not picked:
            raise ValueError(f"없는 질문 ID·목표 버전입니다: {', '.join(sorted(unknown)) or ', '.join(sorted(wanted))}")
        spec = dict(spec, questions=picked)
    today = date.fromisoformat(spec["today"])
    rows = []
    # ignore_cleanup_errors: Windows에서 다른 연결이 임시 DB를 잡고 있어도 평가 결과 저장까지 마친다(11/03 실행에서 결과 유실).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        service = Service(Path(tmp) / "eval.db", clock=lambda: today)
        if spec.get("seed") == "eval":
            from pmo.eval_data import seed_eval
            seed_eval(service, today)
        else:
            seed_demo(service, today)
        base = service
        for q in spec["questions"]:
            t0 = time.perf_counter()
            service = base
            if q.get("user"):   # v2 권한 질문: 그 계정으로 로그인한 것처럼 권한을 적용해 실행한다
                from pmo.access import authenticate
                from pmo.eval_data import EVAL_PASSWORD
                service = Service(base.path, clock=lambda: today,
                                  access=authenticate(base.path, q["user"], EVAL_PASSWORD))
            calls = planner.plan(q, tool_specs(), service)
            called = [c[0] for c in calls]
            if q["steps"] is None:
                # 답변 채점 질문: 맞는 도구가 없거나 답변 내용이 핵심. 기록만 하고 v4 저지가 answer 키로 채점한다.
                row = {"id": q["id"], "question": q["question"], "agent": q.get("agent"), "tools": called,
                       "route_tag": tag_agents(q.get("agent")),
                       "graded": False, "success": None, "problems": [], "answer_key": q.get("answer"),
                       "duration_ms": round((time.perf_counter() - t0) * 1000)}
                _attach_answer(row, getattr(planner, "last", None))
                rows.append(row)
                if on_result:
                    on_result(row)
                continue
            # optional 단계(예: 이름을 받는 도구 앞의 find_people)는 건너뛰어도 된다. 코드가 이름을 관리번호로 바꾼다.
            steps = _expected_steps(q["steps"], called)
            problems = []
            if called != [_match(st, name) for st, name in zip(steps, called)] or len(called) != len(steps):
                want = [st["tool"] + ("(생략 가능)" if st.get("optional") else "") for st in q["steps"]]
                problems.append(f"도구 순서 {called} (기대 {want})")
            else:
                for (name, args), step in zip(calls, steps):
                    expect = step["expect"] if name == step["tool"] else {"ok": True}   # 대체 도구는 성공만 본다
                    problems += [f"{name}: {p}" for p in check(expect, call_tool(service, name, args))]
            row = {"id": q["id"], "question": q["question"], "agent": q.get("agent"),
                   "route_tag": tag_agents(q.get("agent")),
                   "tools": [c[0] for c in calls], "graded": True,
                   "success": not problems, "problems": problems, "answer_key": q.get("answer"),
                   "duration_ms": round((time.perf_counter() - t0) * 1000)}
            if _attach_answer(row, getattr(planner, "last", None)):
                row["success"] = False
                row["problems"].append(f"Agent 오류: {row['agent_error']}")
            rows.append(row)
            if on_result:
                on_result(row)
    graded = [r for r in rows if r.get("graded", True)]
    report = {"planner": planner.name, "today": spec["today"], "questions": Path(questions_path).name,
              "run_at": datetime.now(TIMEZONE).isoformat(), "only": sorted(only) if only else None,
              "passed": sum(bool(r["success"]) for r in graded), "total": len(graded),
              "ungraded": len(rows) - len(graded), "tokens": token_summary(rows),
              "routing": routing_summary(rows), "results": rows}
    if save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        part = "-only-" + "_".join(sorted(only)).replace(" ", "") if only else ""   # 일부만 실행한 결과는 파일 이름으로 구분
        out = RESULTS_DIR / f"{Path(questions_path).stem}-{planner.name}{part}-{datetime.now(TIMEZONE):%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["saved_to"] = str(out)
    return report


def run_routing(client=None, mode: str = "rule", questions_path: Path = ROUTING_SET, save: bool = True,
                on_result=None) -> dict:
    """라우터 판정만 채점한다(Agent는 부르지 않음). client가 없으면 규칙만(LLM으로 넘길 질문은 규칙 판정 그대로).

    mode: rule(키워드, 애매하면 LLM) · llm(가드레일·이어지는 대화·되묻기 뒤 항상 LLM 분류).
    정답 expect가 빈 목록이면 '투입·계약 되묻기'(method clarify)가 정답이다.
    """
    from pmo.agent.router import route
    from pmo.eval_data import seed_eval
    spec = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    today = date.fromisoformat(spec["today"])
    name = (client.name.replace(":", "-").replace("/", "-") if client else "rules") + f"-{mode}"
    rows = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        service = Service(Path(tmp) / "routing.db", clock=lambda: today)
        seed_eval(service, today)
        for q in spec["questions"]:
            tokens = {"prompt": 0, "completion": 0, "llm_calls": 0}
            t0 = time.perf_counter()
            decision = route(service, q["question"], client, q.get("history"), q.get("previous"), tokens, mode=mode)
            got = [] if decision["method"] == "clarify" else decision["agents"]
            row = {"id": q["id"], "kind": q.get("kind"), "question": q["question"], "expect": q["expect"],
                   "routed": got, "method": decision["method"], "ok": got == q["expect"],
                   "ms": round((time.perf_counter() - t0) * 1000), "llm_calls": tokens["llm_calls"],
                   "reason": decision.get("reason"), "rule": decision.get("rule")}
            rows.append(row)
            if on_result:
                on_result(row)
    methods: dict[str, int] = {}
    for r in rows:
        methods[r["method"]] = methods.get(r["method"], 0) + 1
    llm_rows = [r for r in rows if r["llm_calls"]]
    report = {"planner": name, "questions": Path(questions_path).name, "run_at": datetime.now(TIMEZONE).isoformat(),
              "correct": sum(r["ok"] for r in rows), "total": len(rows), "methods": methods,
              "avg_ms": sum(r["ms"] for r in rows) // max(len(rows), 1),
              "avg_llm_ms": sum(r["ms"] for r in llm_rows) // max(len(llm_rows), 1) if llm_rows else None,
              "wrong": [f"{r['id']}: {'+'.join(r['routed']) or '되묻기'} (정답 {'+'.join(r['expect']) or '되묻기'})"
                        for r in rows if not r["ok"]],
              "results": rows}
    if save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"routing-{name}-{datetime.now(TIMEZONE):%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["saved_to"] = str(out)
    return report
