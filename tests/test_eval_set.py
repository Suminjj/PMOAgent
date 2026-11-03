"""정확도 평가 세트: 평가 데이터(기준일 2026-11-15)와 정답이 맞물려 있는지 (가상 데이터)."""
import json
from datetime import date

import pytest

from pmo.eval_data import PEOPLE, TODAY, seed_eval
from pmo.harness import EVAL_SET, OraclePlanner, run
from pmo.service import Service
from scripts.build_eval_set import build


def test_eval_set_file_is_up_to_date():
    """evals/eval_set.json은 scripts/build_eval_set.py로 만든 결과와 같아야 한다(정답을 손으로 고치지 않음)."""
    assert json.loads(EVAL_SET.read_text(encoding="utf-8")) == json.loads(json.dumps(build(), ensure_ascii=False))


def test_oracle_passes_every_graded_question():
    report = run(OraclePlanner(), questions_path=EVAL_SET, save=False)
    assert report["passed"] == report["total"] and report["total"] >= 20
    assert report["ungraded"] >= 5                                         # 답변 채점(저지) 대기 질문


def test_each_person_has_one_case_and_key_cases_hold(tmp_path):
    service = Service(tmp_path / "eval.db", clock=lambda: TODAY)
    seed_eval(service, TODAY)
    assert len({p[0] for p in PEOPLE}) == len(PEOPLE) == len(service.people())
    contracts = {(c["name"], c["contract_no"]): c for c in service.contracts()}
    assert contracts[("이협력", "C-2026-102")]["days_left"] == 15 and not contracts[("이협력", "C-2026-102")]["ending_soon"]
    assert contracts[("조협력", "C-2026-104")]["has_successor"] and not contracts[("조협력", "C-2026-104")]["ending_soon"]
    assert contracts[("강협력", "C-2026-203")]["label"] == "오늘 종료"
    assert len(service.find_people("김협력")) == 2
    # 11/03 담당자 질문용 경우
    assert contracts[("문협력", "C-2026-205")]["days_left"] == 14 and contracts[("문협력", "C-2026-205")]["ending_soon"]
    assert [x["revenue_project"] for x in service.assignments("EV-18")] == [None]            # 매출귀속 프로젝트 미정
    shin = sorted(service.assignments("EV-17"), key=lambda x: x["start_date"])
    assert {x["project"] for x in shin} == {"Soft Lab 고도화"} and len({x["revenue_project"] for x in shin}) == 3
    baek, choi = service.assignments("EV-22")[0], service.assignments("EV-08")[0]
    assert (baek["assignment_type"], baek["mm"]) == ("Full time", "0.500")                  # 10/1~10/16 반 달
    assert (choi["assignment_type"], choi["mm"]) == ("Part time", "0.5")                    # 11월 한 달 병행
    assert service.assignments("EV-20") == [] and "백협력" in {x["name"] for x in service.pending_alerts()}
    assert sorted(x["name"] for x in service.people() if not x["account_id"]) == ["노협력", "서협력", "오협력"]


def test_seed_eval_guards(tmp_path):
    service = Service(tmp_path / "eval.db", clock=lambda: TODAY)
    with pytest.raises(ValueError):
        seed_eval(service, date(2026, 11, 1))
    seed_eval(service, TODAY)
    with pytest.raises(RuntimeError):
        seed_eval(service, TODAY)


def test_optional_steps_count_what_was_called():
    """교체처럼 find_people이 두 번 올 수 있는 질문: 한 번만 불러도, 두 번 불러도, 안 불러도 정답 순서로 본다."""
    from pmo.harness import _expected_steps
    steps = [{"tool": "find_people", "optional": True}, {"tool": "find_people", "optional": True},
             {"tool": "prepare_assignment_replace"}]
    for called in (["prepare_assignment_replace"], ["find_people", "prepare_assignment_replace"],
                   ["find_people", "find_people", "prepare_assignment_replace"]):
        assert [s["tool"] for s in _expected_steps(steps, called)] == called


def test_run_only_selected_questions():
    """--only: 권한 질문만(ID 또는 목표 버전) 골라 빠르게 실행한다."""
    by_id = run(OraclePlanner(), questions_path=EVAL_SET, save=False, only=["O04", "P01", "P02"])
    by_target = run(OraclePlanner(), questions_path=EVAL_SET, save=False, only=["v2"])
    assert [r["id"] for r in by_id["results"]] == [r["id"] for r in by_target["results"]] == ["O04", "P01", "P02"]
    with pytest.raises(ValueError):
        run(OraclePlanner(), questions_path=EVAL_SET, save=False, only=["X99"])
