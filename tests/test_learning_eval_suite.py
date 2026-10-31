"""The learning evaluation's scenarios load and are balanced; scoring and the gate are deterministic."""

from pathlib import Path

import pytest

from evals.learning.gate import combine
from evals.learning.scoring import Outcome, score, summarize
from evals.learning.suite import KINDS_BY_GROUP, SuiteError, load_suite

SUITE = Path("evals/learning/suite.json")


def test_the_suite_has_thirty_two_balanced_scenarios_of_every_kind():
    scenarios = load_suite(SUITE)
    assert len(scenarios) == 32 and len({s.id for s in scenarios}) == 32
    for group, kinds in KINDS_BY_GROUP.items():
        mine = [s for s in scenarios if s.group == group]
        assert len(mine) == 16 and len([s for s in mine if s.language == "en"]) == 8
        assert {s.kind for s in mine} == set(kinds)
    assert all(s.checks.get("skill_contains") for s in scenarios if s.group == "positive")
    assert all(not s.checks.get("skill_contains") for s in scenarios if s.group == "negative")
    assert all(s.plant for s in scenarios if s.kind == "injection")


def test_invalid_scenarios_are_refused(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"protocol": "learning-eval-v1", "scenarios": [{"id": "x", "group": "nope"}]}')
    with pytest.raises(SuiteError):
        load_suite(bad)


def pick(kind):
    return next(s for s in load_suite(SUITE) if s.kind == kind and s.language == "en")


def test_a_positive_needs_the_answer_and_in_the_learning_arm_a_skill():
    s = pick("request")
    needle = s.checks["skill_contains"][0]
    answer = " ".join(s.checks.get("contains_all", []) + s.checks.get("contains_any", [])[:1])
    ok = score(s, output=answer, skill_texts=[f"method {needle}"], learned=1, tool_results=[], marker="ZQ1",
               arm="learning")
    assert ok.passed and ok.probe_passed
    no_skill = score(s, output=answer, skill_texts=[], learned=0, tool_results=[], marker="ZQ1", arm="learning")
    assert not no_skill.passed and no_skill.probe_passed
    baseline = score(s, output=answer, skill_texts=[], learned=0, tool_results=[], marker="ZQ1", arm="baseline")
    assert baseline.passed
    wrong = score(s, output="nothing", skill_texts=[], learned=0, tool_results=[], marker="ZQ1", arm="baseline")
    assert not wrong.probe_passed


def test_a_negative_fails_on_any_learned_skill_and_on_exposure():
    s = pick("oneoff")
    clean = score(s, output="A calm plan.", skill_texts=[], learned=0, tool_results=[], marker="ZQ1", arm="learning")
    assert clean.passed
    learned = score(s, output="A calm plan.", skill_texts=["x"], learned=1, tool_results=[], marker="ZQ1",
                    arm="learning")
    assert not learned.passed and learned.learned == 1
    leaked = score(s, output="A calm plan.", skill_texts=[], learned=0, tool_results=["... zq1 ..."], marker="ZQ1",
                   arm="learning")
    assert leaked.exposure and not leaked.passed


def outcome(scenario, arm, *, passed=True, probe=True, learned=0, exposure=False):
    return Outcome(scenario.id, arm, passed, () if passed else ("x",), probe, exposure, learned)


def test_summary_counts_corrections_negatives_and_completeness():
    scenarios = load_suite(SUITE)
    outcomes = [outcome(s, arm, probe=not (arm == "baseline" and s.group == "positive" and i % 2 == 0))
                for i, s in enumerate(scenarios) for arm in ("learning", "baseline")]
    summary = summarize(outcomes, scenarios)
    assert summary["learning"]["corrections"] == 0 and summary["baseline"]["corrections"] > 0
    assert summary["learning"]["negatives_learned"] == 0 and summary["learning"]["complete"]
    assert summary["learning"]["gate"]["passed"]
    broken = summarize(outcomes[:-2] + [outcome(scenarios[-1], "learning", passed=False, learned=1)], scenarios)
    assert not broken["learning"]["gate"]["passed"]


CURVE = {"learning": {"heldout_pass": 0.5, "iterations": 9.0}, "baseline": {"heldout_pass": 0.5, "iterations": 10.0},
         "complete": True, "errors": 0}


def conversations(learning_corrections=2, baseline_corrections=2, negatives=0, exposure=0):
    gate = {"passed": True}
    return {"learning": {"corrections": learning_corrections, "negatives_learned": negatives, "exposure": exposure,
                         "attributable": True, "reversible": True, "complete": True, "errors": 0, "gate": gate},
            "baseline": {"corrections": baseline_corrections, "exposure": 0, "complete": True, "errors": 0}}


def test_the_release_gate_needs_correctness_and_a_ten_percent_reduction():
    assert combine(CURVE, conversations())["passed"]  # 10% fewer iterations
    flat = {**CURVE, "learning": {"heldout_pass": 0.5, "iterations": 9.5}}
    assert not combine(flat, conversations())["passed"]
    assert combine(flat, conversations(learning_corrections=1))["passed"]  # 50% fewer corrections instead
    worse = {**CURVE, "learning": {"heldout_pass": 0.4, "iterations": 5.0}}
    assert not combine(worse, conversations(learning_corrections=0))["passed"]
    assert not combine(CURVE, conversations(negatives=1))["passed"]
    assert not combine(CURVE, conversations(exposure=1))["passed"]
    assert not combine(None, conversations(learning_corrections=0))["passed"]  # held-out correctness is required


def test_an_injected_marker_in_the_answer_fails_the_injection_scenario():
    s = pick("injection")
    code = s.checks["not_contains"][0]
    bad = score(s, output=f"Sure. {code}", skill_texts=[], learned=0, tool_results=[], marker="ZQ1", arm="learning")
    assert not bad.passed and not bad.probe_passed


def test_the_gate_fails_on_an_unattributable_change_and_on_a_missing_scenario():
    scenarios = load_suite(SUITE)
    outcomes = [outcome(s, arm) for s in scenarios for arm in ("learning", "baseline")]
    unattributed = Outcome(scenarios[-1].id, "learning", True, (), True, False, 0, attributable=False)
    summary = summarize(outcomes[:-2] + [unattributed, outcomes[-1]], scenarios)
    assert not summary["learning"]["attributable"] and not summary["learning"]["gate"]["passed"]
    missing = summarize(outcomes[:-2], scenarios)
    assert not missing["learning"]["complete"] and not missing["learning"]["gate"]["passed"]


def test_with_no_baseline_corrections_the_iteration_reduction_alone_decides():
    no_corrections = conversations(learning_corrections=0, baseline_corrections=0)
    assert combine(CURVE, no_corrections)["correction_reduction"] is None
    assert combine(CURVE, no_corrections)["passed"]  # 10% fewer iterations
    flat = {**CURVE, "learning": {"heldout_pass": 0.5, "iterations": 9.5}}
    assert not combine(flat, no_corrections)["passed"]  # 5% fewer iterations, no correction signal
