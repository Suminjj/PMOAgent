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
    scope: str = ""                    # 다른 Agent가 안내할 때 쓰는 담당 업무 한 줄
    # 도구가 모두 '구현 예정'이면 LLM을 부르지 않고 이 안내를 그대로 답한다(작은 모델이 엉뚱한 카테고리를 안내하던 문제).
    planned_notice: str = ""


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
              ), "인력 찾기, 현재·과거·예정 투입 과제와 기간, 계정 ID 입력"),
    AgentSpec("environment", "근무 환경 준비",
              "너는 근무 환경 준비 담당 Agent다. 방문예약·계정 신청·ID카드 신청·OA기기 신청의 현황 조회와 "
              "상태 변경 준비를 맡는다.",
              ("find_people", "checklist_status", "query_database", "prepare_checklist_update"),
              ("work-environment-checklist", "data-change"), (
                  "이가상 근무 환경 준비 어디까지 됐어?",
                  "준비가 남은 투입 알려줘",
                  "이가상 ID카드 신청 진행 중으로 바꿔줘",
              ), "방문예약·계정 신청·ID카드 신청·OA기기 신청 현황과 상태 변경"),
    AgentSpec("contract", "구매계약",
              "너는 구매계약 담당 Agent다. 인력의 계약 기간, 월·업체별 종료 예정, 미확인 만료 알림 조회와 "
              "계약 등록·수정·삭제, 알림 확인 준비를 맡는다.",
              ("find_people", "list_contracts", "contracts_ending", "pending_contract_alerts", "query_database",
               "monthly_purchase_needs", "prepare_contract_save", "prepare_contract_delete", "prepare_alert_ack"),
              ("contract-expiry", "data-change"), (
                  "박가상 구매계약 언제까지야?",
                  "베타컨설팅의 10월 계약 종료 예정 인력 알려줘.",
                  "처리 안 한 계약 만료 알림 있어?",
              ), "구매계약 기간·종료 예정·만료 알림 조회, 계약 등록·수정·삭제, 알림 확인"),
    AgentSpec("inspection", "검수",
              "너는 검수 담당 Agent다. 업체의 월 검수 금액을 매출귀속 프로젝트별로 안내한다. "
              "계산 규칙이 아직 정해지지 않아 도구는 '구현 예정'이다.",
              ("inspection_amount_by_revenue_project",),
              ("inspection-amount",), (
                  "베타컨설팅의 9월 검수 금액을 매출귀속 프로젝트별로 알려줘.",
              ), "업체별 월 검수 금액",
              "검수 금액 계산은 아직 구현 예정입니다. 매출귀속 프로젝트별 배분·반올림·부가세 규칙이 정해지지 않아 "
              "금액을 계산하거나 추정하지 않습니다. 다른 업무는 홈 화면에서 해당 카테고리를 골라 요청해 주세요."),
]}


def get_agent(key: str | None) -> AgentSpec:
    return AGENTS.get(key or AUTO) or AGENTS[AUTO]


def skill_text(name: str, tools: set[str] | None = None) -> str:
    """업무 지침 본문. 앞머리(frontmatter)와 '사용 시점'·'성공 기준'(개발·평가용) 절은 뺀다.

    tools를 주면 도구 표에서 그 Agent가 쓸 수 없는 도구의 행을 뺀다(작은 모델이 없는 도구를 부르지 않게).
    """
    text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    # 사용 시점(예시 질문)은 카테고리를 이미 골랐으니 빼서 프롬프트를 줄인다(CPU에서 응답 시간에 영향).
    text = re.sub(r"\n## (성공 기준|사용 시점)\n.*?(?=\n## |\Z)", "\n", text, flags=re.S)
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
    others = "\n".join(f"- {a.label}: {a.scope}" for a in AGENTS.values() if a.key not in (AUTO, agent.key))
    parts = [base, agent.role,
             "맡은 업무의 요청이면 기능이 '구현 예정'이어도 먼저 맡은 도구를 호출하고, 도구 결과로 답한다.\n"
             f"다른 카테고리가 맡는 업무:\n{others}\n"
             "요청이 위 목록의 다른 카테고리 업무일 때만 도구를 부르지 말고 아래처럼 한두 문장으로 답한다. "
             f"[카테고리]에는 위 목록에서 그 업무를 맡는 카테고리 이름을 넣는다. '{agent.label}'을(를) 고르라고 안내하지 않는다.\n"
             f"\"이 요청은 {agent.label} 카테고리에서 처리할 수 없습니다. 홈 화면에서 '[카테고리]'를 골라 다시 요청해 주세요.\"\n"
             "할 수 없는 요청을 비슷한 다른 도구로 대신 처리하지 않는다(예: 계약 삭제 요청에 계정 ID 입력 도구를 쓰지 않는다)."]
    parts += [f"[업무 지침: {name}]\n{skill_text(name, set(agent.tools))}" for name in agent.skills]
    return "\n\n".join(parts)


def allowed_tools(agent: AgentSpec) -> set[str] | None:
    return None if agent.tools is None else set(agent.tools)
