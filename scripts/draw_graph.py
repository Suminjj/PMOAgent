"""LangGraph가 만든 라우터 처리 흐름 그래프를 그림 원본(Mermaid)으로 뽑는다(11/17, 발표·시연용).

손으로 그린 그림이 아니라 pmo/agent/graph.py에서 실제로 컴파일한 그래프 구조를 LangGraph가 직접 그린다.
인터넷 접속 없이 Mermaid 글만 만든다(그림 파일 변환은 mermaid.live 또는 mmdc로).

    python -m scripts.draw_graph                 # 화면에 출력 + docs/langgraph_router.mmd 저장
    python -m scripts.draw_graph --out 파일.mmd
"""
import argparse
from pathlib import Path

from pmo.agent.graph import routed_graph


def main() -> None:
    parser = argparse.ArgumentParser(description="LangGraph 라우터 흐름 그래프를 Mermaid로 저장")
    parser.add_argument("--out", default="docs/langgraph_router.mmd", help="저장할 파일(기본 docs/langgraph_router.mmd)")
    args = parser.parse_args()
    text = routed_graph().get_graph().draw_mermaid()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
