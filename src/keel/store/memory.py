"""Curated memory rows (cross-session memory design §2): stable entries, a revision per target, a ledger of every
committed change and a replay record per mutation key. The rules for one batch live in keel.memory."""

from __future__ import annotations

import json
import uuid

from keel.memory import Change, Slot, apply_slots
from keel.store.base import MemoryItem, MemorySource

TARGETS = ("memory", "user")
_ITEMS = (
    "SELECT e.id, e.target, e.text, e.version, e.created_at, e.updated_at, "
    "c.source_kind, c.run_id, c.session_id, c.message_id FROM memory_entries e "
    "LEFT JOIN LATERAL (SELECT source_kind, run_id, session_id, message_id FROM memory_changes "
    "WHERE profile_id = e.profile_id AND entry_id = e.id ORDER BY id DESC LIMIT 1) c ON TRUE "
    "WHERE e.profile_id = ? AND e.state = 'active'")


def _item(row: dict) -> MemoryItem:
    return MemoryItem(row["id"], row["target"], row["text"], row["version"], row["created_at"], row["updated_at"],
                      row["source_kind"], row["run_id"], row["session_id"], row["message_id"])


class MemoryStoreMixin:
    async def apply_memory(self, profile_id: str, target: str, limit: int, ops: list[dict], *,
                           profile_incarnation: str | None = None,
                           source: MemorySource = MemorySource("foreground"),
                           mutation_key: str | None = None) -> dict:
        """Apply one batch to one target atomically under the profile lock. Each committed change goes to the
        ledger and bumps the target's revision. A mutation key that already committed returns its first result."""
        if target not in TARGETS:
            return {"success": False, "code": "invalid", "error": "target must be memory or user.",
                    "entries": [], "items": []}
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            await self.assert_profile_admitted(profile_id)
            await self._check_profile_incarnation(profile_id, profile_incarnation)
            if mutation_key is not None:
                done = await self.db.fetchrow(
                    "SELECT result FROM memory_mutations WHERE profile_id = ? AND mutation_key = ?",
                    profile_id, mutation_key)
                if done is not None:
                    return {**json.loads(done["result"]), "replayed": True}
            rows = await self.db.fetch(
                "SELECT id, text, version FROM memory_entries "
                "WHERE profile_id = ? AND target = ? AND state = 'active' ORDER BY position", profile_id, target)
            before = [Slot(row["id"], row["text"], row["version"]) for row in rows]
            after, result, changes = apply_slots(before, ops, limit, lambda: uuid.uuid4().hex)
            if changes:
                result["revision"] = await self._commit_memory(profile_id, target, before, after, changes, source,
                                                              mutation_key)
            else:
                result["revision"] = await self._memory_revision(profile_id, target)
            if result["success"] and mutation_key is not None:
                await self.db.execute(
                    "INSERT INTO memory_mutations (profile_id, mutation_key, session_id, result, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    profile_id, mutation_key, source.session_id, json.dumps(result, ensure_ascii=False), self.clock())
            return result

    async def _memory_revision(self, profile_id: str, target: str) -> int:
        row = await self.db.fetchrow(
            "SELECT revision FROM memory_revisions WHERE profile_id = ? AND target = ?", profile_id, target)
        return row["revision"] if row else 0

    async def _commit_memory(self, profile_id: str, target: str, before: list[Slot], after: list[Slot],
                             changes: list[Change], source: MemorySource,
                             mutation_key: str | None = None) -> int:
        now = self.clock()
        revision = await self._memory_revision(profile_id, target) + 1
        await self.db.execute(
            "INSERT INTO memory_revisions (profile_id, target, revision) VALUES (?, ?, ?) "
            "ON CONFLICT (profile_id, target) DO UPDATE SET revision = excluded.revision",
            profile_id, target, revision)
        old = {slot.id: slot for slot in before}
        new = {slot.id: slot for slot in after}
        last = await self.db.fetchrow(
            "SELECT COALESCE(MAX(position), -1) AS last FROM memory_entries WHERE profile_id = ? AND target = ?",
            profile_id, target)
        position = last["last"]
        for slot in after:
            if slot.id not in old:
                position += 1
                await self.db.execute(
                    "INSERT INTO memory_entries (id, profile_id, target, position, text, version, state, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                    slot.id, profile_id, target, position, slot.text, slot.version, now, now)
            elif slot != old[slot.id]:
                await self.db.execute(
                    "UPDATE memory_entries SET text = ?, version = ?, updated_at = ? WHERE profile_id = ? AND id = ?",
                    slot.text, slot.version, now, profile_id, slot.id)
        for slot in before:
            if slot.id not in new:
                await self.db.execute(
                    "UPDATE memory_entries SET state = 'removed', version = version + 1, updated_at = ? "
                    "WHERE profile_id = ? AND id = ?", now, profile_id, slot.id)
        for change in changes:
            if change.entry_id not in old and change.entry_id not in new:
                continue  # added and removed within the batch: no row was ever stored
            await self.db.execute(
                "INSERT INTO memory_changes (profile_id, target, entry_id, action, old_text, new_text, source_kind, "
                "run_id, session_id, message_id, tool_call_id, mutation_key, revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                profile_id, target, change.entry_id, change.action, change.old_text, change.new_text, source.kind,
                source.run_id, source.session_id, source.message_id, source.tool_call_id, mutation_key, revision, now)
        return revision

    async def memory_entries(self, profile_id: str) -> dict[str, list[str]]:
        rows = await self.db.fetch(
            "SELECT target, text FROM memory_entries WHERE profile_id = ? AND state = 'active' "
            "ORDER BY target, position", profile_id)
        found: dict[str, list[str]] = {"memory": [], "user": []}
        for row in rows:
            found[row["target"]].append(row["text"])
        return found

    async def memory_items(self, profile_id: str) -> list[MemoryItem]:
        rows = await self.db.fetch(_ITEMS + " ORDER BY e.target, e.position", profile_id)
        return [_item(row) for row in rows]

    async def memory_item(self, profile_id: str, entry_id: str) -> MemoryItem | None:
        row = await self.db.fetchrow(_ITEMS + " AND e.id = ?", profile_id, entry_id)
        return _item(row) if row else None

    async def run_user_message_id(self, session_id: str, run_id: str) -> int | None:
        """The run's latest stored user message: the evidence a foreground write is tied to."""
        row = await self.db.fetchrow(
            "SELECT MAX(id) AS id FROM messages WHERE session_id = ? AND run_id = ? AND role = 'user'",
            session_id, run_id)
        return row["id"] if row else None
    async def review_memory(self, profile_id: str) -> tuple[dict[str, list[Slot]], dict[str, int]]:
        """Active entries per target in order, and each target's revision, read together under the profile lock."""
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            rows = await self.db.fetch(
                "SELECT id, target, text, version FROM memory_entries WHERE profile_id = ? AND state = 'active' "
                "ORDER BY target, position", profile_id)
            revisions = {target: await self._memory_revision(profile_id, target) for target in TARGETS}
        slots: dict[str, list[Slot]] = {target: [] for target in TARGETS}
        for row in rows:
            slots[row["target"]].append(Slot(row["id"], row["text"], row["version"]))
        return slots, revisions

    async def corrected_texts(self, profile_id: str, *, after_message: int,
                              after_time: float) -> dict[str, set[str]]:
        """Text that a foreground or consumer correction replaced or removed after the given evidence: a message id
        when the change has one, else its time. A review of older text must not bring it back."""
        rows = await self.db.fetch(
            "SELECT target, old_text FROM memory_changes WHERE profile_id = ? AND action IN ('remove', 'replace') "
            "AND source_kind IN ('foreground', 'consumer') AND old_text IS NOT NULL "
            "AND (message_id > ? OR (message_id IS NULL AND created_at > ?))",
            profile_id, after_message, after_time)
        found: dict[str, set[str]] = {}
        for row in rows:
            found.setdefault(row["target"], set()).add(row["old_text"])
        return found
