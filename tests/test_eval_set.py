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


def test_seed_eval_guards(tmp_path):
    service = Service(tmp_path / "eval.db", clock=lambda: TODAY)
    with pytest.raises(ValueError):
        seed_eval(service, date(2026, 11, 1))
    seed_eval(service, TODAY)
    with pytest.raises(RuntimeError):
        seed_eval(service, TODAY)
