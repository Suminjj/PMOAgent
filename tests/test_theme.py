"""화면 테마(시안 A 네이비 콘솔) 도우미 테스트. 모양 자체는 PC 화면에서 확인한다."""
from pathlib import Path
import tomllib

import pytest

pytest.importorskip("streamlit")
from ui import theme  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_config_sets_navy_console_theme():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["theme"]["primaryColor"] == "#1D4ED8"
    assert config["theme"]["sidebar"]["backgroundColor"] == "#0F1B33"


def test_nav_label_keeps_page_value_and_shows_alert_badge():
    assert theme.nav_label("홈") == "홈 · AI Agent"
    assert theme.PAGES[0] == "홈" and len(theme.PAGES) == 7
    assert theme.nav_label("계약 종료 알림", 2).endswith(":orange-badge[2]")
    assert "badge" not in theme.nav_label("계약 종료 알림", 0)


def test_css_targets_keyed_panels_and_confirm_card():
    for selector in (".st-key-home_chat_panel", ".st-key-home_agent", 'div[class*="st-key-act"][class*="_card"]'):
        assert selector in theme._CSS
