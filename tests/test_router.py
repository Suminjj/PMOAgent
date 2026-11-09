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
    ("감마테크 450012201 11월 검수 금액 알려줘.", ["inspection"], "keyword"),
    ("알파솔루션 450011101 계약 총액 얼마야?", ["inspection"], "keyword"),         # '계약'보다 '총액'이 무거움
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
    decision = route(service, "알파솔루션 다음 달에 뭐 해야 돼?", client)
    assert decision["agents"] == ["contract"] and decision["method"] == "llm"
    messages, tools = client.seen[0]
    assert tools == [] and "분류기" in messages[0]["content"]             # 도구 없이 카테고리만 묻는다
    unsure = route(service, "알파솔루션 다음 달에 뭐 해야 돼?", ScriptedClient([text("잘 모르겠습니다")]))
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


def test_long_or_hard_questions_go_to_llm(service):
    """11/08 담당자: 짧고 분명하면 규칙, 길게 쓰거나 판단하기 어려우면 LLM이 맥락을 보고 고른다."""
    from pmo.agent.router import hard_to_judge
    from pmo.agent.guardrails import scores
    assert hard_to_judge("김정직 지금 무슨 과제야?", scores("김정직 지금 무슨 과제야?"), False) is None
    assert hard_to_judge("백협력 0.5MM는 반 달만 일한 거야?", scores("백협력 0.5MM는 반 달만 일한 거야?"), False) is None  # 소수점
    q = "이가상 계정 ID lee.fake로 넣어줘"                                   # 계정 ID 안의 점(PC 화면 테스트)
    assert hard_to_judge(q, scores(q), False) is None
    long_q = ("서협력 님 다음 달에도 계속 일할 수 있는지 궁금한데요, 과제 얘기가 아니라 구매계약이 언제 끝나는지가 궁금해요.")
    assert route(service, long_q)["agents"] == ["staff", "contract"]          # 규칙만이면 두 업무로 잘못 봄
    client = ScriptedClient([text("구매계약")])
    decision = route(service, long_q, client)
    assert decision["agents"] == ["contract"] and decision["method"] == "llm" and decision["reason"] == "long"
    assert decision["rule"] == ["staff", "contract"]                          # 규칙 판정도 남김(평가·기록용)


def test_llm_can_pick_several_agents_in_order(service):
    question = "정협력 관련해서 두 가지만 확인 부탁드려요. 하나는 8월 MM이 얼마나 나왔는지고, 다른 하나는 구매계약이 언제까지인지예요."
    decision = route(service, question, ScriptedClient([text("인력관리, 구매계약")]))
    assert decision["agents"] == ["staff", "contract"] and decision["method"] == "llm"


def test_unusable_llm_answer_keeps_rule_decision(service):
    question = "아 그 알파솔루션에 있는 이협력 있잖아요, 요즘 어느 과제에서 일하는지 자꾸 헷갈려서요. 지금 어디 들어가 있는지 좀 알려줄래요?"
    for reply in ("음… 잘 모르겠어요", "없음"):
        decision = route(service, question, ScriptedClient([text(reply)]))
        assert decision["agents"] == ["staff"] and decision["method"] == "keyword" and decision["reason"] == "long"


def test_llm_sees_previous_turn_as_context(service):
    client = ScriptedClient([text("구매계약")])
    history = [{"role": "user", "content": "박협력 구매계약 언제까지야?"},
               {"role": "assistant", "content": "박협력 계약은 11월 27일까지입니다."}]
    route(service, "그럼 그거 연장하려면 언제까지 처리해야 되는지도 궁금하고 그 다음 계약도 알려줄래요?", client, history=history)
    assert "직전 대화" in client.seen[0][0][1]["content"] and "11월 27일" in client.seen[0][0][1]["content"]


