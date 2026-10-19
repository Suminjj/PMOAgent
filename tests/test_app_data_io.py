"""'데이터 가져오기·내보내기' 화면이 열리는지 (Streamlit AppTest). 가져오기 로직은 tests/test_excel_io.py."""
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from pmo.config import today  # noqa: E402
from pmo.demo import seed_demo  # noqa: E402
from pmo.service import Service  # noqa: E402
from ui.theme import PAGES  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app.py"


def test_data_io_page_opens(monkeypatch, tmp_path):
    path = tmp_path / "io.db"
    seed_demo(Service(path), today())
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.button(key=f"nav_{PAGES.index('데이터 가져오기·내보내기')}").click().run()
    assert not app.exception
    assert [s.value for s in app.subheader][:3] == ["1. 양식 내려받기", "2. 파일 올리기와 미리보기",
                                                   "3. 현재 데이터 내려받기"]
    app.button(key="io_export_build").click().run()
    assert not app.exception
