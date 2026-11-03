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
EVAL_SET = ROOT / "evals" / "eval_set.json"   # 정확도 평가 세트 (기준일 2026-11-15, scripts/build_eval_set.py)
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


ROUTINGS = ("auto", "category")


class AgentPlanner:
    """LLM Agent에 질문을 그대로 묻고, Agent가 호출한 도구를 돌려준다.

    routing="auto": 자동(전체) 단일 Agent(모든 도구, 업무 지침 미주입)
    routing="category": 질문의 agent 태그에 맞는 전문 Agent(도구 제한 + 업무 지침 주입)
    """

    def __init__(self, client, routing: str = "auto"):
        from pmo.agent.runner import ask  # LLM 없이 oracle만 쓸 때 불필요한 import를 피한다
        if routing not in ROUTINGS:
            raise ValueError(f"routing은 {', '.join(ROUTINGS)} 중 하나여야 합니다.")
        self._ask = ask
        self.client = client
        self.routing = routing
        self.name = client.name.replace(":", "-").replace("/", "-") + ("" if routing == "auto" else "-category")
        self.last: dict = {}

    def plan(self, question: dict, tools: list[dict], service: Service) -> list[tuple[str, dict]]:
        agent = question.get("agent") if self.routing == "category" else None
        self.last = self._ask(service, question["question"], self.client, agent=agent)
        return [(c["tool"], c["arguments"]) for c in self.last["calls"]]


def check(expect: dict, result: dict) -> list[str]:
    """기대 조건과 실제 결과를 비교해 실패 사유 목록을 돌려준다."""
    problems = []
    if result["ok"] != expect.get("ok", True):
        problems.append(f"ok={result['ok']} (기대 {expect.get('ok', True)}) {result.get('error', '')}".strip())
        return problems
    data = result.get("data")
    if "count" in expect and len(data) != expect["count"]:
        problems.append(f"건수 {len(data)} (기대 {expect['count']})")
    if "names" in expect and sorted({r["name"] for r in data}) != sorted(expect["names"]):
        problems.append(f"인력 {sorted({r['name'] for r in data})} (기대 {sorted(expect['names'])})")
    if "end_dates" in expect and sorted(r["end_date"] for r in data) != sorted(expect["end_dates"]):
        problems.append(f"종료일 {[r['end_date'] for r in data]} (기대 {expect['end_dates']})")
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
               agent_ms=last["duration_ms"], agent_used=last.get("agent"))
    return bool(last["error"])


def _expected_steps(steps: list[dict], called: list[str]) -> list[dict]:
    """optional 단계는 실제로 부른 만큼만 기대한다(교체처럼 find_people이 두 번 올 수 있을 때 하나만 불러도 됨)."""
    from collections import Counter
    spare = Counter(called)
    spare.subtract(Counter(st["tool"] for st in steps if not st.get("optional")))
    kept = []
    for st in steps:
        if st.get("optional"):
            if spare[st["tool"]] <= 0:
                continue
            spare[st["tool"]] -= 1
        kept.append(st)
    return kept


def run(planner: Planner | None = None, questions_path: Path = QUESTIONS,
        save: bool = True, on_result=None) -> dict:
    """on_result(row): 질문 하나가 끝날 때마다 호출된다(진행 상황 출력용)."""
    planner = planner or OraclePlanner()
    spec = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    today = date.fromisoformat(spec["today"])
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
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
            if called != [st["tool"] for st in steps]:
                want = [st["tool"] + ("(생략 가능)" if st.get("optional") else "") for st in q["steps"]]
                problems.append(f"도구 순서 {called} (기대 {want})")
            else:
                for (name, args), step in zip(calls, steps):
                    problems += [f"{name}: {p}" for p in check(step["expect"], call_tool(service, name, args))]
            row = {"id": q["id"], "question": q["question"], "agent": q.get("agent"),
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
              "run_at": datetime.now(TIMEZONE).isoformat(),
              "passed": sum(bool(r["success"]) for r in graded), "total": len(graded),
              "ungraded": len(rows) - len(graded), "results": rows}
    if save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"{Path(questions_path).stem}-{planner.name}-{datetime.now(TIMEZONE):%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["saved_to"] = str(out)
    return report
