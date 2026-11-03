"""정확도 평가용 가상 데이터 (기준일 2026-11-15). 실제 인력·계약 정보를 넣지 않는다.

설계 (2026-11-01 담당자와 정함):
- 기준일을 2026-11-15(일)로 고정하고 날짜는 모두 절대 날짜로 둔다. 다른 기준일에서는 경우가 깨진다.
- 인력 한 명 = 시험할 경우 하나. 이름에 분류를 넣는다(김정직·김협력·김컨설): 손 테스트·캡처가 쉽다.
  분류를 묻는 질문은 평가 세트에 넣지 않는다(이름만 보고 맞힐 수 있으므로).
- 정답은 이 데이터를 넣은 DB에서 업무 함수로 계산한다(scripts/build_eval_set.py → evals/eval_set.json).
"""
from datetime import date
from uuid import uuid4

from pmo.checklist import ITEMS
from pmo.service import Service

TODAY = date(2026, 11, 15)
ACTOR = "평가 데이터"

SOFTLAB, VOUCHER, APC = "Soft Lab 고도화", "전표작성 자동화", "APC Analysis"
# 매출귀속 프로젝트 (D036~D052 범위에서 선택). 한 투입 과제의 인력이 여러 매출귀속 프로젝트로 나뉠 수 있다(11/03 담당자):
# 과제마다 기본 코드 + 일부 인력만 다른 코드(서협력 D039 · 김컨설 D041 · 최협력 D040).
RP_A, RP_B, RP_C = "30133686-D036", "30133686-D037", "30133686-D038"
RP_A2, RP_B2, RP_C2 = "30133686-D039", "30133686-D041", "30133686-D040"

# (관리번호, 이름, 업체, 분류, 시험할 경우)
PEOPLE = [
    ("EV-01", "김정직", "SK AX", "정", "정직원 · 현재 투입 1건 · 구매계약 없음"),
    ("EV-02", "이정직", "SK AX", "정", "정직원 · 과거 투입 + 현재 투입"),
    ("EV-03", "김협력", "알파솔루션", "협력", "동명이인 A"),
    ("EV-04", "김협력", "감마테크", "협력", "동명이인 B"),
    ("EV-05", "이협력", "알파솔루션", "협력", "과거·현재·예정 투입 + 계약 종료 15일 전(종료 예정 아님, 경계)"),
    ("EV-06", "박협력", "알파솔루션", "협력", "계약 종료 12일 전 (종료 예정)"),
    ("EV-07", "정협력", "감마테크", "협력", "8월 과제 교체 (0.25MM + 0.75MM), 같은 계약번호 여러 명"),
    ("EV-08", "최협력", "감마테크", "협력", "Part time 0.5MM (11월), 같은 계약번호 여러 명, 같은 과제 안 다른 매출귀속 프로젝트(D040)"),
    ("EV-09", "강협력", "감마테크", "협력", "계약 종료일 = 기준일 (오늘 종료)"),
    ("EV-10", "조협력", "알파솔루션", "협력", "종료 5일 전이지만 연장 계약 등록됨 (종료 예정 아님)"),
    ("EV-11", "윤협력", "알파솔루션", "협력", "계약 만료 15일 경과, 알림은 확인함"),
    ("EV-12", "한협력", "감마테크", "협력", "매입단가 미입력 (검수 계산 불가), 9월 15일 투입 시작 (9월 0.5MM)"),
    ("EV-13", "오협력", "알파솔루션", "협력", "예정 투입 · 계약 준비(계약번호 없음) · 준비 항목 상태가 섞임"),
    ("EV-14", "서협력", "감마테크", "협력", "신규 투입 예정(11/23) · 준비 항목 모두 미진행 · 같은 과제 안 다른 매출귀속 프로젝트(D039)"),
    ("EV-15", "김컨설", "베타컨설팅", "컨설팅", "컨설팅 · 등급 없음 · 같은 과제 안 다른 매출귀속 프로젝트(D041)"),
    ("EV-16", "이컨설", "베타컨설팅", "컨설팅", "계약 종료 10일 전 (종료 예정)"),
]


def _token() -> str:
    return str(uuid4())


