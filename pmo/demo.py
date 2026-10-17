"""시연·평가용 가상 데이터. 실제 인력·계약 정보를 넣지 않는다."""
from datetime import date, timedelta
from uuid import uuid4

from pmo.service import Service

ACTOR = "가상 데이터"


def _token() -> str:
    return str(uuid4())


def _iso(day: date) -> str:
    return day.isoformat()


def seed_demo(service: Service, today: date) -> dict:
    """오늘 날짜 기준으로 알림·종료 예정 사례가 보이도록 가상 데이터를 넣는다.

    비어 있는 DB에만 넣는다. 기존 데이터를 지우거나 덮어쓰지 않는다.
    """
    if service.people():
        raise RuntimeError("이미 데이터가 있는 DB에는 가상 데이터를 넣지 않습니다. 빈 DB 경로를 지정하세요.")
    month_start = today.replace(day=1)
    people = [
        ("DEMO-001", "김가상", "알파솔루션", "협력"),
        ("DEMO-002", "이가상", "베타컨설팅", "컨설팅"),
        ("DEMO-003", "박가상", "베타컨설팅", "협력"),
        ("DEMO-004", "최가상", "본사", "정"),
        ("DEMO-005", "김가상", "감마테크", "협력"),   # 동명이인
    ]
    for pid, name, company, category in people:
        service.save_person(dict(id=pid, name=name, company=company, category=category),
                            token=_token(), actor=ACTOR)

    def assign(pid, project, revenue, start, end, grade="L6"):
        return service.save_assignment(
            dict(person_id=pid, project=project, revenue_project=revenue,
                 start_date=_iso(start), end_date=_iso(end), grade=grade,
                 assignment_type="Part time", mm="1", sales_rate="10000000",
                 purchase_rate="8000000", rate_unit="원/MM"),
            token=_token(), actor=ACTOR, overlap_reviewed=True)

    past = assign("DEMO-001", "가상 과제 X", "매출 프로젝트 1",
                  today - timedelta(days=400), today - timedelta(days=200))
    now_a = assign("DEMO-001", "가상 과제 Y", "매출 프로젝트 2",
                   today - timedelta(days=60), today + timedelta(days=90))
    now_b = assign("DEMO-002", "가상 과제 Y", "매출 프로젝트 2",
                   today - timedelta(days=30), today + timedelta(days=5), grade="L7")
    now_c = assign("DEMO-003", "가상 과제 Z", "매출 프로젝트 3",
                   month_start, today + timedelta(days=45))
    assign("DEMO-004", "가상 과제 Z", "매출 프로젝트 3", today - timedelta(days=10), today + timedelta(days=120))
    assign("DEMO-005", "가상 과제 W", "매출 프로젝트 1", today + timedelta(days=10), today + timedelta(days=100))

    def contract(pid, company, number, start, end, status="확정", assignment=None, previous=None):
        return service.save_contract(
            dict(person_id=pid, company=company, contract_no=number, start_date=_iso(start),
                 end_date=_iso(end), status=status, assignment_id=assignment,
                 previous_contract_id=previous),
            token=_token(), actor=ACTOR)

    old = contract("DEMO-001", "알파솔루션", "DEMO-C-100", today - timedelta(days=400),
                   today - timedelta(days=200), assignment=past)
    # 오래전에 끝난 계약의 알림은 이미 확인한 상태로 둔다(확인 이력 시연용).
    for alert in service.pending_alerts(today):
        if alert["contract_id"] == old:
            service.acknowledge_alert(alert["id"], actor=ACTOR)
    first = contract("DEMO-001", "알파솔루션", "DEMO-C-101", today - timedelta(days=60),
                     today + timedelta(days=10), assignment=now_a)           # 알림 대상, 연장 등록됨
    contract("DEMO-001", "알파솔루션", "", today + timedelta(days=11),
             today + timedelta(days=90), status="준비", assignment=now_a, previous=first)
    contract("DEMO-002", "베타컨설팅", "DEMO-C-200", today - timedelta(days=30),
             today + timedelta(days=5), assignment=now_b)                     # 알림 대상
    contract("DEMO-003", "베타컨설팅", "DEMO-C-200", month_start,
             today + timedelta(days=45), assignment=now_c)                    # 같은 계약번호, 다른 인력
    contract("DEMO-005", "감마테크", "", today + timedelta(days=10),
             today + timedelta(days=100), status="준비")
    return {"people": len(people), "today": today.isoformat()}
