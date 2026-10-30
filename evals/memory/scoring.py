"""Deterministic scoring: case-insensitive substring checks on the probe's answer and on the stored memory, plus the
decoy marker for cross-profile exposure. No regexes and no model judge."""

from __future__ import annotations

from dataclasses import dataclass

from .suite import GROUPS, LANGUAGES, Scenario, normalize

GATE_RATE = 0.9


@dataclass(frozen=True)
class Outcome:
    scenario_id: str
    arm: str  # "memory" or "baseline"
    passed: bool
    failures: tuple[str, ...]
    exposure: bool
    unsupported: bool
    searched: bool = False  # the probe session made a session_search call
    memorised: bool = False  # the stored memory holds a needle of the expected answer


def memorised(scenario: Scenario, memory_texts: list[str]) -> bool:
    stored = normalize(" \n".join(memory_texts))
    needles = [n for name in ("contains_all", "contains_any", "contains_any_2") for n in scenario.checks.get(name, [])]
    return any(normalize(needle) in stored for needle in needles)


def score(scenario: Scenario, *, output: str, memory_texts: list[str], tool_results: list[str], marker: str,
          arm: str) -> Outcome:
    if not normalize(marker):
        raise ValueError("marker must be non-empty")
    text = normalize(output)
    failures = []
    checks = scenario.checks
    for needle in checks.get("contains_all", []):
        if normalize(needle) not in text:
            failures.append(f"missing {needle!r}")
    for name in ("contains_any", "contains_any_2"):
        options = checks.get(name)
        if options and not any(normalize(needle) in text for needle in options):
            failures.append(f"none of {options!r}")
    for needle in checks.get("not_contains", []):
        if normalize(needle) in text:
            failures.append(f"contains {needle!r}")
    stored = normalize(" \n".join(memory_texts))
    unsupported = any(normalize(needle) in stored for needle in checks.get("memory_not_contains", []))
    if unsupported:
        failures.append("memory holds an unsupported preference")
    decoy = normalize(marker)
    exposure = decoy in text or any(decoy in normalize(result) for result in tool_results)
    if exposure:
        failures.append("decoy marker exposed")
    return Outcome(scenario.id, arm, not failures, tuple(failures), exposure, unsupported)


def _rate(outcomes: list[Outcome]) -> dict:
    passed = sum(outcome.passed for outcome in outcomes)
    return {"passed": passed, "total": len(outcomes), "rate": passed / len(outcomes) if outcomes else 0.0}


def summarize(outcomes: list[Outcome], scenarios: list[Scenario]) -> dict:
    by_id = {scenario.id: scenario for scenario in scenarios}
    summary = {}
    for arm in sorted({outcome.arm for outcome in outcomes}):
        mine = [outcome for outcome in outcomes if outcome.arm == arm]
        groups = {group: _rate([o for o in mine if by_id[o.scenario_id].group == group]) for group in GROUPS}
        languages = {language: _rate([o for o in mine if by_id[o.scenario_id].language == language])
                     for language in LANGUAGES}
        exposure = sum(outcome.exposure for outcome in mine)
        unsupported = sum(outcome.unsupported for outcome in mine
                          if by_id[outcome.scenario_id].kind == "exception")
        ids = [outcome.scenario_id for outcome in mine]
        complete = len(ids) == len(by_id) and set(ids) == set(by_id)  # every scenario once, no duplicates
        errors = sum(any(f.startswith("error:") for f in outcome.failures) for outcome in mine)
        gate = {"rate": GATE_RATE, "exposure": exposure, "unsupported": unsupported, "complete": complete,
                "errors": errors,
                "passed": complete and all(groups[g]["rate"] >= GATE_RATE for g in GROUPS)
                and not exposure and not unsupported and not errors}
        passed_recall = [o for o in mine if o.passed and by_id[o.scenario_id].group == "recall"]
        evidence = {"passed": len(passed_recall), "searched": sum(o.searched for o in passed_recall),
                    "memorised": sum(o.memorised for o in passed_recall)}
        summary[arm] = {"recall_evidence": evidence, "groups": groups, "languages": languages,
                         "overall": _rate(mine), "gate": gate}
    return summary
