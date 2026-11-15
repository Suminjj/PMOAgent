"""LLM-as-a-Judge(v4) 채점 로직. 판정 모델은 가짜 클라이언트로 대신한다."""
import json

import pytest

from pmo.judge import (agreement, build_messages, cohen_kappa, judge_report, key_check, parse_verdict,
                       read_review_csv, same_model, write_review_csv)

GOOD = {"scores": {"correctness": 5, "grounded": 5, "complete": 5, "safety": 5, "concise": 4},
        "verdict": "pass", "reason": "정답과 같다", "issues": []}


class FakeJudge:
    name = "openai:gpt-5-mini"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def chat(self, messages, tools):
        assert tools == []             # 판정 모델에는 도구를 주지 않는다
        self.seen.append(messages)
        return {"content": self.replies.pop(0), "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


def _row(**kw):
    row = {"id": "C07", "question": "11월에 계약 끝나는 사람 알려줘", "answer": "강협력, 문협력입니다.",
           "answer_key": {"must_include": ["강협력", "문협력"], "must_not_include": ["윤협력"], "note": "메모"},
           "call_details": [{"tool": "contracts_ending", "arguments": {"month": 11}, "ok": True, "rows": 2,
                             "evidence": "[{\"name\": \"강협력\"}]"}],
           "success": True}
    row.update(kw)
    return row


def test_parse_verdict_reads_json_in_code_block_and_korean_label():
    text = "```json\n" + json.dumps(dict(GOOD, verdict="부분 정답"), ensure_ascii=False) + "\n```"
    got = parse_verdict(text)
    assert got["verdict"] == "partial" and got["scores"]["concise"] == 4


@pytest.mark.parametrize("bad", ["판정할 수 없습니다", json.dumps(dict(GOOD, verdict="good")),
                                 json.dumps(dict(GOOD, scores={**GOOD["scores"], "safety": 7}))])
def test_parse_verdict_rejects_bad_format(bad):
    with pytest.raises(ValueError):
        parse_verdict(bad)


def test_prompt_has_reference_facts_tools_and_answer():
    body = build_messages(_row(), "2026-11-15", user="leader-softlab")[1]["content"]
    for text in ("들어가야 할 사실: 강협력, 문협력", "나오면 안 되는 말: 윤협력", "contracts_ending",
                 "결과 일부", "강협력, 문협력입니다.", "leader-softlab", "2026-11-15"):
        assert text in body


def test_judge_retries_once_on_bad_format_then_scores():
    judge = FakeJudge(["음...", json.dumps(GOOD)])
    out = judge_report({"results": [_row()], "planner": "openai-gpt-4.1-router", "today": "2026-11-15"}, judge)
    row = out["results"][0]
    assert row["verdict"] == "pass" and row["tokens"] == {"prompt": 200, "completion": 40}
    assert "형식 오류" in judge.seen[1][-1]["content"]
    assert out["summary"]["pass_rate"] == 1.0 and not out["self_judged"]


def test_summary_lists_where_judge_disagrees_with_key_check():
    # C07: 글자 확인은 통과했지만 '연장 필요 5명'을 지어낸 답 → 판정 모델이 오답으로 잡는 경우
    wrong = dict(GOOD, verdict="fail", scores={**GOOD["scores"], "grounded": 1}, reason="없는 5명을 덧붙임")
    judge = FakeJudge([json.dumps(wrong), json.dumps(GOOD)])
    rows = [_row(answer="강협력, 문협력. 연장 필요 5명."),
            _row(id="L04", answer="2026년 12월 31일", answer_key={"must_include": ["2026-12-31"]})]
    s = judge_report({"results": rows, "planner": "x", "today": "2026-11-15"}, judge)["summary"]
    assert s["counts"] == {"pass": 1, "partial": 0, "fail": 1}
    assert [(d["id"], d["key_check"], d["verdict"]) for d in s["key_check_disagree"]] == \
        [("C07", True, "fail"), ("L04", False, "pass")]


def test_missing_answer_is_not_sent_to_judge():
    judge = FakeJudge([])
    out = judge_report({"results": [_row(answer=None)], "planner": "x"}, judge)
    assert out["results"][0]["verdict"] is None and out["summary"]["errors"] == ["C07"]


def test_key_check():
    assert key_check(_row()) is True
    assert key_check(_row(answer="강협력, 문협력, 윤협력")) is False
    assert key_check(_row(answer_key={"note": "없음"})) is None


def test_same_model_warns_self_judging():
    assert same_model("openai:gpt-4.1", "openai-gpt-4.1-router")
    assert not same_model("openai:gpt-4.1", "openai-gpt-4.1-mini-router")
    assert not same_model("openai:gpt-5-mini", "openai-gpt-4.1-router")


def test_cohen_kappa_and_agreement():
    assert cohen_kappa(["pass", "fail"], ["pass", "fail"]) == 1.0
    assert cohen_kappa(["pass", "pass", "fail", "fail"], ["pass", "fail", "pass", "fail"]) == 0.0
    a = agreement({"A": "pass", "B": "fail", "C": "pass"}, {"A": "pass", "B": "pass"})
    assert (a["n"], a["agree"], a["differ"]) == (2, 1, ["B"])


def test_review_csv_round_trip(tmp_path):
    judge = FakeJudge([json.dumps(GOOD)])
    judged = judge_report({"results": [_row()], "planner": "x"}, judge)
    path = tmp_path / "review.csv"
    write_review_csv(judged, path)
    assert read_review_csv(path) == {}                 # 사람 판정 칸은 비어 있다
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    lines[1] = lines[1].rstrip(",") + ",부분 정답,"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    assert read_review_csv(path) == {"C07": "partial"}
