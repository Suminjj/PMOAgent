import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def db_path() -> Path:
    return Path(os.environ.get("PMO_DB_PATH", str(ROOT / "data" / "pmo.db")))


def grades() -> tuple[str, ...]:
    values = tuple(dict.fromkeys(
        part.strip() for part in os.environ.get("PMO_GRADES", "L7,L6,L5").split(",")
        if part.strip()
    ))
    return values or ("L7", "L6", "L5")
