"""Memory evaluation scenarios: setup sessions, a probe in a fresh session, and deterministic checks."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from . import PROTOCOL

GROUPS = ("preference", "recall")
KINDS = ("explicit", "exception", "correction", "decision", "distractor", "absence")
KINDS_BY_GROUP = {
    "preference": ("explicit", "exception", "correction"),
    "recall": ("decision", "distractor", "absence"),
}
LANGUAGES = ("en", "zh")
CHECKS = ("contains_all", "contains_any", "contains_any_2", "not_contains", "memory_not_contains")
_QUOTES = "'\"`‘’‚‛“”„‟「」『』«»＂＇"
_MARKUP = "*_~"


def normalize(text: str) -> str:
    """Case-folded, with every whitespace, quote and emphasis character removed, so spacing, quote style and bold
    markers never decide a check: "10 月 14 日" matches "10月14日", "couldn’t" matches "couldn't" and "**结论**："
    matches "结论："."""
    return "".join(ch for ch in text.casefold() if not ch.isspace() and ch not in _QUOTES and ch not in _MARKUP)


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


def _scenario(raw) -> Scenario:
    if not isinstance(raw, dict):
        raise SuiteError("every scenario must be a JSON object")
    try:
        ident, group, kind, language = raw["id"], raw["group"], raw["kind"], raw["language"]
        raw_sessions, probe, raw_checks = raw["sessions"], raw["probe"], raw["checks"]
    except KeyError as error:
        raise SuiteError(f"scenario {raw.get('id')!r} is incomplete: {error}") from None
    if not isinstance(raw_sessions, list) or not all(isinstance(session, list) for session in raw_sessions):
        raise SuiteError(f"scenario {ident!r}: every session must be a list of user turns")
    if not isinstance(raw_checks, dict):
        raise SuiteError(f"scenario {ident!r}: checks must be an object")
    found = Scenario(ident, group, kind, language, tuple(tuple(session) for session in raw_sessions), probe,
                     dict(raw_checks))
    if not isinstance(found.id, str) or not found.id:
        raise SuiteError("every scenario needs a non-empty string id")
    if found.group not in GROUPS or found.language not in LANGUAGES:
        raise SuiteError(f"scenario {found.id!r} has an unknown group or language")
    if found.kind not in KINDS_BY_GROUP[found.group]:
        raise SuiteError(f"scenario {found.id!r}: kind {found.kind!r} does not belong to group {found.group!r}")
    if not isinstance(found.probe, str) or not found.probe.strip():
        raise SuiteError(f"scenario {found.id!r} needs a non-empty probe")
    if not found.sessions or not all(found.sessions) or not all(isinstance(t, str) and t.strip()
                                                               for s in found.sessions for t in s):
        raise SuiteError(f"scenario {found.id!r} needs non-empty setup sessions")
    if set(found.checks) - set(CHECKS) or not any(found.checks.get(name) for name in CHECKS):
        raise SuiteError(f"scenario {found.id!r} has unknown or no checks")
    if not all(isinstance(value, list) and all(isinstance(item, str) and normalize(item) for item in value)
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
