"""자연어로 한 번 물어보기 (로컬 Ollama).

    python -m scripts.ask "베타컨설팅 10월 계약 종료 예정 인력 알려줘"
    python -m scripts.ask --model gemma4:9b "김가상 지금 무슨 과제야?"

DB는 PMO_DB_PATH(기본 data/pmo.db)를 쓴다. 시연 DB를 쓰려면 PMO_DB_PATH=data/demo.db.
조회만 하며 데이터를 바꾸지 않는다.
"""
import argparse
import sys

from pmo.agent.llm import OllamaClient
from pmo.agent.runner import ask
from pmo.config import db_path
from pmo.service import Service


def main() -> int:
    parser = argparse.ArgumentParser(description="PMOAgent 자연어 조회")
    parser.add_argument("question")
    parser.add_argument("--model", help="Ollama 모델 (기본: PMO_OLLAMA_MODEL 또는 qwen3:8b)")
    args = parser.parse_args()
    client = OllamaClient(model=args.model)
    result = ask(Service(db_path()), args.question, client)
    for call in result["calls"]:
        print(f"[도구] {call['tool']} {call['arguments']} → {'성공' if call['ok'] else '실패'}")
    print()
    print(result["answer"] if result["ok"] else f"오류: {result['error']}")
    print(f"\n({result['model']} · {result['duration_ms']}ms)")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
