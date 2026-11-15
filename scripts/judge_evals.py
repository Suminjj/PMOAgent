"""LLM-as-a-Judge: 평가 결과의 Agent 답변을 판정 모델이 채점 기준표로 채점한다 (v4).

    python -m scripts.judge_evals                                   # 가장 최근 평가 결과를 기본 판정 모델로
    python -m scripts.judge_evals evals\\results\\eval_set-openai-gpt-4.1-router-….json --judge gpt-5-mini,gpt-6-luna
    python -m scripts.judge_evals --human evals\\results\\judge-….json evals\\results\\judge-….csv
                                                                    # 사람 검토와 판정 모델 비교(일치율·kappa)

판정 결과는 evals/results/judge-*.json, 사람 검토 양식은 같은 이름의 .csv(엑셀로 열기)로 저장된다.
판정 모델은 답변 모델과 다른 모델을 쓴다. 기본은 PMO_JUDGE_MODEL, 없으면 gpt-5-mini.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from pmo.config import ROOT, TIMEZONE
from pmo.judge import (CRITERIA, VERDICT_KO, agreement, read_review_csv, write_review_csv,
                       judge_report)

DEFAULT_JUDGE = "gpt-5-mini"
RESULTS_DIR = ROOT / "evals" / "results"   # pmo.harness와 같은 곳(판정만 할 때 Service를 불러오지 않음)


def latest_result() -> Path | None:
    files = [p for p in RESULTS_DIR.glob("eval_set-*.json") if "-oracle-" not in p.name]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def print_row(row: dict) -> None:
    verdict = VERDICT_KO.get(row.get("verdict"), "판정 오류")
    key = {True: "글자 확인 통과", False: "글자 확인 실패", None: ""}[row.get("key_check")]
    scores = " ".join(f"{k[:4]}{v}" for k, v in (row.get("scores") or {}).items())
    print(f"[{verdict}] {row['id']} {scores} {key}  {row['question']}")
    if row.get("reason") or row.get("judge_error"):
        print(f"        {row.get('reason') or row.get('judge_error')}")
    sys.stdout.flush()


def print_summary(judged: dict) -> None:
    s = judged["summary"]
    c = s["counts"]
    print(f"판정 {s['judged']}문항: 정답 {c['pass']} · 부분 정답 {c['partial']} · 오답 {c['fail']}"
          f" (정답률 {s['pass_rate']:.1%})" if s["pass_rate"] is not None else "판정한 문항이 없습니다")
    if s["avg_scores"]:
        print("항목별 평균(5점): " + " · ".join(f"{CRITERIA[k].split(':')[0]} {v}" for k, v in s["avg_scores"].items()))
    if s["key_check_disagree"]:
        print("글자 확인과 다른 판정:")
        for d in s["key_check_disagree"]:
            print(f"  - {d['id']}: 글자 확인 {'통과' if d['key_check'] else '실패'} → 판정 {VERDICT_KO[d['verdict']]} ({d['reason']})")
    if s["errors"]:
        print(f"판정하지 못한 문항: {', '.join(s['errors'])}")
    print(f"판정 모델 토큰: 입력 {s['judge_tokens']['prompt']:,} · 출력 {s['judge_tokens']['completion']:,}")


def compare_human(judged_path: Path, csv_path: Path) -> int:
    judged = json.loads(judged_path.read_text(encoding="utf-8"))
    human = read_review_csv(csv_path)
    if not human:
        print("사람 판정 칸이 비어 있습니다. CSV의 '사람 판정' 칸에 정답·부분 정답·오답을 적어 주세요.")
        return 2
    model = {r["id"]: r["verdict"] for r in judged["results"] if r.get("verdict")}
    a = agreement(human, model)
    print(f"사람 검토 {a['n']}문항 vs 판정 모델({judged['judge']}): 일치 {a['agree']}/{a['n']} ({a['rate']:.0%}) · "
          f"Cohen's kappa {a['kappa']}")
    for i in a["differ"]:
        print(f"  - {i}: 사람 {VERDICT_KO[human[i]]} · 모델 {VERDICT_KO[model[i]]}")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="LLM-as-a-Judge 답변 채점")
    parser.add_argument("results", nargs="*", help="평가 결과 JSON(없으면 evals/results의 가장 최근 결과)")
    parser.add_argument("--judge", help=f"판정 모델(쉼표로 여럿). 기본 PMO_JUDGE_MODEL 또는 {DEFAULT_JUDGE}")
    parser.add_argument("--only", help="골라 채점할 질문 ID(쉼표로)")
    parser.add_argument("--human", nargs=2, metavar=("JUDGE_JSON", "REVIEW_CSV"),
                        help="판정 결과와 사람이 채운 검토 양식을 비교한다(LLM 호출 없음)")
    args = parser.parse_args()
    if args.human:
        return compare_human(Path(args.human[0]), Path(args.human[1]))

    paths = [Path(p) for p in args.results] or [p for p in [latest_result()] if p]
    if not paths:
        print("평가 결과가 없습니다. 먼저 python -m scripts.run_evals --set eval --model gpt-4.1 --routing router 를 실행하세요.")
        return 2
    from pmo.agent.llm import LLMError, make_client
    names = [m.strip() for m in (args.judge or os.environ.get("PMO_JUDGE_MODEL") or DEFAULT_JUDGE).split(",") if m.strip()]
    clients = [make_client(m) for m in names]
    try:
        for client in clients:
            client.check()
    except LLMError as exc:
        print(f"시작할 수 없습니다: {exc}")
        return 2
    only = [x.strip() for x in (args.only or "").split(",") if x.strip()] or None
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        report.setdefault("saved_to", str(path))
        verdicts = {}
        for client in clients:
            print(f"\n=== 채점: {path.name} · 판정 {client.name} ===", flush=True)
            judged = judge_report(report, client, on_result=print_row, only=only)
            if judged["self_judged"]:
                print("! 주의: 판정 모델이 답변 모델과 같습니다. 자기 답을 후하게 채점할 수 있어 다른 모델을 권합니다.")
            print_summary(judged)
            stem = f"judge-{path.stem}-{client.name.split(':', 1)[-1].replace(':', '-')}-{datetime.now(TIMEZONE):%Y%m%d-%H%M%S}"
            out = RESULTS_DIR / f"{stem}.json"
            out.write_text(json.dumps(judged, ensure_ascii=False, indent=2), encoding="utf-8")
            write_review_csv(judged, RESULTS_DIR / f"{stem}.csv")
            print(f"결과 파일: {out}\n사람 검토 양식: {RESULTS_DIR / (stem + '.csv')}")
            verdicts[client.name] = {r["id"]: r["verdict"] for r in judged["results"] if r.get("verdict")}
        if len(verdicts) > 1:
            print("\n=== 판정 모델 간 일치 ===")
            keys = list(verdicts)
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    g = agreement(verdicts[a], verdicts[b])
                    print(f"{a} vs {b}: 일치 {g['agree']}/{g['n']} ({g['rate']:.0%}) · kappa {g['kappa']}"
                          + (f" · 다른 질문 {', '.join(g['differ'])}" if g["differ"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
