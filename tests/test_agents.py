"""카테고리별 전문 Agent(수동 라우팅 멀티에이전트) 테스트 (가짜 LLM, 가상 데이터)."""
import json
from datetime import date

import pytest

from pmo.agent.agents import AGENTS, AUTO, SKILLS_DIR, get_agent, skill_text, system_prompt
from pmo.agent.runner import ask
from pmo.config import ROOT
from pmo.demo import seed_demo
from pmo.harness import AgentPlanner, run
from pmo.service import Service
from pmo.tools import TOOLS, tool_specs
from tests.test_agent import ScriptedClient, text, tool_call

TODAY = date(2026, 10, 17)


@pytest.fixture
def service(tmp_path):
    s = Service(tmp_path / "agents.db", clock=lambda: TODAY)
    seed_demo(s, TODAY)
    return s


def test_four_categories_plus_auto():
    assert [a.label for a in AGENTS.values()] == ["자동(전체)", "인력관리", "근무 환경 준비", "구매계약", "검수"]
    assert get_agent(None).key == AUTO and get_agent("없는키").key == AUTO


def test_agent_tools_and_skills_exist():
    for agent in AGENTS.values():
        assert agent.examples
        if agent.tools is None:
            continue
        assert set(agent.tools) <= set(TOOLS), agent.key
        for name in agent.skills:
            assert (SKILLS_DIR / name / "SKILL.md").exists(), name


def test_every_tool_belongs_to_some_specialist():
    covered = set().union(*(a.tools for a in AGENTS.values() if a.tools))
    assert covered == set(TOOLS)


def test_writes_stay_in_their_category():
    assert {t for t in AGENTS["staff"].tools if t.startswith("prepare_")} == {"prepare_account_id"}
    assert {t for t in AGENTS["environment"].tools if t.startswith("prepare_")} == {"prepare_checklist_update"}
    assert {t for t in AGENTS["contract"].tools if t.startswith("prepare_")} == {
        "prepare_contract_save", "prepare_contract_delete", "prepare_alert_ack"}
    assert AGENTS["inspection"].tools == ("inspection_amount_by_revenue_project",)


def test_prompt_injects_guides_only_for_specialists():
    auto = system_prompt(AGENTS[AUTO], "2026-10-17")
    assert "[업무 지침" not in auto
    staff = system_prompt(AGENTS["staff"], "2026-10-17")
    assert "[업무 지침: staff-assignment-lookup]" in staff and "[업무 지침: data-change]" in staff
    assert "인력관리 담당 Agent" in staff and "구매계약" in staff        # 다른 카테고리 안내
    assert "status:" not in staff and "## 성공 기준" not in staff       # 앞머리·평가용 절 제외
    assert "`prepare_contract_save`" not in staff                      # 쓸 수 없는 도구 행은 뺀다
    assert "`prepare_account_id`" in staff


def test_skill_text_keeps_rows_for_allowed_tools():
    full = skill_text("data-change")
    assert "`prepare_contract_save`" in full
    assert "`prepare_contract_save`" not in skill_text("data-change", {"prepare_account_id"})


def test_tool_specs_filter_by_name():
    names = {t["name"] for t in tool_specs(include_planned=True, names={"find_people", "monthly_purchase_needs"})}
    assert names == {"find_people", "monthly_purchase_needs"}


def test_specialist_gets_only_its_tools(service):
    client = ScriptedClient([tool_call("find_people", name="이가상"),
                             tool_call("checklist_status", person_id="DEMO-002"), text("ID카드 신청은 미진행입니다.")])
    result = ask(service, "이가상 근무 환경 준비 어디까지 됐어?", client, agent="environment")
    assert result["ok"] and result["agent"] == "environment" and result["agent_label"] == "근무 환경 준비"
    tools = {t["name"] for t in client.seen[0][1]}
    assert tools == set(AGENTS["environment"].tools)
    assert "[업무 지침: work-environment-checklist]" in client.seen[0][0][0]["content"]
    assert json.loads(service.run_log()[0]["input_json"])["kwargs"] == {"agent": "environment"}


def test_tool_outside_category_is_blocked(service):
    before = service.contracts("DEMO-002")
    client = ScriptedClient([tool_call("prepare_contract_delete", person_id="DEMO-002"),
                             text("구매계약 카테고리에서 요청해 주세요.")])
    result = ask(service, "이가상 계약 삭제해줘", client, agent="staff")
    call = result["calls"][0]
    assert not call["ok"] and "쓸 수 없는 도구" in call["error"]
    assert "쓸 수 없는 도구" in client.seen[1][0][-1]["content"]        # LLM에도 실패를 알린다
    assert service.contracts("DEMO-002") == before
    assert not [r for r in service.run_log() if r["operation"] == "tool:prepare_contract_delete"]


