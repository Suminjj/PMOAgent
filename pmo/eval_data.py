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


def rp(n: int) -> str:
    """매출귀속 프로젝트 코드 (30133686-D036~D052 범위에서 선택)."""
    return f"30133686-D{n:03d}"


# 매출귀속 프로젝트는 과제가 아니라 인력(투입)마다 정해진다. 한 과제에 매출귀속 프로젝트가 여러 개다(11/03 담당자).
# Soft Lab 고도화: D036(김정직·이협력·강협력) · D039(정협력·윤협력) · D042(서협력)
# 전표작성 자동화: D037(이정직·김협력·박협력) · D043(조협력·오협력·이협력 12월~) · D041(김컨설)
# APC Analysis:    D038(김협력·정협력·최협력) · D040(이정직·한협력) · D044(이컨설) · D049(백협력)
# 같은 과제에서 월마다 다른 프로젝트: 신협력(Soft Lab 고도화) 10월 D036 · 11월 D045 · 12월 D046
# 미정: 문협력 (매출귀속 프로젝트 미입력). 류협력 D047, 명협력 D048.

# (관리번호, 이름, 업체, 분류, 시험할 경우)
PEOPLE = [
    ("EV-01", "김정직", "SK AX", "정", "정직원 · 현재 투입 1건 · 구매계약 없음"),
    ("EV-02", "이정직", "SK AX", "정", "정직원 · 과거 투입 + 현재 투입"),
    ("EV-03", "김협력", "알파솔루션", "협력", "동명이인 A"),
    ("EV-04", "김협력", "감마테크", "협력", "동명이인 B"),
    ("EV-05", "이협력", "알파솔루션", "협력", "과거·현재·예정 투입 + 계약 종료 15일 전(종료 예정 아님, 경계)"),
    ("EV-06", "박협력", "알파솔루션", "협력", "계약 종료 12일 전 (종료 예정)"),
    ("EV-07", "정협력", "감마테크", "협력", "8월 과제 교체 (0.25MM + 0.75MM), 같은 계약번호 여러 명"),
    ("EV-08", "최협력", "감마테크", "협력", "Part time 0.5MM (11월), 같은 계약번호 여러 명"),
    ("EV-09", "강협력", "감마테크", "협력", "계약 종료일 = 기준일 (오늘 종료)"),
    ("EV-10", "조협력", "알파솔루션", "협력", "종료 5일 전이지만 연장 계약 등록됨 (종료 예정 아님)"),
    ("EV-11", "윤협력", "알파솔루션", "협력", "계약 만료 15일 경과, 알림은 확인함"),
    ("EV-12", "한협력", "감마테크", "협력", "매입단가 미입력 (검수 계산 불가), 9월 15일 투입 시작 (9월 0.5MM)"),
    ("EV-13", "오협력", "알파솔루션", "협력", "예정 투입 · 계약 준비(계약번호 없음) · 준비 항목 상태가 섞임"),
    ("EV-14", "서협력", "감마테크", "협력", "신규 투입 예정(11/23) · 준비 항목 모두 미진행"),
    ("EV-15", "김컨설", "베타컨설팅", "컨설팅", "컨설팅 · 등급 없음"),
    ("EV-16", "이컨설", "베타컨설팅", "컨설팅", "계약 종료 10일 전 (종료 예정)"),
    # --- 11/03 담당자 추가 질문용 ---
    ("EV-17", "신협력", "알파솔루션", "협력", "같은 과제에서 월마다 매출귀속 프로젝트가 다름 (10월 D036·11월 D045·12월 D046)"),
    ("EV-18", "문협력", "감마테크", "협력", "매출귀속 프로젝트 미정 · 계약 종료 14일 전(11/29, 알림 시작일) · 계약이 투입보다 먼저 끝남"),
    ("EV-19", "류협력", "알파솔루션", "협력", "교체 대상: 전표작성 자동화 → 12월부터 노협력으로 교체 요청"),
    ("EV-20", "노협력", "알파솔루션", "협력", "교체 투입 예정 인력: 등록만 됨 · 투입·계약·계정 ID 없음"),
    ("EV-21", "명협력", "감마테크", "협력", "과제 이동 대상: Soft Lab 고도화 → 12월부터 APC Analysis로 이동 요청"),
    ("EV-22", "백협력", "감마테크", "협력", "Full time 10/1~10/16 = 0.5MM(반 달 근무) · 만료 후에도 알림 미확인"),
]

# 계정 ID: 아직 근무를 시작하지 않은 서협력·오협력·노협력만 없음 (가상 ID)
NO_ACCOUNT = ("EV-13", "EV-14", "EV-20")
JOB_ROLES = {"정": "IT프로젝트관리", "협력": "응용SW개발자", "컨설팅": "컨설턴트"}


# 평가·시연용 계정 (v2 권한). 가상 데이터 전용 비밀번호이며 업무 DB에는 넣지 않는다.
EVAL_PASSWORD = "eval-pass-1115"
EVAL_USERS = [
    ("pmo", "가상 PMO", "PMO", ()),
    ("pm", "가상 PM", "PM", ()),
    ("leader-softlab", "Soft Lab 리더", "과제 리더", (SOFTLAB,)),
    ("leader-voucher", "전표작성 리더", "과제 리더", (VOUCHER,)),
]


