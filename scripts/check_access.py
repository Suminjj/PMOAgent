"""v2 데이터 접근 권한 별도 검증표 (가상 평가 데이터, LLM 없음).

    python -m scripts.check_access

평가 데이터(기준일 2026-11-15)를 임시 DB에 넣고, 계정마다 같은 요청을 실행해 무엇이 보이고 막히는지 표로 보여준다.
Agent 도구 경로(call_tool)로 실행하므로 화면·Agent가 실제로 받는 결과와 같다.
"""
import tempfile
from pathlib import Path

from pmo.access import authenticate
from pmo.eval_data import EVAL_PASSWORD, EVAL_USERS, TODAY, seed_eval
from pmo.service import Service
from pmo.tools import call_tool

CHECKS = [
    ("전체 인력 수", lambda s: len(s.people())),
    ("김컨설(데이터 플랫폼) 투입 조회", lambda s: _ok(call_tool(s, "get_assignment_timeline", {"person_id": "김컨설"}))),
    ("서협력(차세대 ERP) 투입 조회", lambda s: _ok(call_tool(s, "get_assignment_timeline", {"person_id": "서협력"}))),
    ("볼 수 있는 매입단가 종류", lambda s: len({a["purchase_rate"] for a in s.assignments() if a["purchase_rate"]})),
    ("11월 종료 계약 인원", lambda s: len(call_tool(s, "contracts_ending", {"year": 2026, "month": 11})["data"])),
    ("상태 변경 확인 카드", lambda s: _ok(call_tool(s, "prepare_checklist_update",
                                               {"person_id": "서협력", "item": "ID카드 신청", "status": "진행 중"}))),
    ("변경 이력 조회", lambda s: _try(s.history)),
]


def _ok(result: dict) -> str:
    return "가능" if result["ok"] else "막힘: " + result["error"][:40]


def _try(fn) -> str:
    try:
        fn()
        return "가능"
    except Exception as exc:  # noqa: BLE001 - 표에 이유만 보여준다
        return "막힘: " + str(exc)[:40]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "access.db"
        seed_eval(Service(path, clock=lambda: TODAY), TODAY)
        print("질문에 '나는 데이터 플랫폼 리더야'라고 써도 결과는 같다: 권한은 로그인 계정으로만 정한다.\n")
        for login, name, role, projects in EVAL_USERS:
            access = authenticate(path, login, EVAL_PASSWORD)
            service = Service(path, clock=lambda: TODAY, access=access)
            print(f"■ {login} ({role}{' · ' + ', '.join(projects) if projects else ''})")
            for label, check in CHECKS:
                print(f"   {label:<24} {check(service)}")
            print()


if __name__ == "__main__":
    main()
