"""가상 시연 데이터 넣기: python -m scripts.seed_demo [DB 경로]

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
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "demo.db"
    try:
        result = seed_demo(Service(path), today())
    except RuntimeError as exc:
        print(exc)
        return 1
    print(f"가상 인력 {result['people']}명을 {path}에 넣었습니다 (기준일 {result['today']}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
