"""구매계약·알림 화면 흐름 테스트 (Streamlit AppTest, 가상 데이터)."""
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from pmo.config import today  # noqa: E402
from pmo.demo import seed_demo  # noqa: E402
from pmo.service import Service  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def demo_app(monkeypatch, tmp_path):
    path = tmp_path / "ui-contracts.db"
    seed_demo(Service(path), today())
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    return path, AppTest.from_file(str(APP), default_timeout=30).run()


def go(app, page):
    app.sidebar.radio[0].set_value(page).run()
    assert not app.exception


def test_startup_shows_pending_alert_banner(demo_app):
    _, app = demo_app
    assert any("계약 종료 검토 대상" in w.value for w in app.warning)


def test_acknowledge_alert_from_screen(demo_app):
    path, app = demo_app
    go(app, "계약 종료 알림")
    before = Service(path).pending_alerts()
    assert len(before) == 2
    app.button(key=f"ack_{before[0]['id']}").click().run()
    assert not app.exception
    assert len(Service(path).pending_alerts()) == 1


def test_register_contract_with_preview_and_confirm(demo_app):
    path, app = demo_app
    go(app, "구매계약 관리")
    suffix = "DEMO-001_None"
    app.selectbox(key=f"cstatus_{suffix}").set_value("확정")
    app.text_input(key=f"cno_{suffix}").set_value("UI-C-1")
    app.date_input(key=f"cstart_{suffix}").set_value(date(2030, 1, 1))
    app.date_input(key=f"cend_{suffix}").set_value(date(2030, 6, 30))
    app.button(key="preview_contract").click().run()
    assert not app.exception
    assert not [c for c in Service(path).contracts() if c["contract_no"] == "UI-C-1"]  # 미리보기만으로 저장 안 함
    app.button(key="confirm").click().run()
    assert not app.exception
    saved = [c for c in Service(path).contracts() if c["contract_no"] == "UI-C-1"]
    assert len(saved) == 1 and saved[0]["end_date"] == "2030-06-30"


def test_contract_without_number_cannot_be_confirmed(demo_app):
    path, app = demo_app
    go(app, "구매계약 관리")
    suffix = "DEMO-001_None"
    count = len(Service(path).contracts())
    app.selectbox(key=f"cstatus_{suffix}").set_value("확정")
    app.date_input(key=f"cstart_{suffix}").set_value(date(2030, 1, 1))
    app.date_input(key=f"cend_{suffix}").set_value(date(2030, 6, 30))
    app.button(key="preview_contract").click().run()
    assert any("계약번호" in e.value for e in app.error)
    assert len(Service(path).contracts()) == count
