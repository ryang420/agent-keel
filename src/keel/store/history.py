"""History search index (cross-session memory design §3): written with each message, read only through ownership
checks, and backfilled for messages older than migration 9."""

from __future__ import annotations

import json

from keel.history import index_tokens, searchable_text
from keel.models import Message, part_from_dict


class HistoryStoreMixin:
    async def _index_message(self, profile_id: str, session_id: str, message_id: int, message: Message,
                             created_at: float) -> None:
        """Index one message inside the caller's transaction. Re-indexing a message is a no-op."""
        text = searchable_text(message)
        if text is None:
            return
        await self.db.execute(
            "INSERT INTO message_search (message_id, profile_id, session_id, role, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (message_id) DO NOTHING",
            message_id, profile_id, session_id, message.role, text, created_at)
        tokens = sorted(index_tokens(text))
        if tokens:
            await self.db.execute(
                "INSERT INTO message_tokens (profile_id, token, message_id) SELECT ?, unnest(?::text[]), ? "
                "ON CONFLICT DO NOTHING", profile_id, tokens, message_id)

    async def history_index_complete(self, profile_id: str) -> bool:
        row = await self.db.fetchrow("SELECT done_through, ceiling FROM history_backfill WHERE profile_id = ?",
                                     profile_id)
        return row is None or row["done_through"] >= row["ceiling"]

    async def backfill_history(self, batch: int = 200) -> int | None:
        """Index up to `batch` messages of one profile older than its ceiling, under the profile lock that session
        deletion also takes, so a deleted session is never indexed. None when no profile has backfill left."""
        pending = await self.db.fetchrow(
            "SELECT profile_id FROM history_backfill WHERE done_through < ceiling ORDER BY updated_at, profile_id "
            "LIMIT 1")
        if pending is None:
            return None
        profile_id = pending["profile_id"]
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            state = await self.db.fetchrow(
                "SELECT done_through, ceiling FROM history_backfill WHERE profile_id = ?", profile_id)
            if state is None or state["done_through"] >= state["ceiling"]:
                return 0
            rows = await self.db.fetch(
                "SELECT m.id, m.session_id, m.role, m.parts, m.created_at FROM messages m "
                "JOIN sessions s ON s.id = m.session_id "
                "WHERE s.profile_id = ? AND m.id > ? AND m.id <= ? ORDER BY m.id LIMIT ?",
                profile_id, state["done_through"], state["ceiling"], batch)
            for row in rows:
                message = Message(row["role"], tuple(part_from_dict(p) for p in json.loads(row["parts"])))
                await self._index_message(profile_id, row["session_id"], row["id"], message, row["created_at"])
            done = rows[-1]["id"] if len(rows) == batch else state["ceiling"]
            await self.db.execute("UPDATE history_backfill SET done_through = ?, updated_at = ? WHERE profile_id = ?",
                                  done, self.clock(), profile_id)
        return len(rows)
