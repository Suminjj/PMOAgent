# MVP 개발 계획 (이틀)

완료 기준: 로컬에서 가상 인력·투입·계약 정보를 등록·조회·수정하고, 계약 종료 예정 목록과 알림을 확인할 수 있다.

## 1일차 — 데이터·업무 로직

| 순서 | 작업 | 상태 |
| --- | --- | --- |
| 1 | 기존 저장소 확인 (인력·투입 등록·수정, 변경 이력, MM 계산은 이미 구현) | 완료 |
| 2 | DB v4: 구매계약·계약 알림·실행 기록 테이블, 투입의 매출귀속 프로젝트 (백업 후 마이그레이션) | 완료 |
| 3 | 계약 등록·수정·연장, 월·업체별 종료 예정 조회, 14일 전 알림·재확인·확인 처리 | 완료 |
| 4 | 현재·과거·예정 투입 조회 | 완료 |
| 5 | 업무 로직 자동 테스트 | 완료 |

## 2일차 — 화면·하네스·문서

| 순서 | 작업 | 상태 |
| --- | --- | --- |
| 6 | 화면: 구매계약 관리, 계약 종료 알림, 시작 시 알림 배너, 투입 현재·과거 구분, 실행 기록 | 완료 |
| 7 | Agent 도구 계층(`pmo/tools.py`)과 평가 질문·하네스(oracle) | 완료 |
| 8 | 가상 데이터 시드, AGENTS.md·CLAUDE.md, 개발 스킬·서비스 업무 지침, README | 완료 |
| 9 | 로컬 LLM(Ollama) 연결 자연어 조회, 모델 비교 평가 | 완료 |

## 폴더 구조

```
app.py                     Streamlit 화면 (입력·미리보기·확인·조회)
pmo/
  config.py                DB 경로, 등급, 기준일(Asia/Seoul), 알림 일수
  db.py                    SQLite 연결·스키마·마이그레이션(v1→v4, 백업)
  errors.py                ValidationError, ConflictError
  service.py               업무 함수 (인력·투입·계약·알림·이력·실행 기록)
  contracts.py             계약 검증·알림 기준 계산 (DB 없는 순수 함수)
  mm.py                    Full time MM 계산
  tools.py                 Agent 도구 계층 (입력 스키마·결과 형식)
  agent/                   LLM 연결(llm.py), 프롬프트(prompts.py), 실행 루프(runner.py)
  harness.py               평가 하네스 (Planner, 질문별 결과 기록)
  demo.py                  가상 데이터
scripts/
  seed_demo.py             가상 데이터 넣기
  run_evals.py             평가 질문 실행 (--model로 로컬 LLM 비교)
  ask.py                   터미널 자연어 질문
evals/questions.json       대표 질문·정답 도구·기대 결과
agent_skills/              서비스 Agent 업무 지침 (부분 연결)
.claude/skills/            Claude Code 개발 스킬
docs/                      데이터 설계, 개발 계획
tests/                     자동 테스트
```

## 데이터 구조 요약

```
people 1 ── N assignments ── N checklist
   │             │
   │ 1           │ 0..1
   N             │
contracts ───────┘   (contracts.assignment_id → assignments.id)
   │  └─ previous_contract_id → contracts.id  (연장)
   1
   N
contract_alerts   (계약 × 기준 종료일마다 1건, 유일 제약)

audit     모든 등록·수정·알림 확인의 전후 값
run_log   업무 함수·도구 호출 기록
requests  저장 요청 토큰 (중복 저장 방지)
```

## 다음 단계 제안

1. 체크리스트 상태 변경·해당 없음 사유 화면
2. 로컬 모델 비교 후 기본 모델 확정, 평가 질문 확대, 업무 지침(agent_skills) 프롬프트 주입
3. 구매 필요 판정·검수 금액 규칙 확정 후 해당 도구 구현
4. 인력·계약·검수 전문 Agent 분리 (멀티에이전트), 업무 지침 RAG 검색
