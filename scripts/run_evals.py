"""평가 질문 실행.

    python -m scripts.run_evals                          # oracle: 도구·데이터 검증 (LLM 없음)
    python -m scripts.run_evals --model qwen3:8b          # 로컬 Ollama 모델로 Agent 평가
    python -m scripts.run_evals --model qwen3:8b,gemma4:12b  # 여러 모델 비교

가상 데이터를 임시 DB에 넣고 질문별 도구 선택·성공 여부·응답 시간을 기록한다.
결과는 evals/results/ 에 JSON으로 저장된다(Git 추적 제외).
"""
import argparse
import sys

from pmo.harness import AgentPlanner, OraclePlanner, run


def print_row(row: dict) -> None:
    mark = "PASS" if row["success"] else "FAIL"
    ms = row.get("agent_ms", row["duration_ms"])
    print(f"[{mark}] {row['id']} {ms:>6}ms  {row['question']}")
    print(f"        도구: {' → '.join(row['tools']) or '(호출 없음)'}")
    if row.get("answer"):
        print("        답변: " + row["answer"].replace("\n", " ")[:160])
    for problem in row["problems"]:
        print(f"        - {problem}")
    sys.stdout.flush()


def report(result: dict) -> None:
    times = [r.get("agent_ms", r["duration_ms"]) for r in result["results"]]
    print(f"{result['passed']}/{result['total']} 통과 · 평균 {sum(times) // max(len(times), 1)}ms"
          f" · 결과 파일: {result.get('saved_to')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="PMOAgent 평가 질문 실행")
    parser.add_argument("--model", help="Ollama 모델 이름. 쉼표로 여러 개를 비교한다. 없으면 oracle")
    args = parser.parse_args()
    if not args.model:
        planners = [OraclePlanner()]
    else:
        from pmo.agent.llm import OllamaClient
        planners = [AgentPlanner(OllamaClient(model=m.strip())) for m in args.model.split(",") if m.strip()]
    results = []
    for planner in planners:
        print(f"\n=== {planner.name} === (질문 하나가 끝날 때마다 표시됩니다)", flush=True)
        result = run(planner, on_result=print_row)
        report(result)
        results.append(result)
    if len(results) > 1:
        print("\n=== 비교 ===")
        for r in results:
            times = [x.get("agent_ms", x["duration_ms"]) for x in r["results"]]
            print(f"{r['planner']:<30} {r['passed']}/{r['total']} 통과 · 평균 {sum(times) // len(times)}ms")
    return 0 if all(r["passed"] == r["total"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
