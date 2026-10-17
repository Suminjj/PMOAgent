# CLAUDE.md

이 저장소의 개발 지침은 `AGENTS.md`에 있다. Claude Code도 아래 문서를 그대로 따른다.

@AGENTS.md

## Claude Code 적용 메모

- `AGENTS.md`의 "Codex"는 이 저장소를 개발하는 AI 도구를 뜻한다. Claude Code로 작업할 때도 9장 "Codex 작업 방식"을 동일하게 적용한다.
- 기능 구현·수정·검증 작업 전에는 `.agents/skills/pmo-feature-development/SKILL.md`를 읽고 그 절차를 따른다.
- 업무 규칙은 `AGENTS.md`가 기준이다. 이 파일에 규칙을 따로 추가하지 않고, 규칙 변경은 `AGENTS.md`에 반영한다.
- 작업은 새 브랜치에서 진행하고 Pull Request로 올린다. `main`에 직접 push하지 않는다.
- 커밋 전에 테스트를 실행하고 결과를 보고한다.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```
