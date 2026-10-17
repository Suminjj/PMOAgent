import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Asia/Seoul")
# 계약 종료일 기준 며칠 전부터 후속 검토 대상으로 표시할지 (AGENTS.md 확정 사항)
CONTRACT_ALERT_DAYS = 14


def today() -> date:
    """업무 날짜 판단은 Asia/Seoul 기준이다."""
    return datetime.now(TIMEZONE).date()

ROOT = Path(__file__).resolve().parents[1]


def db_path() -> Path:
    return Path(os.environ.get("PMO_DB_PATH", str(ROOT / "data" / "pmo.db")))


def grades() -> tuple[str, ...]:
    values = tuple(dict.fromkeys(
        part.strip() for part in os.environ.get("PMO_GRADES", "L7,L6,L5").split(",")
        if part.strip()
    ))
    return values or ("L7", "L6", "L5")
