"""근무 환경 준비 체크리스트: 항목·상태 규칙과 신청 초안. DB에 의존하지 않는 순수 함수만 둔다.

AGENTS.md 5.2
- 항목: 좌석 배정·ID카드 신청·OA기기 신청·방문예약
- 상태: 미진행·진행 중·완료·해당 없음, 해당 없음은 사유 필수
- 신청 초안 생성과 실제 신청 완료를 구분한다. 실제 신청·예약을 하지 않았으면 완료로 표시하지 않는다.
"""
from pmo.errors import ValidationError

ITEMS = ("좌석 배정", "ID카드 신청", "OA기기 신청", "방문예약")
STATUSES = ("미진행", "진행 중", "완료", "해당 없음")
CLOSED = ("완료", "해당 없음")

_REQUEST = {
    "좌석 배정": "위 인력이 근무할 좌석 배정을 요청드립니다.",
    "ID카드 신청": "위 인력의 출입용 ID카드 발급을 신청합니다.",
    "OA기기 신청": "위 인력이 사용할 OA기기(노트북·모니터 등) 지급을 요청드립니다.",
    "방문예약": "계정·ID카드 발급 전 출입을 위해 방문 예약을 요청드립니다.",
}


def validate_change(item: str, status: str, reason: object, actual_done: bool) -> dict:
    """상태 변경 입력 검증. 저장할 {status, reason}을 돌려준다."""
    if item not in ITEMS:
        raise ValidationError("체크리스트 항목을 선택하세요.")
    if status not in STATUSES:
        raise ValidationError("상태는 미진행·진행 중·완료·해당 없음 중 선택하세요.")
    reason = str(reason or "").strip()
    if status == "해당 없음" and not reason:
        raise ValidationError("'해당 없음'으로 처리하려면 사유를 입력하세요.")
    if status == "완료" and not actual_done:
        raise ValidationError("실제 신청·예약을 마쳤는지 확인하세요. 초안만 만들었다면 '진행 중'으로 두세요.")
    return {"status": status, "reason": reason}


def make_draft(item: str, person: dict, assignment: dict) -> str:
    """사내 시스템에 붙여 넣을 신청 초안 문구. 실제 신청은 하지 않는다."""
    if item not in ITEMS:
        raise ValidationError("체크리스트 항목을 선택하세요.")
    lines = [
        f"[{item} 요청]",
        f"- 대상: {person['name']} ({person['company'] or '업체 미입력'}, {person['category']})",
        f"- 계정 ID: {person.get('account_id') or '발급 전'}",
        f"- 투입 과제: {assignment.get('project') or '미입력'}",
        f"- 투입 기간: {assignment['start_date']} ~ {assignment['end_date']}",
        f"- 근무 시작 예정일: {assignment['start_date']}",
        f"- 요청 사항: {_REQUEST[item]}",
        "",
        "※ PMOAgent가 만든 초안입니다. 실제 신청은 사내 시스템에서 직접 진행하세요.",
    ]
    return "\n".join(lines)