def test_multi_question_is_split_per_agent(service):
    """v3.1 PC 평가 M01·M02: 두 Agent가 같은 답을 반복 → Agent마다 자기 몫의 질문만 준다."""
    from pmo.agent.router import split_question
    assert split_question(service, "이협력 지금 투입 과제랑 구매계약 종료일 알려줘.", ["staff", "contract"]) == {
        "staff": "이협력 지금 투입 과제 알려줘.", "contract": "이협력 구매계약 종료일 알려줘."}
    assert split_question(service, "박협력 계약 종료일이랑 11월 투입 MM 알려줘.", ["contract", "staff"]) == {
        "contract": "박협력 계약 종료일 알려줘.", "staff": "박협력 11월 투입 MM 알려줘."}
    client = ScriptedClient([tool_call("find_people", name="이협력"), text("Soft Lab 고도화입니다."),
                             tool_call("list_contracts", person_id="이협력"), text("11월 30일까지입니다.")])
    ask_routed(service, "이협력 지금 투입 과제랑 구매계약 종료일 알려줘.", client)
    asked = [m[0][1]["content"] for m in client.seen if len(m[0]) == 2]   # Agent마다 첫 호출의 질문
    assert asked[0].startswith("이협력 지금 투입 과제 알려줘.") and asked[1].startswith("이협력 구매계약 종료일 알려줘.")


def test_v31_second_pc_eval_fixes(service):
    """v3.1 2차 PC 평가: 여러 업무의 Agent가 사전 검사 정해진 답(L04), 기간 지어내기(H01), 단가 칸 숫자 아님(X01), 과제별 MM(S08)."""
    from pmo.agent.runner import ask
    # 여러 업무 질문의 Agent는 사전 검사를 다시 하지 않는다
    q = "정협력 관련해서 두 가지만 확인 부탁드려요. 하나는 8월 MM이 얼마나 나왔는지고, 다른 하나는 구매계약이 언제까지인지예요."
    result = ask(service, q, ScriptedClient([tool_call("list_contracts", person_id="정협력"), text("구매계약은 12월 31일까지입니다.")]),
                 agent="contract", focus="구매계약 부분만")
    assert result["guardrail"] != "redirect" and "12월 31일" in result["answer"]
    # 기간을 말하지 않았는데 기간 MM 계산 → 실행하지 않음
    client = ScriptedClient([tool_call("calculate_mm", start_date="2026-11-01", end_date="2026-11-18", person_id="백협력"),
                             text("투입 이력을 보겠습니다.")])
    result = ask(service, "백협력 0.5MM는 반 달만 일한 거야, 한 달 내내 반씩 일한 거야?", client, agent="staff")
    # 3차 평가: 오류만 주면 사용자에게 '확인해 주세요'로 넘김 → 인력이 있으면 투입 이력 조회로 바꿔 실행
    assert result["calls"][0]["tool"] == "get_assignment_timeline" and result["calls"][0]["ok"]
    assert not result["calls"][0].get("blocked")
    # 과제가 둘인 달의 MM을 합계만 답하면 과제별 값을 붙인다
    client = ScriptedClient([tool_call("calculate_mm", start_date="2026-08-01", end_date="2026-08-31", person_id="정협력"),
                             text("정협력 8월 MM은 1.000MM입니다.")])
    result = ask(service, "정협력 8월 MM 얼마야?", client, agent="staff")
    assert "0.250" in result["answer"] and "0.750" in result["answer"]
    # 단가 칸에 숫자가 아닌 값 → 값을 안 준 것으로
    from pmo.agent.guardrails import drop_bad_rates
    assert drop_bad_rates({"purchase_rate": "L6", "grade": "L6"}) == {"grade": "L6"}
    assert drop_bad_rates({"purchase_rate": "9,500,000"}) == {"purchase_rate": "9,500,000"}


def test_purchase_question_goes_to_contract(service):
    """11/09: '구매'가 구매계약 키워드에 없어 LLM 분류가 인력관리를 고르던 문제."""
    decision = route(service, "12월 구매해야 하는 인력 알려줘.")
    assert (decision["agents"], decision["method"]) == (["contract"], "keyword")


def test_short_question_with_person_and_task_word_is_new_question(service):
    """11/09: '이협력 투입 중인 과제 뭐야?'는 되물음 대답이 아니라 새 질문(키워드 판정)."""
    history = [{"role": "user", "content": "12월 구매해야 하는 인력 알려줘."},
               {"role": "assistant", "content": "판정 기준이 아직 정해지지 않았습니다."}]
    decision = route(service, "이협력 투입 중인 과제 뭐야?", history=history, previous=["contract"])
    assert (decision["agents"], decision["method"]) == (["staff"], "keyword")
    # 업무 말 없이 이름만 답하면 여전히 이어지는 대화
    assert route(service, "감마테크 김협력B", history=history, previous=["staff"])["method"] == "follow_up"


