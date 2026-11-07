"""v3.1 자동 라우터 멀티에이전트 (평가용 가상 데이터, 가짜 LLM)."""
import pytest

from pmo.agent.router import ask_routed, route
from pmo.eval_data import TODAY, seed_eval
from pmo.harness import AgentPlanner, EVAL_SET, run
from pmo.service import Service
from tests.test_agent import ScriptedClient, text, tool_call


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "router.db", clock=lambda: TODAY)
    seed_eval(s, TODAY)
    return s


@pytest.mark.parametrize("question, agents, method", [
    ("김정직 지금 무슨 과제에 투입되어 있어?", ["staff"], "keyword"),
    ("박협력 구매계약 언제까지야?", ["contract"], "keyword"),
    ("감마테크 C-2026-202 11월 검수 금액 알려줘.", ["inspection"], "keyword"),
    ("알파솔루션 C-2026-103 계약 총액 얼마야?", ["inspection"], "keyword"),         # '계약'보다 '총액'이 무거움
    ("11월 1일부터 18일까지 MM 얼마야?", ["staff"], "keyword"),
    ("이협력 지금 투입 과제랑 구매계약 종료일 알려줘.", ["staff", "contract"], "multi"),   # 질문에 나온 순서
    ("박협력 계약 종료일이랑 11월 투입 MM 알려줘.", ["contract", "staff"], "multi"),
    ("오늘 점심 메뉴 추천해줘.", ["auto"], "precheck"),                             # 가드레일이 정해진 문장으로 답함
])
def test_keyword_routing(service, question, agents, method):
    decision = route(service, question)
    assert (decision["agents"], decision["method"]) == (agents, method)


def test_same_category_words_with_joiner_stay_single(service):
    """'과제와 기간'처럼 한 업무 안의 연결 말은 여러 업무로 보지 않는다."""
    assert route(service, "이정직 투입 과제와 기간 알려줘.")["agents"] == ["staff"]


def test_follow_up_goes_to_previous_agents(service):
    history = [{"role": "user", "content": "김협력 투입 기간 알려줘"},
               {"role": "assistant", "content": "김협력이 2명입니다. 어느 업체인가요?"}]
    decision = route(service, "감마테크", history=history, previous=["staff"])
    assert decision == {"agents": ["staff"], "method": "follow_up", "scores": decision["scores"]}


def test_unclear_question_asks_llm_for_category_only(service):
    client = ScriptedClient([text("구매계약")])
    decision = route(service, "서협력 다음 달에 뭐 해야 돼?", client)
    assert decision["agents"] == ["contract"] and decision["method"] == "llm"
    messages, tools = client.seen[0]
    assert tools == [] and "분류기" in messages[0]["content"]             # 도구 없이 카테고리만 묻는다
    unsure = route(service, "서협력 다음 달에 뭐 해야 돼?", ScriptedClient([text("잘 모르겠습니다")]))
    assert unsure["agents"] == ["auto"] and unsure["method"] == "fallback"   # 못 고르면 전체 Agent


def test_routed_question_uses_specialist_tools(service):
    client = ScriptedClient([tool_call("list_contracts", person_id="박협력"), text("박협력 계약은 11월 27일까지입니다.")])
    result = ask_routed(service, "박협력 구매계약 언제까지야?", client)
    assert result["ok"] and result["agent"] == "contract"
    assert result["routing"]["agents"] == ["contract"] and result["routing"]["labels"] == ["구매계약"]
    assert "[업무 지침: contract-expiry]" in client.seen[0][0][0]["content"]   # 전문 Agent 지침이 들어감
    assert any(r["operation"] == "router" for r in service.run_log(5))       # 라우팅을 실행 기록에 남김


def test_multi_agent_question_combines_answers(service):
    client = ScriptedClient([
        tool_call("get_assignment_timeline", person_id="이협력"), text("이협력은 Soft Lab 고도화에 투입 중입니다."),
        tool_call("list_contracts", person_id="이협력"),
        text("이협력 계약은 11월 30일까지입니다. 투입 과제는 인력관리 카테고리에서 처리할 수 없습니다. "
             "홈 화면에서 '인력관리'를 골라 다시 요청해 주세요."),
    ])
    result = ask_routed(service, "이협력 지금 투입 과제랑 구매계약 종료일 알려줘.", client)
    assert result["agent"] == "staff+contract" and result["agent_label"] == "인력관리 + 구매계약"
    assert [c["tool"] for c in result["calls"]] == ["get_assignment_timeline", "list_contracts"]
    assert result["answer"].index("**인력관리**") < result["answer"].index("**구매계약**")
    assert "Soft Lab 고도화" in result["answer"] and "11월 30일" in result["answer"]
    assert "다시 요청해 주세요" not in result["answer"]                     # 다른 Agent 몫의 안내 문장은 뺀다
    staff_prompt = client.seen[0][0][0]["content"]
    assert "인력관리 업무 부분만" in staff_prompt and "구매계약 업무 부분은 다른 Agent가" in staff_prompt


def test_router_eval_reports_routing_accuracy():
    class KeywordsOnly:
        name = "fake:router"

        def chat(self, messages, tools):
            return text("모르겠습니다.")

    report = run(AgentPlanner(KeywordsOnly(), "router"), questions_path=EVAL_SET, save=False,
                 only=["S01", "C01", "I01", "M01", "O01"])
    assert report["planner"] == "fake-router-router"
    rows = {r["id"]: r for r in report["results"]}
    assert rows["M01"]["routed"] == ["staff", "contract"] and rows["M01"]["route_ok"]
    assert "route_ok" not in rows["O01"]                                    # 태그 없는(가드레일) 질문은 채점 안 함
    assert report["routing"]["checked"] == 4 and report["routing"]["correct"] == 4
