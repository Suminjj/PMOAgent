# 서비스 Agent 업무 지침

이 폴더는 **향후 서비스 안에서 동작할 LLM Agent**에게 줄 업무 절차다.
저장소를 개발하는 Claude Code의 스킬(`.claude/skills/`)과 다르다.

## 연결 상태

| 지침 | 업무 | 주입되는 Agent | 서비스 연결 |
| --- | --- | --- | --- |
| [staff-assignment-lookup](staff-assignment-lookup/SKILL.md) | 인력의 현재·과거 투입 조회, 계정 ID 입력 | 인력관리 | 연결 |
| [work-environment-checklist](work-environment-checklist/SKILL.md) | 근무 환경 준비 현황 조회·상태 변경 | 근무 환경 준비 | 연결 |
| [contract-expiry](contract-expiry/SKILL.md) | 구매계약 기간·종료 예정·만료 알림, 계약 등록·수정·삭제·알림 확인 | 구매계약 | 연결 (구매 필요 판정은 구현 예정) |
| [inspection-amount](inspection-amount/SKILL.md) | 매출귀속 프로젝트별 검수 금액 | 검수 | 지침만 연결 (도구 구현 예정) |
| [data-change](data-change/SKILL.md) | 변경 요청 공통 규칙 (확인 카드 준비) | 인력관리·근무 환경 준비·구매계약 | 연결 |

- **연결**: 홈 화면에서 카테고리를 고르면 그 전문 Agent(`pmo/agent/agents.py`)의 시스템 프롬프트에 위 지침 본문이 들어간다.
  앞머리와 '성공 기준'(개발·평가용) 절, 그 Agent가 쓸 수 없는 도구의 표 행은 빼고 넣는다.
- **자동(전체)** Agent에는 지침을 넣지 않고 `pmo/agent/prompts.py`의 공통 규칙만 쓴다(비교 기준).
- 질문에 맞는 지침을 자동으로 고르는 기능(라우터·RAG)은 다음 단계다.
- 도구는 `pmo/tools.py`에 정의되어 있고, 구현 여부는 각 지침의 "연결 도구" 표를 따른다.
- 도구 이름·입력을 바꾸면 이 지침, `pmo/agent/agents.py`, `evals/questions.json`을 함께 고친다.

## 공통 원칙

- 조회는 정해진 도구를 먼저 쓰고, 그 밖의 조회만 `query_database`(Text-to-SQL, SELECT만)를 쓴다. 결과를 추측하지 않는다.
- 저장·삭제는 하지 않는다. `prepare_*` 도구로 확인 카드를 준비하고, 사용자가 화면에서 확인해 저장한다.
- 이름만으로 인력을 확정하지 않는다. `find_people` 결과가 여러 명이면 업체·관리번호로 대상을 물어본다.
- 도구가 `ok: false`를 돌려주면 오류 내용을 그대로 설명하고, 성공한 것처럼 답하지 않는다.
- 날짜는 Asia/Seoul 기준이다. 답변에 기준일을 밝힌다.
