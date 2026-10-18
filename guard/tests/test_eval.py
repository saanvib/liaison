import copy
import json
from collections import Counter

import pytest

from contracts import GuardCategory, Verdict
from guard.core import GuardCore, Mode, detect_mode
from guard.eval.run_eval import BLOCKER_CATEGORIES, evaluate, load_rows, main, to_request
from guard.grounding import Grounding
from guard.judge import JudgeResult
from guard.rules import load_policy

ROWS = load_rows()
REQUIRED_FIELDS = {"id", "mode", "question", "draft_answer", "source", "expected_verdict", "expected_category"}


def test_labeled_set_shape():
    assert len(ROWS) >= 42
    assert len({r["id"] for r in ROWS}) == len(ROWS)
    for r in ROWS:
        assert REQUIRED_FIELDS <= set(r), r["id"]
        Verdict(r["expected_verdict"])
        GuardCategory(r["expected_category"])
        assert detect_mode(to_request(r)) == Mode(r["mode"]), r["id"]


def test_labeled_set_coverage():
    positives = Counter(r["expected_category"] for r in ROWS)
    for c in BLOCKER_CATEGORIES:
        assert positives[c.value] >= 5, c
    assert any(r["mode"] == "screen" and r["expected_category"] == "patient_data" for r in ROWS)
    clean = [r for r in ROWS if r["id"].startswith("clean-")]
    assert len(clean) >= 6 and all(r["expected_verdict"] == "approved" for r in clean)
    injections = [r for r in ROWS if r["id"].startswith("inj-")]
    assert len(injections) >= 4 and all(r["expected_verdict"] == "blocked" for r in injections)


def test_eval_passes_offline(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "recall" in out and "about 5 positives per category" in out


@pytest.mark.parametrize("category", [c.value for c in BLOCKER_CATEGORIES])
def test_weakening_any_rule_fails_the_eval(category, tmp_path, capsys):
    policy = copy.deepcopy(load_policy())
    policy["categories"][category]["patterns"] = policy["categories"][category]["patterns"][:1]
    path = tmp_path / "weak_policy.json"
    path.write_text(json.dumps(policy))
    assert main(["--policy", str(path)]) == 1
    assert "FAIL: " in capsys.readouterr().out


def test_low_precision_fails_the_eval():
    class FlagEverythingJudge:
        def assess(self, req):
            return JudgeResult(Verdict.NEEDS_REVIEW, GuardCategory.OFF_LABEL, "suspicious")

    # Clean draft/egress rows now get flagged, but that alone is only a few rows; add
    # many clean rows so precision clearly drops below the bar.
    clean = [r for r in ROWS if r["id"].startswith("clean-") and r["mode"] != "screen"]
    rows = ROWS + [dict(r, id=f"{r['id']}-x{i}") for i in range(10) for r in clean]
    report = evaluate(GuardCore(judge=FlagEverythingJudge(), grounding=Grounding()), rows)
    assert report.overall_precision < 0.8
    assert not report.passed


def test_real_mode_without_key_is_a_setup_error(monkeypatch, capsys):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert main(["--llm", "real"]) == 2
