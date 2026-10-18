"""업무 카테고리별 전문 Agent 정의 (수동 라우팅 멀티에이전트).

사용자가 홈 화면에서 카테고리를 고르면 그 Agent만 동작한다.
- 도구: 업무에 필요한 도구만 넘긴다. 목록 밖 도구를 부르면 실행하지 않고 실패 결과를 돌려준다.
- 지침: agent_skills/ 의 업무 지침 본문을 시스템 프롬프트 뒤에 붙인다(공통 규칙은 prompts.py).
- '자동(전체)'은 기존 단일 Agent다. 모든 도구를 쓰고 업무 지침은 넣지 않는다(비교 기준).

Agent를 추가하려면 AGENTS 에 한 줄을 더하고, 필요하면 agent_skills/ 에 지침을 만든다.
LLM 제공자·모델과 무관하다. 질문에 맞는 Agent를 자동으로 고르는 라우터는 다음 단계다.
"""
import re
from dataclasses import dataclass

from pmo.agent.prompts import SYSTEM_PROMPT
from pmo.config import ROOT

SKILLS_DIR = ROOT / "agent_skills"
AUTO = "auto"


@dataclass(frozen=True)
class AgentSpec:
    key: str
    label: str
    role: str                          # 시스템 프롬프트에 붙는 역할 설명
    tools: tuple[str, ...] | None      # None이면 모든 도구
    skills: tuple[str, ...] = ()       # 주입할 agent_skills/<이름>/SKILL.md
    examples: tuple[str, ...] = ()


AGENTS: dict[str, AgentSpec] = {a.key: a for a in [
    AgentSpec(AUTO, "자동(전체)", "", None, (), (
        "처리 안 한 계약 만료 알림 있어?",
        "이가상 지금 무슨 과제에 투입되어 있어?",
        "협력 인력 중 계정 ID가 없는 사람 몇 명이야?",
        "이가상 ID카드 신청 진행 중으로 바꿔줘",
    )),
    AgentSpec("staff", "인력관리",
              "너는 인력관리 담당 Agent다. 인력 찾기, 현재·과거·예정 투입 과제와 기간, 계정 ID 입력을 맡는다.",
              ("find_people", "get_assignment_timeline", "query_database", "prepare_account_id"),
              ("staff-assignment-lookup", "data-change"), (
                  "이가상 지금 무슨 과제에 투입되어 있어?",
                  "김가상의 CoE 전체 투입 과제와 기간을 알려줘.",
                  "이가상 계정 ID lee.fake로 넣어줘",
              )),
    AgentSpec("environment", "근무 환경 준비",
              "너는 근무 환경 준비 담당 Agent다. 좌석 배정·ID카드 신청·OA기기 신청·방문예약의 현황 조회와 "
              "상태 변경 준비를 맡는다.",
              ("find_people", "checklist_status", "query_database", "prepare_checklist_update"),
              ("work-environment-checklist", "data-change"), (
                  "이가상 근무 환경 준비 어디까지 됐어?",
                  "준비가 남은 투입 알려줘",
                  "이가상 ID카드 신청 진행 중으로 바꿔줘",
              )),
    AgentSpec("contract", "구매계약",
              "너는 구매계약 담당 Agent다. 인력의 계약 기간, 월·업체별 종료 예정, 미확인 만료 알림 조회와 "
              "계약 등록·수정·삭제, 알림 확인 준비를 맡는다.",
              ("find_people", "list_contracts", "contracts_ending", "pending_contract_alerts", "query_database",
               "monthly_purchase_needs", "prepare_contract_save", "prepare_contract_delete", "prepare_alert_ack"),
              ("contract-expiry", "data-change"), (
                  "박가상 구매계약 언제까지야?",
                  "베타컨설팅의 10월 계약 종료 예정 인력 알려줘.",
                  "처리 안 한 계약 만료 알림 있어?",
              )),
    AgentSpec("inspection", "검수",
              "너는 검수 담당 Agent다. 업체의 월 검수 금액을 매출귀속 프로젝트별로 안내한다. "
              "계산 규칙이 아직 정해지지 않아 도구는 '구현 예정'이다.",
              ("inspection_amount_by_revenue_project",),
              ("inspection-amount",), (
                  "베타컨설팅의 9월 검수 금액을 매출귀속 프로젝트별로 알려줘.",
              )),
]}


def get_agent(key: str | None) -> AgentSpec:
    return AGENTS.get(key or AUTO) or AGENTS[AUTO]


def skill_text(name: str, tools: set[str] | None = None) -> str:
    """업무 지침 본문. 앞머리(frontmatter)와 '성공 기준'(개발·평가용) 절은 뺀다.

    tools를 주면 도구 표에서 그 Agent가 쓸 수 없는 도구의 행을 뺀다(작은 모델이 없는 도구를 부르지 않게).
    """
    text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    text = re.sub(r"\n## 성공 기준\n.*?(?=\n## |\Z)", "\n", text, flags=re.S)
    if tools is not None:
        def keep(line: str) -> bool:
            m = re.match(r"\| `(\w+)` \|", line)
            return not m or m.group(1) in tools
        text = "\n".join(line for line in text.split("\n") if keep(line))
    return text.strip()


def system_prompt(agent: AgentSpec, today: str) -> str:
    base = SYSTEM_PROMPT.format(today=today)
    if agent.tools is None:
        return base
    others = ", ".join(a.label for a in AGENTS.values() if a.key not in (AUTO, agent.key))
    parts = [base, agent.role,
             f"맡은 업무 밖의 요청이면 도구를 부르지 말고, 홈 화면에서 다른 카테고리({others} 또는 자동(전체))를 "
             "골라 다시 요청해 달라고 안내한다. 할 수 없는 요청을 비슷한 다른 도구로 대신 처리하지 않는다"
             "(예: 계약 삭제 요청에 계정 ID 입력 도구를 쓰지 않는다)."]
    parts += [f"[업무 지침: {name}]\n{skill_text(name, set(agent.tools))}" for name in agent.skills]
    return "\n\n".join(parts)


def allowed_tools(agent: AgentSpec) -> set[str] | None:
    return None if agent.tools is None else set(agent.tools)
