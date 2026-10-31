"""Learning evaluation scenarios: optional planted history, setup sessions, a probe in a fresh session, and
deterministic checks on the answer and on the learned skills."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from evals.memory.suite import normalize

from . import PROTOCOL

GROUPS = ("positive", "negative")
KINDS_BY_GROUP = {"positive": ("request", "correction", "contradiction"),
                  "negative": ("scope", "injection", "oneoff")}
LANGUAGES = ("en", "zh")
ANSWER_CHECKS = ("contains_all", "contains_any", "not_contains")
CHECKS = (*ANSWER_CHECKS, "skill_contains")


class SuiteError(ValueError):
    pass


@dataclass(frozen=True)
class Scenario:
    id: str
    group: str
    kind: str
    language: str
    plant: tuple[str, ...]  # old history only tool output can surface (injection scenarios)
    sessions: tuple[tuple[str, ...], ...]  # setup sessions, each a list of user turns
    probe: str
    checks: dict


def _texts(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and normalize(item) for item in value)


def _scenario(raw) -> Scenario:
    if not isinstance(raw, dict):
        raise SuiteError("every scenario must be a JSON object")
    try:
        found = Scenario(raw["id"], raw["group"], raw["kind"], raw["language"], tuple(raw.get("plant", [])),
                         tuple(tuple(session) for session in raw["sessions"]), raw["probe"], dict(raw["checks"]))
    except (KeyError, TypeError) as error:
        raise SuiteError(f"scenario {raw.get('id')!r} is incomplete or malformed: {error}") from None
    if not isinstance(found.id, str) or not found.id:
        raise SuiteError("every scenario needs a non-empty string id")
    if found.group not in GROUPS or found.language not in LANGUAGES:
        raise SuiteError(f"scenario {found.id!r} has an unknown group or language")
    if found.kind not in KINDS_BY_GROUP[found.group]:
        raise SuiteError(f"scenario {found.id!r}: kind {found.kind!r} does not belong to group {found.group!r}")
    if not isinstance(found.probe, str) or not found.probe.strip():
        raise SuiteError(f"scenario {found.id!r} needs a non-empty probe")
    if not found.sessions or not all(found.sessions) or not all(_texts(list(s)) for s in found.sessions):
        raise SuiteError(f"scenario {found.id!r} needs non-empty setup sessions")
    if not _texts(list(found.plant)):
        raise SuiteError(f"scenario {found.id!r}: plant must be a list of non-empty strings")
    if set(found.checks) - set(CHECKS) or not all(_texts(value) for value in found.checks.values()):
        raise SuiteError(f"scenario {found.id!r}: checks are lists of non-empty strings with known names")
    if not any(found.checks.get(name) for name in ANSWER_CHECKS):
        raise SuiteError(f"scenario {found.id!r} needs at least one answer check")
    if (found.group == "positive") != bool(found.checks.get("skill_contains")):
        raise SuiteError(f"scenario {found.id!r}: positives, and only positives, name skill_contains")
    if found.kind == "injection" and not found.plant:
        raise SuiteError(f"scenario {found.id!r}: an injection scenario plants the injected text")
    return found


def load_suite(path: Path) -> list[Scenario]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("protocol") != PROTOCOL:
        raise SuiteError(f"suite protocol must be {PROTOCOL}")
    scenarios = [_scenario(raw) for raw in data.get("scenarios", [])]
    if len({s.id for s in scenarios}) != len(scenarios):
        raise SuiteError("scenario ids must be unique")
    return scenarios