def seed_eval(service: Service, today: date = TODAY) -> dict:
    """빈 DB에 평가용 가상 데이터를 넣는다. 기존 데이터를 지우거나 덮어쓰지 않는다."""
    if today != TODAY:
        raise ValueError(f"평가 데이터는 기준일 {TODAY}에 맞춰 만들었습니다. (받은 기준일 {today})")
    if service.people():
        raise RuntimeError("이미 데이터가 있는 DB에는 평가 데이터를 넣지 않습니다. 빈 DB 경로를 지정하세요.")
    for pid, name, company, category, _case in PEOPLE:
        service.save_person(dict(id=pid, name=name, company=company, category=category, job_role=None),
                            token=_token(), actor=ACTOR)

    def assign(pid, project, revenue, start, end, rate=None, grade="L6", kind="Full time", mm=None):
        return service.save_assignment(
            dict(person_id=pid, project=project, revenue_project=revenue, start_date=start, end_date=end,
                 grade=grade, assignment_type=kind, mm=mm, purchase_rate=rate,
                 rate_unit="원/MM" if rate else None),
            token=_token(), actor=ACTOR, overlap_reviewed=True)

    def contract(pid, company, number, start, end, assignment=None, previous=None, status="확정"):
        return service.save_contract(
            dict(person_id=pid, company=company, contract_no=number, start_date=start, end_date=end,
                 status=status, assignment_id=assignment, previous_contract_id=previous),
            token=_token(), actor=ACTOR)

    a = {}
    a["EV-01"] = assign("EV-01", SOFTLAB, RP_A, "2026-03-01", "2027-02-28")
    assign("EV-02", VOUCHER, RP_B, "2025-07-01", "2026-06-30")
    a["EV-02"] = assign("EV-02", APC, RP_C, "2026-07-01", "2026-12-31")
    a["EV-03"] = assign("EV-03", VOUCHER, RP_B, "2026-09-01", "2027-02-28", rate="9000000", grade="L5")
    contract("EV-03", "알파솔루션", "C-2026-101", "2026-09-01", "2027-02-28", a["EV-03"])
    a["EV-04"] = assign("EV-04", APC, RP_C, "2026-10-01", "2026-12-31", rate="8000000")
    contract("EV-04", "감마테크", "C-2026-201", "2026-10-01", "2026-12-31", a["EV-04"])
    assign("EV-05", SOFTLAB, RP_A, "2025-11-03", "2026-04-30", rate="9500000")
    a["EV-05"] = assign("EV-05", SOFTLAB, RP_A, "2026-05-11", "2026-11-30", rate="9500000")   # 5월 0.737MM
    assign("EV-05", VOUCHER, RP_B, "2026-12-01", "2027-05-31", rate="9500000")
    contract("EV-05", "알파솔루션", "C-2026-102", "2026-05-11", "2026-11-30", a["EV-05"])
    a["EV-06"] = assign("EV-06", VOUCHER, RP_B, "2026-08-10", "2026-11-27", rate="7000000", grade="L5")
    contract("EV-06", "알파솔루션", "C-2026-103", "2026-08-10", "2026-11-27", a["EV-06"])
    assign("EV-07", SOFTLAB, RP_A, "2026-07-01", "2026-08-07", rate="8500000")                 # 8월 0.25MM
    a["EV-07"] = assign("EV-07", APC, RP_C, "2026-08-10", "2026-12-31", rate="8500000")   # 8월 0.75MM
    contract("EV-07", "감마테크", "C-2026-202", "2026-07-01", "2026-12-31")              # 연결 없음 → 두 투입
    a["EV-08"] = assign("EV-08", APC, RP_C2, "2026-11-01", "2026-11-30", rate="6000000", grade="L5",
                        kind="Part time", mm="0.5")
    contract("EV-08", "감마테크", "C-2026-202", "2026-11-01", "2026-11-30", a["EV-08"])
    a["EV-09"] = assign("EV-09", SOFTLAB, RP_A, "2026-05-15", "2026-11-15", rate="8000000")
    contract("EV-09", "감마테크", "C-2026-203", "2026-05-15", "2026-11-15", a["EV-09"])
    a["EV-10"] = assign("EV-10", VOUCHER, RP_B, "2026-05-01", "2027-04-30", rate="7500000", grade="L5")
    first = contract("EV-10", "알파솔루션", "C-2026-104", "2026-05-01", "2026-11-20", a["EV-10"])
    contract("EV-10", "알파솔루션", "C-2026-105", "2026-11-21", "2027-04-30", a["EV-10"], previous=first)
    a["EV-11"] = assign("EV-11", SOFTLAB, RP_A, "2026-04-01", "2026-10-31", rate="7000000")
    expired = contract("EV-11", "알파솔루션", "C-2026-106", "2026-04-01", "2026-10-31", a["EV-11"])
    a["EV-12"] = assign("EV-12", APC, RP_C, "2026-09-15", "2027-03-31")                   # 매입단가 없음
    contract("EV-12", "감마테크", "C-2026-202", "2026-09-15", "2026-12-31", a["EV-12"])
    a["EV-13"] = assign("EV-13", VOUCHER, RP_B, "2026-12-01", "2027-05-31", rate="9000000")
    contract("EV-13", "알파솔루션", None, "2026-12-01", "2027-05-31", a["EV-13"], status="준비")
    a["EV-14"] = assign("EV-14", SOFTLAB, RP_A2, "2026-11-23", "2027-05-31", rate="8000000")
    contract("EV-14", "감마테크", "C-2026-204", "2026-11-23", "2027-05-31", a["EV-14"])
    a["EV-15"] = assign("EV-15", VOUCHER, RP_B2, "2026-10-01", "2027-03-31", rate="12000000", grade=None)
    contract("EV-15", "베타컨설팅", "C-2026-301", "2026-10-01", "2027-03-31", a["EV-15"])
    a["EV-16"] = assign("EV-16", APC, RP_C, "2026-09-01", "2026-11-25", rate="11000000", grade=None)
    contract("EV-16", "베타컨설팅", "C-2026-302", "2026-09-01", "2026-11-25", a["EV-16"])

    # 만료된 윤협력 계약의 알림은 이미 확인한 상태(미확인 알림 목록에서 빠짐)
    for alert in service.pending_alerts(today):
        if alert["contract_id"] == expired:
            service.acknowledge_alert(alert["id"], actor=ACTOR)

    # 근무 환경 준비: 오협력·서협력을 빼면 모두 완료. 오협력은 상태가 섞이게.
    def set_item(aid, item, status, reason=""):
        row = next(r for r in service.checklist(aid) if r["item"] == item)
        service.update_checklist_item(aid, item, status, reason, actual_done=status == "완료",
                                      token=_token(), actor=ACTOR, expected_version=row["version"])

    for pid, aid in a.items():
        if pid in ("EV-13", "EV-14"):
            continue
        for item in ITEMS:
            set_item(aid, item, "완료")
    set_item(a["EV-13"], "방문예약", "완료")
    set_item(a["EV-13"], "계정 신청", "진행 중")
    set_item(a["EV-13"], "ID카드 신청", "해당 없음", "기존 출입증 보유")
    return {"people": len(PEOPLE), "today": today.isoformat()}
