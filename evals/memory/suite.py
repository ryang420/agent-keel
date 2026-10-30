"""Memory evaluation scenarios: setup sessions, a probe in a fresh session, and deterministic checks."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from . import PROTOCOL

GROUPS = ("preference", "recall")
KINDS = ("explicit", "exception", "correction", "decision", "distractor", "absence")
LANGUAGES = ("en", "zh")
CHECKS = ("contains_all", "contains_any", "not_contains", "memory_not_contains")


class SuiteError(ValueError):
    pass


@dataclass(frozen=True)
class Scenario:
    id: str
    group: str
    kind: str
    language: str
    sessions: tuple[tuple[str, ...], ...]  # setup sessions, each a list of user turns
    probe: str  # the question asked in a fresh session afterwards
    checks: dict


def _scenario(raw: dict) -> Scenario:
    try:
        sessions = tuple(tuple(turn for turn in session) for session in raw["sessions"])
        found = Scenario(raw["id"], raw["group"], raw["kind"], raw["language"], sessions, raw["probe"],
                         dict(raw["checks"]))
    except (KeyError, TypeError) as error:
        raise SuiteError(f"scenario {raw.get('id')!r} is incomplete: {error}") from None
    if found.group not in GROUPS or found.kind not in KINDS or found.language not in LANGUAGES:
        raise SuiteError(f"scenario {found.id!r} has an unknown group, kind or language")
    if not found.sessions or not all(found.sessions) or not all(isinstance(t, str) and t.strip()
                                                               for s in found.sessions for t in s):
        raise SuiteError(f"scenario {found.id!r} needs non-empty setup sessions")
    if set(found.checks) - set(CHECKS) or not any(found.checks.get(name) for name in CHECKS):
        raise SuiteError(f"scenario {found.id!r} has unknown or no checks")
    if not all(isinstance(value, list) and all(isinstance(item, str) and item for item in value)
               for value in found.checks.values()):
        raise SuiteError(f"scenario {found.id!r}: every check is a list of non-empty strings")
    return found


def load_suite(path: Path) -> list[Scenario]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("protocol") != PROTOCOL:
        raise SuiteError(f"suite protocol must be {PROTOCOL}")
    scenarios = [_scenario(raw) for raw in data.get("scenarios", [])]
    if len({s.id for s in scenarios}) != len(scenarios):
        raise SuiteError("scenario ids must be unique")
    return scenarios
