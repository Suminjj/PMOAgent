from pathlib import Path

from streamlit.testing.v1 import AppTest

from pmo.service import Service

APP = Path(__file__).resolve().parents[1] / "app.py"


def find(elements, label):
    return next(e for e in elements if e.label == label)


def preview_person(app):
    find(app.text_input, "인력 ID").set_value("FAKE-UI-001")
    find(app.text_input, "이름").set_value("가상 화면 인력")
    app.button(key="preview_person").click().run()


def test_person_preview_cancel_save_edit_restart(monkeypatch, tmp_path):
    path = tmp_path / "ui.db"
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    app = AppTest.from_file(str(APP)).run()
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
    assert len(Service(path).history()) == 1
    app.selectbox(key="person_target").set_value("FAKE-UI-001").run()
    find(app.text_input, "이름").set_value("가상 화면 수정")
    app.button(key="preview_person").click().run()
    assert Service(path).people()[0]["name"] == "가상 화면 인력"
    app.button(key="confirm").click().run()
    assert Service(path).people()[0]["name"] == "가상 화면 수정"
    restarted = AppTest.from_file(str(APP)).run()
    assert not restarted.exception
    assert len(restarted.dataframe) == 1


def test_assignment_preview_validation_save_and_edit(monkeypatch, tmp_path):
    path = tmp_path / "ui.db"
    monkeypatch.setenv("PMO_DB_PATH", str(path))
    service = Service(path)
    service.save_person(dict(id="FAKE-UI-001", name="가상 인력", company="", category="정"),
                        token="fake-person-request", actor="테스트")
    app = AppTest.from_file(str(APP)).run()
    find(app.radio, "메뉴").set_value("투입 관리").run()
    app.button(key="preview_assignment").click().run()
    assert app.error and not service.assignments()
    find(app.text_input, "과제명").set_value("가상 화면 과제")
    find(app.text_input, "매출단가").set_value("2000000.10")
    find(app.text_input, "매입단가").set_value("1000000")
    find(app.text_input, "단가 기준 단위 (예: 원/MM, 원/일)").set_value("원/MM")
    app.button(key="preview_assignment").click().run()
    assert not service.assignments()
    app.button(key="confirm").click().run()
    assert not app.exception
    assert len(service.assignments()) == 1
    saved = service.assignments()[0]
    app.selectbox(key="assignment_target_FAKE-UI-001").set_value(saved["id"]).run()
    find(app.text_input, "과제명").set_value("수정된 가상 과제")
    app.button(key="preview_assignment").click().run()
    app.button(key="confirm").click().run()
    assert not app.exception
    assert service.assignments()[0]["project"] == "수정된 가상 과제"
    find(app.radio, "메뉴").set_value("변경 이력").run()
    assert len(app.expander) == 3
