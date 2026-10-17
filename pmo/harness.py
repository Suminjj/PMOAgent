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
RESULTS_DIR = ROOT / "evals" / "results"


class Planner(Protocol):
    """질문을 받아 실행할(또는 실행한) 도구 호출 목록 [(이름, 입력)]을 돌려준다."""
    name: str

    def plan(self, question: dict, tools: list[dict], service: Service) -> list[tuple[str, dict]]: ...


class OraclePlanner:
    """정답 도구 호출을 그대로 사용한다(LLM 없이 도구·데이터 검증용)."""
    name = "oracle"

    def plan(self, question: dict, tools: list[dict], service: Service) -> list[tuple[str, dict]]:
        return [(s["tool"], s["args"]) for s in question["steps"]]


class AgentPlanner:
    """LLM Agent에 질문을 그대로 묻고, Agent가 호출한 도구를 돌려준다."""

    def __init__(self, client):
        from pmo.agent.runner import ask  # LLM 없이 oracle만 쓸 때 불필요한 import를 피한다
        self._ask = ask
        self.client = client
        self.name = client.name.replace(":", "-").replace("/", "-")
        self.last: dict = {}

    def plan(self, question: dict, tools: list[dict], service: Service) -> list[tuple[str, dict]]:
        self.last = self._ask(service, question["question"], self.client)
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
    if "current_projects" in expect:
        got = sorted(r["project"] for r in data["current"])
        if got != sorted(expect["current_projects"]):
            problems.append(f"현재 과제 {got} (기대 {expect['current_projects']})")
    return problems


def run(planner: Planner | None = None, questions_path: Path = QUESTIONS,
        save: bool = True) -> dict:
    planner = planner or OraclePlanner()
    spec = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    today = date.fromisoformat(spec["today"])
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        service = Service(Path(tmp) / "eval.db", clock=lambda: today)
        seed_demo(service, today)
        for q in spec["questions"]:
            t0 = time.perf_counter()
            calls = planner.plan(q, tool_specs(), service)
            expected = [(s["tool"], s["args"]) for s in q["steps"]]
            problems = []
            if [c[0] for c in calls] != [e[0] for e in expected]:
                problems.append(f"도구 순서 {[c[0] for c in calls]} (기대 {[e[0] for e in expected]})")
            for (name, args), step in zip(calls, q["steps"]):
                problems += [f"{name}: {p}" for p in check(step["expect"], call_tool(service, name, args))]
            row = {"id": q["id"], "question": q["question"], "tools": [c[0] for c in calls],
                   "success": not problems, "problems": problems,
                   "duration_ms": round((time.perf_counter() - t0) * 1000)}
            last = getattr(planner, "last", None)
            if last:
                row.update(answer=last["answer"], agent_error=last["error"],
                           agent_ms=last["duration_ms"])
                if last["error"]:
                    row["success"] = False
                    row["problems"].append(f"Agent 오류: {last['error']}")
            rows.append(row)
    report = {"planner": planner.name, "today": spec["today"],
              "run_at": datetime.now(TIMEZONE).isoformat(),
              "passed": sum(r["success"] for r in rows), "total": len(rows), "results": rows}
    if save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"{planner.name}-{datetime.now(TIMEZONE):%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["saved_to"] = str(out)
    return report
