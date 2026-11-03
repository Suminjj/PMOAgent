from pathlib import Path
from datetime import date

from streamlit.testing.v1 import AppTest

from pmo.service import Service

APP = Path(__file__).resolve().parents[1] / "app.py"


def open_app(page="신규 인력 등록"):
    """앱을 열고 메뉴를 고른다. 첫 화면은 홈(AI Agent)이다."""
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    go(app, page)
    return app


def go(app, page):
    """왼쪽 메뉴 버튼(nav_0~nav_8)을 누른다."""
    from ui.theme import PAGES
    app.button(key=f"nav_{PAGES.index(page)}").click().run()


def find(elements, label):
    return next(e for e in elements if e.label == label)


def preview_person(app):
    find(app.text_input, "이름").set_value("가상 화면 인력")
    find(app.text_input, "업체명").set_value("가상 화면 업체")
    find(app.selectbox, "분류").set_value("협력")
    find(app.selectbox, "투입 등급").set_value("L6")
    find(app.date_input, "투입 시작일").set_value(date(2026, 10, 1))
    find(app.date_input, "투입 종료일").set_value(date(2026, 10, 31))
    app.button(key="preview_person").click().run()


def test_person_preview_cancel_save_edit_restart(monkeypatch, tmp_path):
    path = tmp_path / "ui.db"
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    app = open_app()
    assert not app.exception
    preview_person(app)
    assert not Service(path).people()
    app.button(key="cancel").click().run()
    assert not Service(path).people()
    app.button(key="preview_person").click().run()
    app.button(key="confirm").click().run()
    assert not app.exception
    assert len(Service(path).people()) == 1
    app.run()
    assert len(Service(path).history()) == 2
    person_id = Service(path).people()[0]["id"]
    assert Service(path).people()[0]["account_id"] is None
    assert Service(path).assignments()[0]["purchase_rate"] is None
    assert Service(path).assignments()[0]["grade"] == "L6"
    go(app, "전체 인력 조회")                                        # 수정·계정 ID 보완은 전체 인력 조회에서
    app.selectbox(key="person_target").set_value(person_id).run()
    find(app.text_input, "이름").set_value("가상 화면 수정")
    find(app.text_input, "계정 ID (발급 후 입력 / 선택)").set_value("fake-account")
    app.button(key="preview_person").click().run()
    assert Service(path).people()[0]["name"] == "가상 화면 인력"
    app.button(key="confirm").click().run()
    assert Service(path).people()[0]["name"] == "가상 화면 수정"
    assert Service(path).people()[0]["account_id"] == "fake-account"
    assert Service(path).assignments()[0]["person_id"] == person_id
    restarted = open_app("전체 인력 조회")
    assert not restarted.exception
    assert len(restarted.dataframe[0].value) == 1                    # 재시작 후에도 인력 1명


def test_assignment_preview_validation_save_and_edit(monkeypatch, tmp_path):
    path = tmp_path / "ui.db"
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    service = Service(path)
    service.save_person(dict(id="FAKE-UI-001", name="가상 인력", company="", category="정"),
                        token="fake-person-request", actor="테스트")
    app = open_app()
    go(app, "과제 투입 관리")
    app.button(key="preview_assignment").click().run()
    assert app.error and not service.assignments()
    find(app.text_input, "과제명 (선택)").set_value("가상 화면 과제")
    find(app.selectbox, "등급").set_value("L6")
    find(app.date_input, "시작일").set_value(date(2026, 10, 1))
    find(app.date_input, "종료일").set_value(date(2026, 10, 31))
    find(app.text_input, "매출단가 (선택)").set_value("2000000.10")
    find(app.text_input, "매입단가 (선택)").set_value("1000000")
    find(app.text_input, "단가 기준 단위 (예: 원/MM, 원/일)").set_value("원/MM")
    app.button(key="preview_assignment").click().run()
    assert not service.assignments()
    app.button(key="confirm").click().run()
    assert not app.exception
    assert len(service.assignments()) == 1
    saved = service.assignments()[0]
    app.selectbox(key="assignment_target_FAKE-UI-001").set_value(saved["id"]).run()
    find(app.text_input, "과제명 (선택)").set_value("수정된 가상 과제")
    app.button(key="preview_assignment").click().run()
    app.button(key="confirm").click().run()
    assert not app.exception
    assert service.assignments()[0]["project"] == "수정된 가상 과제"
    go(app, "변경 이력")
    assert len(app.expander) == 3
