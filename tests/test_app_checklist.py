"""근무 환경 준비 화면 테스트 (Streamlit AppTest, 가상 데이터)."""
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from pmo.config import today  # noqa: E402
from pmo.demo import seed_demo  # noqa: E402
from pmo.service import Service  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def ready(monkeypatch, tmp_path):
    path = tmp_path / "ui-checklist.db"
    seed_demo(Service(path), today())
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.sidebar.radio[0].set_value("근무 환경 준비").run()
    assert not app.exception
    aid = Service(path).checklist_overview()[0]["id"]
    return path, app, aid


def test_not_applicable_needs_reason_then_saves_after_confirm(ready):
    path, app, aid = ready
    app.selectbox(key=f"ck_status_{aid}_3").set_value("해당 없음")
    app.button(key=f"ck_preview_{aid}_3").click().run()
    assert any("사유" in e.value for e in app.error)
    app.text_input(key=f"ck_reason_{aid}_3").set_value("기존 출입증 보유")
    app.button(key=f"ck_preview_{aid}_3").click().run()
    assert Service(path).checklist(aid)[3]["status"] == "미진행"   # 미리보기만으로는 저장 안 함
    app.button(key=f"ck_confirm_{aid}_3").click().run()
    assert not app.exception
    row = Service(path).checklist(aid)[3]
    assert row["status"] == "해당 없음" and row["reason"] == "기존 출입증 보유"


def test_done_requires_actual_application_check(ready):
    path, app, aid = ready
    app.selectbox(key=f"ck_status_{aid}_1").set_value("완료").run()
    app.button(key=f"ck_preview_{aid}_1").click().run()
    assert any("실제 신청" in e.value for e in app.error)
    app.checkbox(key=f"ck_actual_{aid}_1").check().run()
    app.button(key=f"ck_preview_{aid}_1").click().run()
    app.button(key=f"ck_confirm_{aid}_1").click().run()
    assert Service(path).checklist(aid)[1]["status"] == "완료"


def test_draft_keeps_status(ready):
    path, app, aid = ready
    app.button(key=f"ck_draft_{aid}_2").click().run()
    assert not app.exception
    row = Service(path).checklist(aid)[2]
    assert row["draft"] and row["status"] == "미진행"


def test_overview_table_shows_each_item_status(ready):
    path, app, aid = ready
    service = Service(path)
    item = service.checklist(aid)[1]
    service.update_checklist_item(aid, item["item"], "진행 중", token="ui-overview", actor="담당자",
                                  expected_version=item["version"])
    app.run()
    table = app.dataframe[0].value
    row = table[table["투입 관리번호"] == aid].iloc[0]
    assert row["ID카드 신청"] == "진행 중" and row["좌석 배정"] == "미진행"
    assert "남은 항목" not in table.columns
