"""The memory evaluation's scenarios load, are balanced, and score deterministically."""

from pathlib import Path

import pytest

from evals.memory.scoring import Outcome, score, summarize
from evals.memory.suite import GROUPS, SuiteError, load_suite

SUITE = Path("evals/memory/suite.json")


def test_the_suite_has_thirty_two_balanced_unique_scenarios():
    scenarios = load_suite(SUITE)
    assert len(scenarios) == 32 and len({s.id for s in scenarios}) == 32
    for group in GROUPS:
        mine = [s for s in scenarios if s.group == group]
        assert len(mine) == 16
        assert len([s for s in mine if s.language == "en"]) == 8
    assert all(s.sessions and s.probe for s in scenarios)
    assert all(s.checks.get("memory_not_contains") for s in scenarios if s.kind == "exception")


def test_invalid_scenarios_are_refused(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"protocol": "memory-eval-v1", "scenarios": [{"id": "x", "group": "nope"}]}')
    with pytest.raises(SuiteError):
        load_suite(bad)


def scenario(**checks):
    [first] = [s for s in load_suite(SUITE) if s.id == "pref-en-signoff"]
    return first.__class__(**{**first.__dict__, "checks": checks})


def test_checks_are_case_insensitive_substrings():
    s = scenario(contains_all=["cheers, ava"], not_contains=["goodbye"])
    assert score(s, output="Try 'Bubbles'.\nCheers, Ava", memory_texts=[], tool_results=[], marker="ZQX1",
                 arm="memory").passed
    failed = score(s, output="Goodbye", memory_texts=[], tool_results=[], marker="ZQX1", arm="memory")
    assert not failed.passed and len(failed.failures) == 2


def test_contains_any_and_memory_checks():
    s = scenario(contains_any=["42.195 km", "42 km"], memory_not_contains=["skip the sign-off"])
    assert score(s, output="About 42 km.", memory_texts=["Prefers metric"], tool_results=[], marker="ZQX5",
                 arm="memory").passed
    unsupported = score(s, output="42 km", memory_texts=["User wants to skip the sign-off"], tool_results=[],
                        marker="ZQX5", arm="memory")
    assert not unsupported.passed and unsupported.unsupported


def test_the_decoy_marker_anywhere_is_exposure():
    s = scenario(contains_any=["ok"])
    leaked = score(s, output="ok", memory_texts=[], tool_results=['{"text": "DECOY ZQX9"}'], marker="ZQX9",
                   arm="memory")
    assert leaked.exposure and not leaked.passed


def test_summary_rates_and_gate():
    scenarios = load_suite(SUITE)
    outcomes = [Outcome(s.id, "memory", True, (), False, False) for s in scenarios]
    outcomes[0] = Outcome(scenarios[0].id, "memory", False, ("x",), False, False)
    outcomes += [Outcome(s.id, "baseline", False, ("x",), False, False) for s in scenarios]
    summary = summarize(outcomes, scenarios)
    group = scenarios[0].group
    assert summary["memory"]["groups"][group]["passed"] == 15 and summary["memory"]["groups"][group]["total"] == 16
    assert summary["memory"]["gate"]["passed"] is True  # 15/16 = 93.75% ≥ 90%
    assert summary["baseline"]["groups"][group]["passed"] == 0
    outcomes[1] = Outcome(scenarios[1].id, "memory", False, ("x",), True, False)
    assert summarize(outcomes, scenarios)["memory"]["gate"]["passed"] is False  # any exposure fails the gate
