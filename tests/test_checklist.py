"""근무 환경 준비 체크리스트 업무 함수 테스트 (가상 데이터)."""
import json
import sqlite3
from datetime import date
from uuid import uuid4

import pytest

from pmo.checklist import ITEMS
from pmo.db import connection
from pmo.service import ConflictError, Service, ValidationError

TODAY = date(2026, 10, 18)


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path / "checklist.db", clock=lambda: TODAY)


def token():
    return str(uuid4())


def setup_assignment(service, pid="P-1", start="2026-10-26", end="2026-12-31", name="가상 인력"):
    service.save_person(dict(id=pid, name=name, company="가상 업체", category="협력"),
                        token=token(), actor="테스트")
    return service.save_assignment(
        dict(person_id=pid, project="가상 과제", start_date=start, end_date=end, grade="L6",
             assignment_type="Part time", mm="1"), token=token(), actor="테스트", overlap_reviewed=True)


def update(service, aid, item, status, reason="", **kwargs):
    version = next(r for r in service.checklist(aid) if r["item"] == item)["version"]
    return service.update_checklist_item(aid, item, status, reason, token=token(), actor="담당자",
                                         expected_version=kwargs.pop("expected_version", version),
                                         **kwargs)


def test_four_items_in_order_and_created_once(service):
    aid = setup_assignment(service)
    rows = service.checklist(aid)
    assert [r["item"] for r in rows] == list(ITEMS)
    assert {r["status"] for r in rows} == {"미진행"} and all(r["exists"] for r in rows)
    with connection(service.path) as con:
        assert con.execute("SELECT COUNT(*) FROM checklist").fetchone()[0] == 4


def test_status_change_with_audit_and_persistence(service):
    aid = setup_assignment(service)
    update(service, aid, "좌석 배정", "진행 중")
    row = Service(service.path, clock=lambda: TODAY).checklist(aid)[0]
    assert row["status"] == "진행 중" and row["version"] == 2 and row["updated_by"] == "담당자"
    audit = next(h for h in service.history() if h["entity"] == "checklist")
    assert json.loads(audit["before_json"])["status"] == "미진행"
    assert json.loads(audit["after_json"])["status"] == "진행 중"


def test_not_applicable_requires_reason(service):
    aid = setup_assignment(service)
    with pytest.raises(ValidationError, match="사유"):
        update(service, aid, "방문예약", "해당 없음", "  ")
    update(service, aid, "방문예약", "해당 없음", "기존 출입증 보유")
    row = service.checklist(aid)[3]
    assert row["status"] == "해당 없음" and row["reason"] == "기존 출입증 보유"


def test_done_requires_actual_application(service):
    aid = setup_assignment(service)
    with pytest.raises(ValidationError, match="실제 신청"):
        update(service, aid, "ID카드 신청", "완료")
    assert service.checklist(aid)[1]["status"] == "미진행"
    update(service, aid, "ID카드 신청", "완료", actual_done=True)
    row = service.checklist(aid)[1]
    assert row["status"] == "완료" and row["applied_at"]
    update(service, aid, "ID카드 신청", "진행 중")          # 되돌리면 신청 완료 시각도 지운다
    assert service.checklist(aid)[1]["applied_at"] is None


def test_draft_does_not_mark_done(service):
    aid = setup_assignment(service)
    draft = service.create_checklist_draft(aid, "OA기기 신청", actor="담당자")
    assert "가상 인력" in draft and "2026-10-26" in draft and "실제 신청은 사내 시스템" in draft
    row = service.checklist(aid)[2]
    assert row["status"] == "미진행" and row["draft"] == draft and row["drafted_at"]
    assert row["version"] == 1  # 초안은 상태 변경 미리보기를 깨지 않는다
    update(service, aid, "OA기기 신청", "진행 중")


def test_stale_version_same_token_and_invalid_input(service):
    aid = setup_assignment(service)
    t = token()
    first = service.update_checklist_item(aid, "좌석 배정", "진행 중", token=t, actor="담당자",
                                          expected_version=1)
    assert service.update_checklist_item(aid, "좌석 배정", "진행 중", token=t, actor="담당자",
                                         expected_version=1) == first
    assert service.checklist(aid)[0]["version"] == 2
    with pytest.raises(ConflictError):
        update(service, aid, "좌석 배정", "미진행", expected_version=1)
    for item, status in (("주차 등록", "진행 중"), ("좌석 배정", "보류")):
        with pytest.raises(ValidationError):
            service.update_checklist_item(aid, item, status, token=token(), actor="담당자",
                                          expected_version=2)
    with pytest.raises(ValidationError, match="투입 건"):
        service.update_checklist_item("없는-투입", "좌석 배정", "진행 중", token=token(),
                                      actor="담당자", expected_version=0)


def test_overview_lists_open_items_by_start_date(service):
    late = setup_assignment(service, "P-1", start="2026-11-10")
    soon = setup_assignment(service, "P-2", start="2026-10-20", name="빠른 인력")
    setup_assignment(service, "P-3", start="2026-01-01", end="2026-06-30", name="끝난 인력")
    for item in ITEMS:
        update(service, late, item, "해당 없음", "재투입")
    rows = service.checklist_overview()
    assert [r["id"] for r in rows] == [soon]
    assert rows[0]["done"] == 0 and rows[0]["days_to_start"] == 2 and len(rows[0]["open_items"]) == 4
    assert {r["id"] for r in service.checklist_overview(include_closed=True)} == {late, soon}


def test_missing_items_from_old_data_are_shown_and_created_on_save(service):
    aid = setup_assignment(service)
    with connection(service.path) as con:
        con.execute("DELETE FROM checklist WHERE assignment_id=? AND item='방문예약'", (aid,))
    row = service.checklist(aid)[3]
    assert row["exists"] is False and row["status"] == "미진행" and row["version"] == 0
    update(service, aid, "방문예약", "진행 중")
    assert service.checklist(aid)[3]["exists"] is True


def test_v4_database_migrates_checklist_with_backup(tmp_path):
    path = tmp_path / "v4.db"
    Service(path)  # 최신 스키마로 만든 뒤 v4 모양으로 되돌린다
    with sqlite3.connect(path) as con:
        con.executescript("""
            DROP TABLE checklist;
            CREATE TABLE checklist (assignment_id TEXT NOT NULL REFERENCES assignments(id),
                item TEXT NOT NULL, status TEXT NOT NULL DEFAULT '미진행',
                reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(assignment_id, item));
            INSERT INTO people (id,name,company,category) VALUES ('OLD','기존','업체','협력');
            INSERT INTO assignments (id,person_id,start_date,end_date,grade)
                VALUES ('A-OLD','OLD','2026-10-01','2026-12-31','L6');
            INSERT INTO checklist VALUES ('A-OLD','좌석 배정','해당 없음','기존 좌석');
            PRAGMA user_version = 4;
        """)
    service = Service(path, clock=lambda: TODAY)
    row = service.checklist("A-OLD")[0]
    assert row["status"] == "해당 없음" and row["reason"] == "기존 좌석" and row["version"] == 1
    assert len(list(tmp_path.glob("v4.db.backup-v4-*.db"))) == 1
    update(service, "A-OLD", "좌석 배정", "미진행")
