"""The memory evaluation's scenarios load, are balanced, and score deterministically."""

import json
from pathlib import Path

import pytest

from evals.memory.scoring import Outcome, normalize, score, summarize
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


GOOD = {"id": "x", "group": "preference", "kind": "explicit", "language": "en", "sessions": [["Remember this."]],
        "probe": "Hi?", "checks": {"contains_all": ["hi"]}}


def write_suite(tmp_path, scenarios):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps({"protocol": "memory-eval-v1", "scenarios": scenarios}, ensure_ascii=False),
                    encoding="utf-8")
    return path


@pytest.mark.parametrize("change", [
    {"kind": "decision"},  # a preference scenario cannot use a recall kind
    {"group": "recall", "kind": "explicit"},  # nor a recall scenario a preference kind
    {"probe": "   "},
    {"sessions": ["a string is not a session"]},
    {"sessions": [["   "]]},
    {"checks": {"contains_all": ["   "]}},
])
def test_loader_refuses_malformed_scenarios(tmp_path, change):
    with pytest.raises(SuiteError):
        load_suite(write_suite(tmp_path, [{**GOOD, **change}]))


def test_loader_refuses_a_scenario_that_is_not_an_object(tmp_path):
    with pytest.raises(SuiteError):
        load_suite(write_suite(tmp_path, ["not a scenario"]))


def test_the_loader_accepts_a_well_formed_scenario(tmp_path):
    [loaded] = load_suite(write_suite(tmp_path, [GOOD]))
    assert loaded.id == "x" and loaded.sessions == (("Remember this.",),)


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


def test_normalize_ignores_case_spacing_and_quote_style():
    assert normalize("10 月 14 日") == normalize("10月14日") == "10月14日"
    assert normalize("couldn’t") == normalize("couldn't") == "couldnt"
    assert normalize("“结论”") == normalize("结论") == "结论"
    assert normalize("「好」『的』") == "好的"
    assert normalize("ABC  def") == "abcdef"


def test_normalised_checks_match_across_spacing_and_quote_style():
    s = scenario(contains_all=["10月14日"], contains_any=["couldn't"])
    assert score(s, output="发布在 10 月 14 日。He couldn’t.", memory_texts=["“结论：”"], tool_results=[],
                 marker="ZQX5", arm="memory").passed


def test_contains_any_2_is_a_second_any_of_list():
    s = scenario(contains_any=["Postgres"], contains_any_2=["transaction"])
    assert score(s, output="Postgres, for transactions", memory_texts=[], tool_results=[], marker="ZQX5",
                 arm="memory").passed
    no_reason = score(s, output="Postgres", memory_texts=[], tool_results=[], marker="ZQX5", arm="memory")
    assert not no_reason.passed and no_reason.failures == ("none of ['transaction']",)
    no_database = score(s, output="for transactions", memory_texts=[], tool_results=[], marker="ZQX5",
                        arm="memory")
    assert not no_database.passed


@pytest.mark.parametrize("marker", ["", "   ", "“”"])
def test_an_empty_marker_is_refused(marker):
    with pytest.raises(ValueError, match="marker must be non-empty"):
        score(scenario(contains_any=["ok"]), output="ok", memory_texts=[], tool_results=[], marker=marker,
              arm="memory")


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


def test_the_gate_needs_every_scenario_exactly_once():
    scenarios = load_suite(SUITE)
    passing = [Outcome(s.id, "memory", True, (), False, False) for s in scenarios]
    assert summarize(passing, scenarios)["memory"]["gate"] == {
        "rate": 0.9, "exposure": 0, "unsupported": 0, "complete": True, "passed": True}
    missing = summarize(passing[1:], scenarios)["memory"]["gate"]
    assert missing["complete"] is False and missing["passed"] is False
    duplicated = summarize(passing + [passing[0]], scenarios)["memory"]["gate"]
    assert duplicated["complete"] is False and duplicated["passed"] is False
