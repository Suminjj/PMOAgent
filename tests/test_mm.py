import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from pmo.mm import calculate_full_time
from pmo.service import Service, ValidationError
from tests.test_app import APP, find, open_app, preview_person
from tests.test_intake import intake


def test_october_user_examples_and_holidays():
    partial = calculate_full_time(date(2026, 10, 1), date(2026, 10, 10))
    assert partial["mm"] == "0.250"
    assert partial["months"][0]["assigned_days"] == 5
    assert partial["months"][0]["working_days"] == 20
    assert [d["date"] for d in partial["months"][0]["excluded_holidays"]] == [
        "2026-10-05", "2026-10-09"
    ]
    assert calculate_full_time(date(2026, 10, 1), date(2026, 10, 31))["mm"] == "1.000"
    assert calculate_full_time(date(2026, 10, 3), date(2026, 10, 5))["mm"] == "0.000"


def test_cross_month_year_and_leap_year():
    assert calculate_full_time(date(2026, 10, 1), date(2026, 11, 30))["mm"] == "2.000"
    assert calculate_full_time(date(2026, 12, 1), date(2027, 1, 31))["mm"] == "2.000"
    assert calculate_full_time(date(2024, 2, 1), date(2024, 2, 29))["mm"] == "1.000"
    parts = calculate_full_time(date(2026, 10, 30), date(2026, 11, 2))
    assert [(r["assigned_days"], r["working_days"]) for r in parts["months"]] == [(1, 20), (1, 21)]
    assert parts["exact_fraction"] == "41/420"


def test_extra_holiday_and_invalid_calendar(monkeypatch):
    monkeypatch.setenv("PMO_EXTRA_HOLIDAYS", "2026-10-02")
    data = calculate_full_time(date(2026, 10, 1), date(2026, 10, 10))
    assert data["months"][0]["assigned_days"] == 4
    assert data["months"][0]["working_days"] == 19
    monkeypatch.setenv("PMO_EXTRA_HOLIDAYS", "invalid")
    with pytest.raises(ValueError):
        calculate_full_time(date(2026, 10, 1), date(2026, 10, 10))
    monkeypatch.setenv("PMO_EXTRA_HOLIDAYS", ",".join(
        (date(2026, 10, 1) + timedelta(days=i)).isoformat() for i in range(31)))
    with pytest.raises(ValueError, match="근무일이 없어"):
        calculate_full_time(date(2026, 10, 1), date(2026, 10, 10))


@pytest.mark.parametrize("mm", ["", "-0.1", "NaN", "Infinity", "text"])
def test_manual_mm_invalid_is_not_zero(tmp_path, mm):
    service = Service(tmp_path / "manual.db")
    data = intake()
    data["assignment"].update(assignment_type="Part time", mm=mm)
    with pytest.raises(ValidationError):
        service.save_intake(data, token="test-manual", actor="테스트")
    assert not service.people()


def test_manual_mm_and_full_time_recomputed_on_date_change(tmp_path):
    service = Service(tmp_path / "types.db")
    data = intake()
    data["assignment"].update(assignment_type="Part time", mm="0.123456789")
    service.save_intake(data, token="manual", actor="테스트")
    saved = service.assignments()[0]
    assert saved["mm"] == "0.123456789"
    service.save_assignment(dict(saved, assignment_type="Full time", start_date="2026-10-01",
                                 end_date="2026-10-10", mm="999"),
                            assignment_id=saved["id"], expected_version=1, token="full", actor="테스트")
    changed = service.assignments()[0]
    assert changed["mm"] == "0.250"  # Never trust a supplied automatic result.
    detail = json.loads(changed["mm_details"])
    assert detail["exact_fraction"] == "1/4" and detail["inclusive_dates"]
    assert json.loads(service.history()[0]["before_json"])["mm"] == "0.123456789"


def test_ui_live_mm_manual_switch_and_preview_invalidated(monkeypatch, tmp_path):
    path = tmp_path / "ui-mm.db"
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    app = open_app()
    preview_person(app)
    find(app.date_input, "투입 종료일").set_value(date(2026, 10, 10)).run()
    assert "0.250" in [m.value for m in app.metric]
    assert not any(b.key == "confirm" for b in app.button)
    app.button(key="preview_person").click().run()
    app.button(key="confirm").click().run()
    assert Service(path).assignments()[0]["mm"] == "0.250"
    find(app.selectbox, "투입 Type").set_value("Part time").run()
    assert not app.metric
    find(app.text_input, "투입 MM (수동)").set_value("0.4")
    app.button(key="preview_person").click().run()
    app.button(key="confirm").click().run()
    assert not app.exception
    assert sorted(Decimal(a["mm"]) for a in Service(path).assignments()) == [
        Decimal("0.25"), Decimal("0.4")]


def test_existing_v2_migration_keeps_mm_unknown_and_backup(tmp_path):
    path = tmp_path / "existing.db"
    schema = (Path(__file__).parent / "fixtures" / "schema-v2.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as con:
        con.executescript(schema)
        con.execute("INSERT INTO people VALUES ('FAKE-OLD','가상 인력','가상 업체','정',1,NULL)")
        con.execute("""INSERT INTO assignments VALUES
            ('FAKE-A','FAKE-OLD',NULL,'2026-10-01','2026-10-31','L6',NULL,NULL,NULL,1)""")
    service = Service(path)
    saved = service.assignments()[0]
    assert saved["id"] == "FAKE-A" and saved["assignment_type"] is None and saved["mm"] is None
    backups = list(tmp_path.glob("existing.db.backup-v2-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    Service(path)
    assert len(list(tmp_path.glob("existing.db.backup-v2-*.db"))) == 1