def test_auto_agent_keeps_all_tools(service):
    client = ScriptedClient([text("안녕하세요.")])
    result = ask(service, "안녕", client)
    assert result["agent"] == AUTO
    assert {t["name"] for t in client.seen[0][1]} == set(TOOLS)


def test_eval_questions_are_tagged_with_existing_agents():
    spec = json.loads((ROOT / "evals" / "questions.json").read_text(encoding="utf-8"))
    for q in spec["questions"]:
        agent = AGENTS[q["agent"]]
        assert agent.tools and {s["tool"] for s in q["steps"]} <= set(agent.tools), q["id"]


def test_category_routing_in_eval_harness():
    class Recorder:
        name = "fake:router"

        def __init__(self):
            self.prompts = []

        def chat(self, messages, tools):
            self.prompts.append((messages[0]["content"], {t["name"] for t in tools}))
            return text("모르겠습니다.")

    client = Recorder()
    report = run(AgentPlanner(client, "category"), save=False)
    assert report["planner"] == "fake-router-category"
    rows = {r["id"]: r for r in report["results"]}
    assert rows["Q7"]["agent_used"] == "inspection" and rows["Q9"]["agent_used"] == "environment"
    assert any("[업무 지침: inspection-amount]" in p for p, _ in client.prompts)
    with pytest.raises(ValueError):
        AgentPlanner(client, "router")


def test_request_outside_category_is_not_turned_into_another_change(service):
    """인력관리 Agent에 계약 삭제를 요청하자 모델이 계정 ID 입력으로 대신 처리하던 문제 (PC 확인 10/18)."""
    client = ScriptedClient([tool_call("prepare_account_id", person_id="박가상", account_id="park.fake"),
                             text("구매계약 카테고리에서 요청해 주세요.")])
    result = ask(service, "박가상 계약 삭제해줘", client, agent="staff")
    call = result["calls"][0]
    assert not call["ok"] and "사용자가 말한 값이 아닙니다" in call["error"] and not call["data"]
    assert "할 수 없는 요청을 비슷한 다른 도구로 대신 처리하지 않는다" in client.seen[0][0][0]["content"]
    assert not [r for r in service.run_log() if r["operation"] == "tool:prepare_account_id"]


def test_name_is_not_accepted_as_account_id(service):
    client = ScriptedClient([tool_call("prepare_account_id", person_id="박가상", account_id="박가상"), text("?")])
    assert not ask(service, "박가상 계약 삭제해줘", client, agent="staff")["calls"][0]["ok"]


def test_values_the_user_said_still_make_a_card(service):
    client = ScriptedClient([tool_call("prepare_account_id", person_id="이가상", account_id="Lee.Fake"),
                             text("확인 카드에서 '확인 후 저장'을 눌러 주세요.")])
    call = ask(service, "이가상 계정 ID lee.fake로 넣어줘", client, agent="staff")["calls"][0]
    assert call["ok"] and call["data"]["plan"]["action"] == "account_id"
    earlier = [{"role": "user", "content": "이가상 계정 ID는 lee.fake야"}, {"role": "assistant", "content": "네"}]
    client = ScriptedClient([tool_call("prepare_account_id", person_id="이가상", account_id="lee.fake"), text("확인")])
    assert ask(service, "그걸로 넣어줘", client, agent="staff", history=earlier)["calls"][0]["ok"]


def test_out_of_scope_prompt_names_the_right_category():
    prompt = system_prompt(AGENTS["staff"], "2026-10-18")
    assert "- 구매계약: 구매계약 기간·종료 예정·만료 알림 조회, 계약 등록·수정·삭제, 알림 확인" in prompt
    assert "이 요청은 인력관리 카테고리에서 처리할 수 없습니다." in prompt
    assert "- 인력관리:" not in prompt                         # 자기 자신은 목록에 없음
    assert all(a.scope for a in AGENTS.values() if a.key != AUTO)


def test_answer_does_not_leak_tool_names(service):
    """PC 확인 10/18: 모델이 지시문과 도구 이름을 답변에 그대로 옮겨 적음."""
    client = ScriptedClient([text("도구가 `ok: false`이면 오류를 설명한다. '계약 삭제' 요청은 `prepare_*` 도구로 "
                                  "처리할 수 없습니다. prepare_contract_delete는 구매계약에서 씁니다.")])
    answer = ask(service, "박가상 계약 삭제해줘", client, agent="staff")["answer"]
    assert "prepare_" not in answer and "ok: false" not in answer and "`" not in answer
    assert "'구매계약 삭제'" in answer
