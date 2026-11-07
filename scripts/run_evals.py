"""평가 질문 실행.

    python -m scripts.run_evals                          # oracle: 도구·데이터 검증 (LLM 없음)
    python -m scripts.run_evals --model qwen3:8b          # 로컬 Ollama 모델로 Agent 평가
    python -m scripts.run_evals --model qwen3:8b,gemma4:12b  # 여러 모델 비교
    python -m scripts.run_evals --model qwen3:8b --routing auto,category
                                                         # 자동(전체) 단일 Agent vs 카테고리별 전문 Agent
    python -m scripts.run_evals --set eval --model qwen3:8b --routing router
                                                         # v3.1 자동 라우터 (라우팅 정확도 함께 표시)

가상 데이터를 임시 DB에 넣고 질문별 도구 선택·성공 여부·응답 시간을 기록한다.
결과는 evals/results/ 에 JSON으로 저장된다(Git 추적 제외).
"""
import argparse
import os
import sys

from pmo.harness import EVAL_SET, QUESTIONS, ROUTINGS, AgentPlanner, OraclePlanner, run


def print_row(row: dict) -> None:
    mark = "답변" if row.get("graded") is False else ("PASS" if row["success"] else "FAIL")
    ms = row.get("agent_ms", row["duration_ms"])
    who = f" [{row['agent_used']}]" if row.get("agent_used") else ""
    if row.get("routed"):     # v3.1 라우터: 고른 Agent(방법), 정답 태그와 다르면 표시
        mark_route = "" if row.get("route_ok", True) else f" ≠ 정답 {'+'.join(row['route_tag'])}"
        who = f" [라우터 → {'+'.join(row['routed'])} ({row['route_method']}){mark_route}]"
    tok = row.get("tokens")
    used = f"  토큰 {tok['prompt']:,}+{tok['completion']:,}" if tok else ""
    print(f"[{mark}] {row['id']}{who} {ms:>6}ms{used}  {row['question']}")
    print(f"        도구: {' → '.join(row['tools']) or '(호출 없음)'}")
    if row.get("answer"):
        print("        답변: " + row["answer"].replace("\n", " ")[:160])
    for problem in row["problems"]:
        print(f"        - {problem}")
    if row.get("graded") is False and row.get("answer") is not None:
        # 답변 채점 질문: 저지(v4) 전 참고용으로 '나오면 안 되는 말'·'들어가야 할 말'만 글자 그대로 확인한다
        key = row.get("answer_key") or {}
        leaked = [w for w in key.get("must_not_include") or [] if w in row["answer"]]
        missing = [w for w in key.get("must_include") or [] if w not in row["answer"]]
        if leaked:
            print(f"        ! 참고: 나오면 안 되는 말이 있음 → {', '.join(leaked)}")
        if missing:
            print(f"        ! 참고: 들어가야 할 말이 없음 → {', '.join(missing)}")
        if not leaked and not missing and (key.get("must_not_include") or key.get("must_include")):
            print("        OK 참고: 답변 키(들어가야 할 말·나오면 안 되는 말) 글자 확인 통과")
    sys.stdout.flush()


def report(result: dict) -> None:
    times = [r.get("agent_ms", r["duration_ms"]) for r in result["results"]]
    print(f"{result['passed']}/{result['total']} 통과 (답변 채점 대기 {result.get('ungraded', 0)}개) · 평균 {sum(times) // max(len(times), 1)}ms"
          f" · 결과 파일: {result.get('saved_to')}")
    if result.get("routing"):
        r = result["routing"]
        print(f"라우팅 정확도: {r['correct']}/{r['checked']} · 방법별 {r['methods']}"
              + (f" · 틀림: {'; '.join(r['wrong'])}" if r["wrong"] else ""))
    if result.get("tokens"):
        t = result["tokens"]
        print(f"토큰: 질문당 평균 입력 {t['avg_prompt']:,} · 출력 {t['avg_completion']:,} (LLM 호출 평균 {t['avg_llm_calls']}회), "
              f"전체 입력 {t['prompt']:,} · 출력 {t['completion']:,}")


def main() -> int:
    # Windows 콘솔(cp949)이 못 쓰는 글자(✓·이모지 등)가 답변에 있어도 평가가 멈추지 않게 바꿔서 출력한다(11/04 --only 실행 중단).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="PMOAgent 평가 질문 실행")
    parser.add_argument("--model", help="Ollama 모델 이름. 쉼표로 여러 개를 비교한다. 없으면 oracle")
    parser.add_argument("--set", default="v1", choices=("v1", "eval"),
                        help="v1: 기존 평가 질문 9개(시연 데이터) · eval: 정확도 평가 세트(evals/eval_set.json, 기준일 11/15)")
    parser.add_argument("--only", help="골라 실행할 질문 ID 또는 목표 버전(쉼표로). 예: O04,P01,P02 또는 v2")
    parser.add_argument("--routing", default="auto",
                        help="auto(자동(전체) 단일 Agent), category(질문별 전문 Agent), router(v3.1 자동 라우터). "
                             "쉼표로 여럿 비교")
    args = parser.parse_args()
    routings = [r.strip() for r in args.routing.split(",") if r.strip()]
    if any(r not in ROUTINGS for r in routings):
        parser.error(f"--routing은 {', '.join(ROUTINGS)} 중에서 고릅니다.")
    if not args.model:
        planners = [OraclePlanner()]
    else:
        from pmo.agent.llm import OllamaClient
        from pmo.agent.llm import LLMError
        clients = [OllamaClient(model=m.strip()) for m in args.model.split(",") if m.strip()]
        try:
            for client in clients:
                client.check()  # 서버·모델이 없으면 질문을 돌리기 전에 바로 멈춘다
        except LLMError as exc:
            print(f"시작할 수 없습니다: {exc}")
            return 2
        planners = [AgentPlanner(c, r) for c in clients for r in routings]
        print("설정: 문맥 " + str(clients[0].num_ctx) + "토큰, 추론 모드 "
              + os.environ.get("PMO_OLLAMA_THINK", "false"), flush=True)
    results = []
    for planner in planners:
        print(f"\n=== {planner.name} === (질문 하나가 끝날 때마다 표시됩니다)", flush=True)
        only = [x for x in (args.only or "").split(",") if x.strip()] or None
        try:
            result = run(planner, questions_path=EVAL_SET if args.set == "eval" else QUESTIONS, on_result=print_row,
                         only=only)
        except ValueError as exc:
            print(exc)
            return 2
        report(result)
        results.append(result)
    if len(results) > 1:
        print("\n=== 비교 ===")
        for r in results:
            times = [x.get("agent_ms", x["duration_ms"]) for x in r["results"]]
            tok = r.get("tokens") or {}
            extra = f" · 토큰 평균 {tok['avg_prompt']:,}+{tok['avg_completion']:,}" if tok else ""
            print(f"{r['planner']:<30} {r['passed']}/{r['total']} 통과 · 평균 {sum(times) // len(times)}ms{extra}")
    return 0 if all(r["passed"] == r["total"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
