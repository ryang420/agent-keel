"""The review's observe tool (procedural learning design §4): each call is checked against the chunk under review and
staged; the queue commits the staged observations with the memory proposal, or drops them with it."""

from __future__ import annotations

from dataclasses import dataclass
import json

from keel.observations import REVIEW_OUTCOMES, check_observation
from keel.tools.base import tool_json


@dataclass(frozen=True)
class StagedObservation:
    topic: str
    situation: str
    approach: str
    outcome: str
    evidence: tuple[int, ...]


def _refusal(message: str, code: str = "invalid") -> tuple[str, bool]:
    return tool_json({"success": False, "code": code, "error": message}), True


class StagedObservations:
    def __init__(self, *, reviewed: set[int], context: set[int], finished: dict[int, bool], requested: set[int],
                 remaining: int) -> None:
        """`reviewed` are the chunk's message ids, `context` the context-only ones; `finished[id]` says the
        message's run completed without reaching the iteration cap; `requested` are messages a `requested`
        observation already cites; `remaining` is what the job's cap still allows."""
        self.reviewed, self.context, self.finished = reviewed, context, finished
        self.requested, self.remaining = requested, remaining
        self.items: list[StagedObservation] = []

    def call(self, arguments: str) -> tuple[str, bool]:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return _refusal("Arguments must be a JSON object.")
        if not isinstance(args, dict):
            return _refusal("Arguments must be a JSON object.")
        outcome = args.get("outcome")
        if outcome not in REVIEW_OUTCOMES:
            return _refusal(f"outcome must be one of {', '.join(REVIEW_OUTCOMES)}.")
        topic, situation, approach = args.get("topic"), args.get("situation"), args.get("approach")
        if reason := check_observation(topic, situation, approach):
            return _refusal(reason)
        if not approach.strip():
            return _refusal("approach must describe the method.")
        evidence = args.get("evidence")
        if (not isinstance(evidence, list) or not evidence
                or not all(isinstance(item, int) and not isinstance(item, bool) for item in evidence)):
            return _refusal("evidence must be a non-empty list of message ids.")
        cited = tuple(sorted(set(evidence)))
        if any(item not in self.reviewed and item not in self.context for item in cited):
            return _refusal("Cite only message ids shown in this conversation.")
        if not any(item in self.reviewed for item in cited):
            return _refusal("Cite at least one message outside the context-only part; context-only messages cannot "
                            "be the only evidence.")
        if outcome == "worked" and not all(self.finished.get(item, False) for item in cited):
            return _refusal("That run did not finish its task normally, so it cannot show that an approach worked.")
        if outcome == "requested" and any(item in self.requested for item in cited):
            return _refusal("That request is already recorded.")
        if len(self.items) >= self.remaining:
            return _refusal("No more observations can be recorded for this conversation.", "limit")
        self.items.append(StagedObservation(topic, situation, approach, outcome, cited))
        return tool_json({"success": True, "staged": len(self.items)}), False
