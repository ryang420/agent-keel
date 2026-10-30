"""A staged copy of a profile's memory for one review attempt (cross-session memory design §4). The reviewer's memory
calls change only this copy, with the same validation as the live tool; the queue commits the proposal under the
profile lock if no touched target moved meanwhile."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from keel.memory import Change, Slot, apply_slots, joined
from keel.tools.base import tool_json
from keel.tools.memory import requested_ops

TARGETS = ("memory", "user")
CORRECTED = "That fact was corrected or removed after this part of the conversation. Do not save it again."


@dataclass
class StagedTarget:
    revision: int
    before: list[Slot]
    after: list[Slot]
    changes: list[Change] = field(default_factory=list)


def _refusal(message: str, code: str = "invalid") -> tuple[str, bool]:
    return tool_json({"success": False, "code": code, "error": message}), True


class StagedMemory:
    def __init__(self, slots: dict[str, list[Slot]], revisions: dict[str, int], limits: dict[str, int],
                 corrected: dict[str, set[str]]) -> None:
        self.limits = limits
        self.corrected = corrected
        self.targets = {target: StagedTarget(revisions.get(target, 0), list(slots.get(target, [])),
                                             list(slots.get(target, []))) for target in TARGETS}

    def call(self, arguments: str) -> tuple[str, bool]:
        """One memory tool call against the stage: (JSON result, error)."""
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return _refusal("Arguments must be a JSON object.")
        if not isinstance(args, dict):
            return _refusal("Arguments must be a JSON object.")
        if args.get("action") == "list":
            return tool_json(self.listing()), False
        target = args.get("target") or "memory"
        if target not in TARGETS:
            return _refusal("target must be memory or user.")
        ops = requested_ops(args)
        if not isinstance(ops, list):
            return _refusal("operations must be a list.")
        for op in ops:
            if isinstance(op, dict) and op.get("action") in ("add", "replace"):
                content = op.get("content") or op.get("new_text") or ""
                if isinstance(content, str) and content.strip() in self.corrected.get(target, set()):
                    return _refusal(CORRECTED, "corrected")
        staged = self.targets[target]
        after, result, changes = apply_slots(staged.after, ops, self.limits[target], lambda: uuid.uuid4().hex)
        if changes:
            staged.after = after
            staged.changes.extend(changes)
        return tool_json(result), not result.get("success")

    def items(self) -> dict[str, list[dict]]:
        return {target: [{"id": slot.id, "text": slot.text, "version": slot.version} for slot in staged.after]
                for target, staged in self.targets.items()}

    def listing(self) -> dict:
        listing = {}
        for target, mine in self.items().items():
            used = len(joined([item["text"] for item in mine]))
            listing[target] = {"usage": f"{used}/{self.limits[target]}", "items": mine}
        return listing

    def proposals(self) -> dict[str, tuple[int, list[Slot], list[Slot], list[Change]]]:
        return {target: (staged.revision, staged.before, staged.after, staged.changes)
                for target, staged in self.targets.items() if staged.changes}
