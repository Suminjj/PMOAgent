# 서비스 Agent 업무 지침

이 폴더는 **향후 서비스 안에서 동작할 LLM Agent**에게 줄 업무 절차다.
저장소를 개발하는 Claude Code의 스킬(`.claude/skills/`)과 다르다.

## 연결 상태

| 지침 | 업무 | 서비스 연결 |
| --- | --- | --- |
| [staff-assignment-lookup](staff-assignment-lookup/SKILL.md) | 인력의 현재·과거 투입 조회 | 부분 연결 (도구 구현, 지침 본문 미주입) |
| [contract-expiry](contract-expiry/SKILL.md) | 구매계약 기간·종료 예정·만료 알림 조회 | 부분 연결 (도구 일부 구현, 지침 본문 미주입) |
| [work-environment-checklist](work-environment-checklist/SKILL.md) | 근무 환경 준비 현황 조회 | 부분 연결 (조회 도구 구현, 지침 본문 미주입) |
| [data-change](data-change/SKILL.md) | 상태 변경·계약 등록·수정·삭제·계정 ID·알림 확인 (확인 카드 준비) | 부분 연결 (prepare_* 도구 구현, 지침 본문 미주입) |

- **부분 연결**: 로컬 LLM Agent(`pmo/agent/`)가 연결되어 도구를 호출한다. 다만 Agent에는
  `pmo/agent/prompts.py`의 공통 규칙만 들어가고, 이 폴더의 지침 본문은 아직 프롬프트에 넣지 않는다.
  질문에 맞는 지침을 골라 넣는 기능(스킬 선택·RAG)은 다음 단계다. 문서가 있다고 Agent가 따르는 것이 아니다.
- 도구는 `pmo/tools.py`에 정의되어 있고, 구현 여부는 각 지침의 "연결 도구" 표를 따른다.
- 도구 이름·입력을 바꾸면 이 지침과 `evals/questions.json`을 함께 고친다.

## 공통 원칙

- 조회는 정해진 도구를 먼저 쓰고, 그 밖의 조회만 `query_database`(Text-to-SQL, SELECT만)를 쓴다. 결과를 추측하지 않는다.
- 저장·삭제는 하지 않는다. `prepare_*` 도구로 확인 카드를 준비하고, 사용자가 화면에서 확인해 저장한다.
- 이름만으로 인력을 확정하지 않는다. `find_people` 결과가 여러 명이면 업체·관리번호로 대상을 물어본다.
- 도구가 `ok: false`를 돌려주면 오류 내용을 그대로 설명하고, 성공한 것처럼 답하지 않는다.
- 날짜는 Asia/Seoul 기준이다. 답변에 기준일을 밝힌다.
