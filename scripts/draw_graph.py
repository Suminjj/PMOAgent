"""LangGraph가 만든 처리 흐름 그래프 두 개를 그림 원본(Mermaid)으로 뽑는다(11/17, 발표·시연용).

- 라우터 그래프(pmo/agent/graph.py): 되물음 대답 합치기 → 라우팅 → 전문 Agent 차례 호출 → 재배정 → 정리
- Agent 그래프(pmo/agent/agent_graph.py): 사전 가드레일 → LLM ↔ 도구(ReAct) → 다시 시키기 → 답변 검사 → 마무리

손으로 그린 그림이 아니라 pmo/agent/graph.py에서 실제로 컴파일한 그래프 구조를 LangGraph가 직접 그린다.
인터넷 접속 없이 Mermaid 글만 만든다(그림 파일 변환은 mermaid.live 또는 mmdc로).

    python -m scripts.draw_graph        # 화면에 출력 + docs/langgraph_router.mmd · docs/langgraph_agent.mmd 저장
"""
import argparse
from pathlib import Path

from pmo.agent.agent_graph import agent_graph
from pmo.agent.graph import routed_graph


def main() -> None:
    parser = argparse.ArgumentParser(description="LangGraph 흐름 그래프를 Mermaid로 저장")
    parser.add_argument("--dir", default="docs", help="저장할 폴더(기본 docs)")
    args = parser.parse_args()
    for name, graph in (("langgraph_router", routed_graph()), ("langgraph_agent", agent_graph())):
        text = graph.get_graph().draw_mermaid()
        out = Path(args.dir) / f"{name}.mmd"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"===== {name} =====\n{text}\n저장: {out}\n")


if __name__ == "__main__":
    main()
