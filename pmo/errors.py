"""업무 함수 공통 오류. 화면과 향후 Agent 도구가 같은 오류를 처리한다."""


class ValidationError(ValueError):
    """입력값·연결 관계 검증 실패. 저장하지 않는다."""


class ConflictError(ValueError):
    """오래된 미리보기 또는 같은 요청 ID의 다른 내용. 다시 조회해야 한다."""