def seed_eval_users(path) -> dict:
    """평가용 계정을 만든다(첫 계정은 PMO). 아이디 → 사용자 id."""
    from pmo.access import authenticate, create_user
    ids, admin = {}, None
    for login, name, role, projects in EVAL_USERS:
        ids[login] = create_user(path, login=login, name=name, role=role, password=EVAL_PASSWORD,
                                 projects=projects, actor=admin)
        if admin is None:
            admin = authenticate(path, login, EVAL_PASSWORD)
    return ids


def _token() -> str:
    return str(uuid4())


def seed_eval(service: Service, today: date = TODAY) -> dict:
    """빈 DB에 평가용 가상 데이터를 넣는다. 기존 데이터를 지우거나 덮어쓰지 않는다."""
    if today != TODAY:
        raise ValueError(f"평가 데이터는 기준일 {TODAY}에 맞춰 만들었습니다. (받은 기준일 {today})")
    if service.people():
        raise RuntimeError("이미 데이터가 있는 DB에는 평가 데이터를 넣지 않습니다. 빈 DB 경로를 지정하세요.")
    for pid, name, company, category, _case in PEOPLE:
        account = None if pid in NO_ACCOUNT else f"evuser{pid[3:]}"
        service.save_person(dict(id=pid, name=name, company=company, category=category,
                                 job_role=JOB_ROLES[category], account_id=account),
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
    a["EV-01"] = assign("EV-01", SOFTLAB, rp(36), "2026-03-01", "2027-02-28")
    assign("EV-02", VOUCHER, rp(37), "2025-07-01", "2026-06-30")
    a["EV-02"] = assign("EV-02", APC, rp(40), "2026-07-01", "2026-12-31")
    a["EV-03"] = assign("EV-03", VOUCHER, rp(37), "2026-09-01", "2027-02-28", rate="9000000", grade="L5")
    contract("EV-03", "알파솔루션", "C-2026-101", "2026-09-01", "2027-02-28", a["EV-03"])
    a["EV-04"] = assign("EV-04", APC, rp(38), "2026-10-01", "2026-12-31", rate="8000000")
    contract("EV-04", "감마테크", "C-2026-201", "2026-10-01", "2026-12-31", a["EV-04"])
    assign("EV-05", SOFTLAB, rp(36), "2025-11-03", "2026-04-30", rate="9500000")
    a["EV-05"] = assign("EV-05", SOFTLAB, rp(36), "2026-05-11", "2026-11-30", rate="9500000")   # 5월 0.737MM
    assign("EV-05", VOUCHER, rp(43), "2026-12-01", "2027-05-31", rate="9500000")
    contract("EV-05", "알파솔루션", "C-2026-102", "2026-05-11", "2026-11-30", a["EV-05"])
    a["EV-06"] = assign("EV-06", VOUCHER, rp(37), "2026-08-10", "2026-11-27", rate="7000000", grade="L5")
    contract("EV-06", "알파솔루션", "C-2026-103", "2026-08-10", "2026-11-27", a["EV-06"])
    assign("EV-07", SOFTLAB, rp(39), "2026-07-01", "2026-08-07", rate="8500000")                 # 8월 0.25MM
    a["EV-07"] = assign("EV-07", APC, rp(38), "2026-08-10", "2026-12-31", rate="8500000")   # 8월 0.75MM
    contract("EV-07", "감마테크", "C-2026-202", "2026-07-01", "2026-12-31")              # 연결 없음 → 두 투입
    a["EV-08"] = assign("EV-08", APC, rp(38), "2026-11-01", "2026-11-30", rate="6000000", grade="L5",
                        kind="Part time", mm="0.5")
    contract("EV-08", "감마테크", "C-2026-202", "2026-11-01", "2026-11-30", a["EV-08"])
    a["EV-09"] = assign("EV-09", SOFTLAB, rp(36), "2026-05-15", "2026-11-15", rate="8000000")
    contract("EV-09", "감마테크", "C-2026-203", "2026-05-15", "2026-11-15", a["EV-09"])
    a["EV-10"] = assign("EV-10", VOUCHER, rp(43), "2026-05-01", "2027-04-30", rate="7500000", grade="L5")
    first = contract("EV-10", "알파솔루션", "C-2026-104", "2026-05-01", "2026-11-20", a["EV-10"])
    contract("EV-10", "알파솔루션", "C-2026-105", "2026-11-21", "2027-04-30", a["EV-10"], previous=first)
    a["EV-11"] = assign("EV-11", SOFTLAB, rp(39), "2026-04-01", "2026-10-31", rate="7000000")
    expired = contract("EV-11", "알파솔루션", "C-2026-106", "2026-04-01", "2026-10-31", a["EV-11"])
    a["EV-12"] = assign("EV-12", APC, rp(40), "2026-09-15", "2027-03-31")                   # 매입단가 없음
    contract("EV-12", "감마테크", "C-2026-202", "2026-09-15", "2026-12-31", a["EV-12"])
    a["EV-13"] = assign("EV-13", VOUCHER, rp(43), "2026-12-01", "2027-05-31", rate="9000000")
    contract("EV-13", "알파솔루션", None, "2026-12-01", "2027-05-31", a["EV-13"], status="준비")
    a["EV-14"] = assign("EV-14", SOFTLAB, rp(42), "2026-11-23", "2027-05-31", rate="8000000")
    contract("EV-14", "감마테크", "C-2026-204", "2026-11-23", "2027-05-31", a["EV-14"])
    a["EV-15"] = assign("EV-15", VOUCHER, rp(41), "2026-10-01", "2027-03-31", rate="12000000", grade=None)
    contract("EV-15", "베타컨설팅", "C-2026-301", "2026-10-01", "2027-03-31", a["EV-15"])
    a["EV-16"] = assign("EV-16", APC, rp(44), "2026-09-01", "2026-11-25", rate="11000000", grade=None)
    contract("EV-16", "베타컨설팅", "C-2026-302", "2026-09-01", "2026-11-25", a["EV-16"])

    # --- 11/03 담당자 추가 질문용 ---
    # 신협력: 같은 과제(Soft Lab 고도화)에서 월마다 매출귀속 프로젝트가 다르다 → 투입을 달마다 나눠 둔다.
    # 계약은 투입과 연결하지 않아 세 투입 모두를 덮는다. "12월만 D047로" 요청 시 12월 투입만 바뀌어야 한다.
    shin = [assign("EV-17", SOFTLAB, rp(36), "2026-10-01", "2026-10-31", rate="8000000"),
            assign("EV-17", SOFTLAB, rp(45), "2026-11-01", "2026-11-30", rate="8000000"),
            assign("EV-17", SOFTLAB, rp(46), "2026-12-01", "2026-12-31", rate="8000000")]
    contract("EV-17", "알파솔루션", "C-2026-107", "2026-10-01", "2026-12-31")
    # 문협력: 매출귀속 프로젝트 미정. 계약은 11/29 종료(기준일 14일 전 = 알림 시작일)로 투입(1/31)보다 먼저 끝난다.
    a["EV-18"] = assign("EV-18", APC, None, "2026-11-02", "2027-01-31", rate="7500000")
    contract("EV-18", "감마테크", "C-2026-205", "2026-11-02", "2026-11-29", a["EV-18"])
    # 류협력 → 노협력 교체 요청용. 노협력은 등록만 되어 있다.
    a["EV-19"] = assign("EV-19", VOUCHER, rp(47), "2026-08-03", "2027-01-31", rate="8500000")
    contract("EV-19", "알파솔루션", "C-2026-108", "2026-08-03", "2027-01-31", a["EV-19"])
    # 명협력: Soft Lab 고도화에 2/28까지 투입 중 → 12/1부터 APC Analysis로 이동 요청용.
    a["EV-21"] = assign("EV-21", SOFTLAB, rp(48), "2026-09-01", "2027-02-28", rate="8000000")
    contract("EV-21", "감마테크", "C-2026-206", "2026-09-01", "2027-02-28", a["EV-21"])
    # 백협력: Full time 10/1~10/16 → 10월 근무일 20일 중 10일(10/5 개천절 대체휴일·10/9 한글날 제외) = 0.5MM (반 달 근무).
    # 최협력의 Part time 0.5MM(11월 한 달 내내 다른 업무와 병행)과 대비. 계약 만료 후 알림을 확인하지 않았다.
    a["EV-22"] = assign("EV-22", APC, rp(49), "2026-10-01", "2026-10-16", rate="7000000")
    contract("EV-22", "감마테크", "C-2026-207", "2026-10-01", "2026-10-16", a["EV-22"])

    # 만료된 윤협력 계약의 알림은 이미 확인한 상태(미확인 알림 목록에서 빠짐)
    for alert in service.pending_alerts(today):
        if alert["contract_id"] == expired:
            service.acknowledge_alert(alert["id"], actor=ACTOR)

    # 근무 환경 준비: 오협력·서협력을 빼면 모두 완료. 오협력은 상태가 섞이게.
    def set_item(aid, item, status, reason=""):
        row = next(r for r in service.checklist(aid) if r["item"] == item)
        service.update_checklist_item(aid, item, status, reason, actual_done=status == "완료",
                                      token=_token(), actor=ACTOR, expected_version=row["version"])

    for aid in [aid for pid, aid in a.items() if pid not in ("EV-13", "EV-14")] + shin:
        for item in ITEMS:
            set_item(aid, item, "완료")
    set_item(a["EV-13"], "방문예약", "완료")
    set_item(a["EV-13"], "계정 신청", "진행 중")
    set_item(a["EV-13"], "ID카드 신청", "해당 없음", "기존 출입증 보유")
    seed_eval_users(service.path)
    return {"people": len(PEOPLE), "users": len(EVAL_USERS), "today": today.isoformat()}
