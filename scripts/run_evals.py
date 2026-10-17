"""평가 질문 실행: python -m scripts.run_evals

가상 데이터를 임시 DB에 넣고 질문별 도구 호출 결과·성공 여부·소요 시간을 기록한다.
결과는 evals/results/ 에 JSON으로 저장된다(Git 추적 제외).
"""
import sys

from pmo.harness import run


def main() -> int:
    report = run()
    for row in report["results"]:
        mark = "PASS" if row["success"] else "FAIL"
        print(f"[{mark}] {row['id']} {row['duration_ms']:>4}ms  {row['question']}")
        for problem in row["problems"]:
            print(f"        - {problem}")
    print(f"\n{report['passed']}/{report['total']} 통과 · 결과 파일: {report.get('saved_to')}")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
