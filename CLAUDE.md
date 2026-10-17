# CLAUDE.md

이 저장소의 개발 지침은 `AGENTS.md`에 있다. Claude Code는 아래 문서를 그대로 따른다.

@AGENTS.md

## Claude Code 적용 메모

- 업무 규칙과 작업 방식의 기준은 `AGENTS.md`다. 이 파일에 규칙을 따로 추가하지 않고, 규칙 변경은 `AGENTS.md`에 반영한다.
- 기능 구현·수정·검증 작업에는 `.claude/skills/pmo-feature-development` 스킬을 사용한다.
- `agent_skills/`는 서비스 Agent용 업무 지침이다. 개발 작업 지침으로 사용하지 않는다.
- 작업은 새 브랜치에서 진행하고 Pull Request로 올린다. `main`에 직접 push하지 않는다.
- 커밋 전에 테스트를 실행하고 결과를 보고한다.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m scripts.run_evals   # 평가 질문(oracle) 실행
```