def test_redirect_answer_is_rerouted_not_shown(service):
    """11/09: 자동 배정된 Agent가 '다른 카테고리를 골라 다시 요청하라'고 답하면 그 Agent에게 다시 맡긴다."""
    client = ScriptedClient([
        text("인력관리"),                                                           # LLM 분류(잘못)
        text("이 요청은 인력관리 카테고리에서 처리할 수 없습니다. 홈 화면에서 '구매계약'을 골라 다시 요청해 주세요."),
        tool_call("search_contracts", company="알파솔루션"),                        # 구매계약 Agent
        text("알파솔루션 계약 목록입니다."),
    ])
    result = ask_routed(service, "알파솔루션 다음 달에 뭐 해야 돼?", client)
    assert result["agent"] == "contract" and "카테고리에서 처리할 수 없습니다" not in result["answer"]
    assert result["routing"]["agents"] == ["contract"] and result["routing"]["rerouted_from"] == "staff"
    assert result["tokens"]["llm_calls"] == 4


@pytest.mark.parametrize("question, agents, method", [
    ("이협력 투입 종료일 알려줘", ["staff"], "keyword"),              # 투입과 함께 나온 종료일은 인력관리
    ("박협력 계약 언제 끝나?", ["contract"], "keyword"),
    ("이협력 계약 금액 알려줘", ["inspection"], "keyword"),           # 돈은 검수
    ("검수 전에 확인하려는데 정협력 11월 MM 얼마야?", ["staff"], "keyword"),   # MM은 인력관리
    ("감마테크 인력들 이번 달 MM이랑 검수 금액 알려줘", ["inspection"], "keyword"),  # 검수 결과에 인력별 MM 포함
    ("이협력 누구야?", ["staff"], "people"),                          # 인력에 대한 정보
    ("알파솔루션 사람 몇 명이야?", ["staff"], "people"),
    ("이번 주에 챙겨야 할 거 있어?", ["contract"], "keyword"),         # 만료 알림
    ("서협력 아이디 in06538 넣어줘", ["staff"], "keyword"),
])
def test_boundary_1110(service, question, agents, method):
    """11/10 담당자 경계: 인력 정보는 인력관리, 구매계약 기간·연장·종료는 구매계약, 금액은 검수."""
    decision = route(service, question)
    assert (decision["agents"], decision["method"]) == (agents, method)


@pytest.mark.parametrize("question", ["이협력 종료일 12월 31일로 바꿔줘", "박협력 언제 끝나?", "곧 끝나는 사람 있어?"])
def test_unclear_end_date_asks_first(service, question):
    """11/10 담당자: 투입 종료일인지 구매계약 종료일인지 알 수 없으면 Agent를 부르지 않고 되묻는다."""
    decision = route(service, question, ScriptedClient([]))
    assert decision["agents"] == [] and decision["method"] == "clarify"


def test_clarify_then_answer_goes_to_chosen_agent(service):
    from pmo.agent.router import CLARIFY_END
    first = ask_routed(service, "이협력 종료일 12월 31일로 바꿔줘", ScriptedClient([]))
    assert first["ok"] and first["answer"] == CLARIFY_END and first["routing"]["method"] == "clarify"
    assert first["tokens"]["llm_calls"] == 0
    history = [{"role": "user", "content": "이협력 종료일 12월 31일로 바꿔줘"}, {"role": "assistant", "content": CLARIFY_END}]
    client = ScriptedClient([tool_call("list_contracts", person_id="이협력"), text("이협력 구매계약 종료일을 확인했습니다.")])
    second = ask_routed(service, "계약", client, history=history, previous=[])
    assert second["routing"]["agents"] == ["contract"]
    assert "이협력 종료일 12월 31일로 바꿔줘 (계약 기준)" in client.seen[0][0][-1]["content"]   # 원래 질문에 대답을 붙여 처리


def test_routing_set_rules_and_llm_mode(service):
    from pmo.harness import run_routing
    report = run_routing(save=False)
    assert report["total"] == 32 and report["correct"] == report["total"]
    client = ScriptedClient([text("인력관리")] * 40)
    llm = run_routing(client, mode="llm", save=False)
    assert llm["methods"].get("llm", 0) > 0                                # llm 모드는 규칙 대신 LLM이 고른다
