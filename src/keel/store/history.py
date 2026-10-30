"""History search index (cross-session memory design §3): written with each message, read only through ownership
checks, and backfilled for messages older than migration 9."""

from __future__ import annotations

import json

from keel.history import index_tokens, searchable_text
from keel.models import Message, part_from_dict
from keel.store.base import HistoryMessage, HistoryRow, Session


def _history_message(row: dict) -> HistoryMessage:
    from keel.store.sql import _message  # late: sql imports this module

    return HistoryMessage(row["id"], row["session_id"], _message(row).message, row["created_at"], row["run_status"])


def _session(row: dict) -> Session:
    from keel.store.sql import _session as parse  # late: sql imports this module

    return parse(row)


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

    async def search_history(self, profile_id: str, tokens: list[str], *, session_id: str | None = None,
                             exclude_session_id: str | None = None, before: float | None = None,
                             after: float | None = None, candidates: int = 200) -> list[HistoryRow]:
        """Messages of this profile containing any token, filtered first, then the best `candidates` by distinct
        tokens matched and recency, so older high-coverage matches survive the cut."""
        where, args = ["t.profile_id = ?", "t.token = ANY(?::text[])", "s.profile_id = ?"], [profile_id, tokens,
                                                                                           profile_id]
        if session_id is not None:
            where.append("s.session_id = ?")
            args.append(session_id)
        if exclude_session_id is not None:
            where.append("s.session_id <> ?")
            args.append(exclude_session_id)
        if before is not None:
            where.append("s.created_at < ?")
            args.append(before)
        if after is not None:
            where.append("s.created_at >= ?")
            args.append(after)
        ranked = await self.db.fetch(
            "SELECT t.message_id, COUNT(*) AS hits FROM message_tokens t "
            "JOIN message_search s ON s.message_id = t.message_id "
            f"WHERE {' AND '.join(where)} GROUP BY t.message_id, s.created_at "
            "ORDER BY hits DESC, s.created_at DESC, t.message_id DESC LIMIT ?", *args, candidates)
        if not ranked:
            return []
        rows = await self.db.fetch(
            "SELECT s.message_id, s.session_id, ss.title, s.role, s.text, s.created_at, r.status AS run_status "
            "FROM message_search s JOIN sessions ss ON ss.id = s.session_id AND ss.profile_id = s.profile_id "
            "JOIN messages m ON m.id = s.message_id LEFT JOIN runs r ON r.id = m.run_id "
            "WHERE s.profile_id = ? AND s.message_id = ANY(?::bigint[])",
            profile_id, [row["message_id"] for row in ranked])
        found = {row["message_id"]: row for row in rows}
        return [HistoryRow(row["message_id"], found[row["message_id"]]["session_id"],
                           found[row["message_id"]]["title"], found[row["message_id"]]["role"],
                           found[row["message_id"]]["text"], found[row["message_id"]]["created_at"],
                           found[row["message_id"]]["run_status"], int(row["hits"]))
                for row in ranked if row["message_id"] in found]

    async def _owned_session(self, profile_id: str, session_id: str) -> bool:
        return await self.db.fetchrow("SELECT 1 AS n FROM sessions WHERE id = ? AND profile_id = ?",
                                      session_id, profile_id) is not None

    async def history_context(self, profile_id: str, session_id: str, around_id: int | None,
                              window: int) -> tuple[list[HistoryMessage], int | None, int | None] | None:
        """Up to `window` messages each side of the anchor (or the latest 2 * window + 1), with the nearest message
        not returned on each side for continuation. None when the session or anchor is not this profile's."""
        if not await self._owned_session(profile_id, session_id):
            return None
        select = ("SELECT m.*, r.status AS run_status FROM messages m LEFT JOIN runs r ON r.id = m.run_id "
                  "WHERE m.session_id = ? ")
        if around_id is None:
            rows = list(reversed(await self.db.fetch(select + "ORDER BY m.id DESC LIMIT ?", session_id,
                                                     2 * window + 2)))
            earlier = rows.pop(0)["id"] if len(rows) == 2 * window + 2 else None
            return [_history_message(row) for row in rows], earlier, None
        after = await self.db.fetch(select + "AND m.id >= ? ORDER BY m.id LIMIT ?", session_id, around_id, window + 2)
        if not after or after[0]["id"] != around_id:
            return None
        before = list(reversed(await self.db.fetch(select + "AND m.id < ? ORDER BY m.id DESC LIMIT ?", session_id,
                                                   around_id, window + 1)))
        earlier = before.pop(0)["id"] if len(before) == window + 1 else None
        later = after.pop()["id"] if len(after) == window + 2 else None
        return [_history_message(row) for row in before + after], earlier, later

    async def history_message(self, profile_id: str, message_id: int) -> HistoryMessage | None:
        row = await self.db.fetchrow(
            "SELECT m.*, r.status AS run_status FROM messages m JOIN sessions s ON s.id = m.session_id "
            "LEFT JOIN runs r ON r.id = m.run_id WHERE m.id = ? AND s.profile_id = ?", message_id, profile_id)
        return _history_message(row) if row else None

    async def history_sessions(self, profile_id: str, *, before: float | None = None, after: float | None = None,
                               limit: int) -> list[Session]:
        where, args = ["profile_id = ?"], [profile_id]
        if before is not None:
            where.append("last_active_at < ?")
            args.append(before)
        if after is not None:
            where.append("last_active_at >= ?")
            args.append(after)
        rows = await self.db.fetch(
            f"SELECT * FROM sessions WHERE {' AND '.join(where)} ORDER BY last_active_at DESC, id LIMIT ?",
            *args, limit)
        return [_session(row) for row in rows]
