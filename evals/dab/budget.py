"""Cumulative token budget for executions.

An execution must be granted an explicit budget. Before every run the harness reserves an input estimate plus the
maximum output; a run that would exceed the grant does not start. Settlement charges the reported usage, or the
reservation when the provider reported nothing (an unknown count is not zero tokens).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import PROTOCOL

class BudgetError(RuntimeError):
    """The budget is missing or malformed."""


class BudgetExhausted(BudgetError):
    """The next reservation would exceed the granted budget."""


@dataclass
class Ledger:
    """A persisted grant and its settlements."""

    path: Path
    max_tokens: int
    spent_tokens: int = 0
    entries: list[dict] = field(default_factory=list)

    @classmethod
    def grant(cls, path: Path, max_tokens: int) -> "Ledger":
        """Start a fresh grant. A non-positive grant is refused rather than treated as unlimited."""
        if max_tokens <= 0:
            raise BudgetError("a positive cumulative token budget is required")
        ledger = cls(path=path, max_tokens=max_tokens)
        ledger._save()
        return ledger

    @classmethod
    def open(cls, path: Path) -> "Ledger":
        if not path.is_file():
            raise BudgetError("no budget grant; run `execute --max-tokens N` first")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("protocol") != PROTOCOL:
            raise BudgetError("budget.json belongs to another protocol")
        return cls(path=path, max_tokens=int(data["max_tokens"]), spent_tokens=int(data["spent_tokens"]),
                   entries=list(data.get("entries", [])))

    @property
    def remaining(self) -> int:
        return self.max_tokens - self.spent_tokens

    def reserve(self, case_id: str, tokens: int) -> int:
        """Reserve tokens before one request; raise ``BudgetExhausted`` instead of starting it."""
        reserved = max(1, int(tokens))
        if reserved > self.remaining:
            self.entries.append({"case": case_id, "at": _now(), "reserved": reserved, "actual": None,
                                 "outcome": "budget_exhausted"})
            self._save()
            raise BudgetExhausted(f"budget: {case_id} needs about {reserved} tokens, {self.remaining} remain")
        return reserved

    def settle(self, case_id: str, reserved: int, actual: int | None) -> None:
        """Charge the reported usage; an unknown count charges the reservation and stays ``None`` in the entry."""
        self.spent_tokens += reserved if actual is None else actual
        self.entries.append({"case": case_id, "at": _now(), "reserved": reserved, "actual": actual,
                             "spent": self.spent_tokens})
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"protocol": PROTOCOL, "max_tokens": self.max_tokens, "spent_tokens": self.spent_tokens,
                   "entries": self.entries}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
