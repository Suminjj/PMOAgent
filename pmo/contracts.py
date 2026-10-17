"""구매계약 검증과 만료 알림 기준 계산. DB에 의존하지 않는 순수 함수만 둔다."""
from datetime import date, timedelta

from pmo.config import CONTRACT_ALERT_DAYS
from pmo.errors import ValidationError

CONTRACT_STATUSES = ("준비", "확정")
# 구매계약 대상 분류 (AGENTS.md 5.3: 컨설팅·협력 인력의 계약 정보)
CONTRACT_CATEGORIES = ("협력", "컨설팅")


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _date(value: object, label: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label}을(를) 올바르게 입력하세요.") from exc


def validate_contract(data: dict) -> dict:
    """화면·도구 공통 입력 검증. DB가 필요한 연결 관계 검증은 저장 트랜잭션에서 한다."""
    person_id = _text(data.get("person_id"))
    if not person_id:
        raise ValidationError("계약 대상 인력을 선택하세요.")
    company = _text(data.get("company"))
    if not company:
        raise ValidationError("업체명을 입력하세요.")
    start = _date(data.get("start_date"), "계약 시작일")
    end = _date(data.get("end_date"), "계약 종료일")
    if end < start:
        raise ValidationError("계약 종료일은 시작일보다 빠를 수 없습니다.")
    status = data.get("status")
    if status not in CONTRACT_STATUSES:
        raise ValidationError("계약 상태는 준비·확정 중 선택하세요.")
    contract_no = _text(data.get("contract_no"))
    if status == "확정" and not contract_no:
        raise ValidationError("확정 계약은 계약번호가 필요합니다. 발급 전이면 '준비'로 저장하세요.")
    result = {
        "person_id": person_id, "assignment_id": _text(data.get("assignment_id")),
        "company": company, "contract_no": contract_no,
        "start_date": start, "end_date": end, "status": status,
        "previous_contract_id": _text(data.get("previous_contract_id")),
        "note": _text(data.get("note")),
    }
    if data.get("id"):
        result["id"] = str(data["id"]).strip()
    return result


def notify_from(end_date: str) -> str:
    """알림 시작일 = 계약 종료일 - 14일 (종료일 변경 시 다시 계산된다)."""
    return (date.fromisoformat(end_date) - timedelta(days=CONTRACT_ALERT_DAYS)).isoformat()


def expiry_state(end_date: str, today: date) -> dict:
    """화면·도구에 표시할 만료 상태. 알림 대상이 아니면 in_window=False."""
    end = date.fromisoformat(end_date)
    days_left = (end - today).days
    if days_left < 0:
        label = f"만료 {-days_left}일 경과"
    elif days_left == 0:
        label = "오늘 종료"
    else:
        label = f"종료 D-{days_left}"
    return {"days_left": days_left, "label": label,
            "in_window": today.isoformat() >= notify_from(end_date)}


def month_range(year: int, month: int) -> tuple[str, str]:
    if not 1 <= month <= 12:
        raise ValidationError("월은 1~12 사이로 입력하세요.")
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()
