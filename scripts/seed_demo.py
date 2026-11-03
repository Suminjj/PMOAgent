"""가상 시연 데이터 넣기: python -m scripts.seed_demo [DB 경로]
평가 데이터 넣기:       python -m scripts.seed_demo --eval [DB 경로]   (기준일 2026-11-15, 기본 data/eval.db)

기본 경로는 data/demo.db 이다. 데이터가 이미 있는 DB에는 넣지 않는다(기존 데이터 보호).
화면에서 보려면 같은 경로로 앱을 실행한다:
    PMO_DB_PATH=data/demo.db python -m streamlit run app.py
"""
import sys
from pathlib import Path

from pmo.config import ROOT, today
from pmo.demo import seed_demo
from pmo.service import Service


def main() -> int:
    args = sys.argv[1:]
    use_eval = "--eval" in args
    args = [a for a in args if a != "--eval"]
    path = Path(args[0]) if args else ROOT / "data" / ("eval.db" if use_eval else "demo.db")
    try:
        if use_eval:
            from pmo.eval_data import TODAY, seed_eval
            result = seed_eval(Service(path, clock=lambda: TODAY), TODAY)
            print("화면에서 보려면 기준일을 고정해 실행하세요: PMO_TODAY=2026-11-15, PMO_DB_PATH=" + str(path))
        else:
            result = seed_demo(Service(path), today())
    except RuntimeError as exc:
        print(exc)
        return 1
    print(f"가상 인력 {result['people']}명을 {path}에 넣었습니다 (기준일 {result['today']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
