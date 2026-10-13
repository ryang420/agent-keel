"""Every query the service runs, written once for SQLite and Postgres (`?` placeholders)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Callable

from keel.models import (
    Message, Usage, part_from_dict, part_to_dict, reasoning_from_dict, reasoning_to_dict,
    tool_call_from_dict, tool_call_to_dict,
)
from keel.store.base import (
    TITLE_RANK, WORKER_LOST_MESSAGE, Event, Profile, Run, Session, SessionBusy, SessionNotFound, StoredMessage,
    TitleInUse, UniqueViolation,
)
from keel.store.migrations import migrate

_IN_FLIGHT = "('queued', 'running', 'stopping')"


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _profile(row: dict) -> Profile:
    return Profile(id=row["id"], models=json.loads(row["models"]), sandbox=json.loads(row["sandbox"]),
                   soul=row["soul"], skills=json.loads(row["skills"]), updated_at=row["updated_at"],
                   memory=bool(row["memory"]))


def _session(row: dict) -> Session:
    return Session(
        id=row["id"], profile_id=row["profile_id"], title=row["title"], title_source=row["title_source"],
        created_at=row["created_at"], last_active_at=row["last_active_at"], message_count=row["message_count"],
        system_prompt=row["system_prompt"], provider=row["provider"], model=row["model"], cwd=row["cwd"],
        summary=row["summary"], summary_head_id=row["summary_head_id"], summary_tail_id=row["summary_tail_id"],
        context_tokens=row["context_tokens"], context_message_id=row["context_message_id"],
        compaction_failed_at=row["compaction_failed_at"], turns_since_memory=row["turns_since_memory"],
        pruned_id=row["pruned_id"],
    )


def _message(row: dict) -> StoredMessage:
    message = Message(
        row["role"],
        tuple(part_from_dict(p) for p in json.loads(row["parts"])),
        tuple(tool_call_from_dict(c) for c in json.loads(row["tool_calls"])),
        row["tool_call_id"],
        row["tool_name"],
        tuple(reasoning_from_dict(r) for r in json.loads(row["reasoning"])),
    )
    return StoredMessage(row["id"], row["session_id"], row["run_id"], message, row["created_at"])


def _run(row: dict) -> Run:
    return Run(
        row["id"], row["profile_id"], row["session_id"], row["status"], row["provider"], row["model"],
        row["reasoning_effort"], json.loads(row["input"]), row["instructions"], row["output"],
        Usage.from_dict(json.loads(row["usage"])), row["error_code"], row["error"], row["lease_owner"],
        row["lease_expires_at"], row["created_at"], row["started_at"], row["finished_at"],
        row["current_exec_id"],
    )


def _event(row: dict) -> Event:
    return Event(row["run_id"], row["seq"], row["type"], json.loads(row["data"]), row["at"])


class SqlStore:
    def __init__(self, db, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.clock = clock

    async def init(self) -> None:
        await migrate(self.db)

    async def ping(self) -> bool:
        row = await self.db.fetchrow("SELECT 1 AS ok")
        return row is not None and row["ok"] == 1

    async def close(self) -> None:
        await self.db.close()

    # profiles

    async def upsert_profile(self, profile_id: str, *, models: list[dict], sandbox: dict, soul: str,
                             skills: list[str]) -> Profile:
        await self.db.execute(
            "INSERT INTO profiles (id, models, sandbox, soul, skills, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET models = excluded.models, sandbox = excluded.sandbox, "
            "soul = excluded.soul, skills = excluded.skills, updated_at = excluded.updated_at",
            profile_id, _j(models), _j(sandbox), soul, _j(skills), self.clock())
        return await self.get_profile(profile_id)

    async def get_profile(self, profile_id: str) -> Profile | None:
        row = await self.db.fetchrow("SELECT * FROM profiles WHERE id = ?", profile_id)
        return _profile(row) if row else None

    # sessions

    async def ensure_session(self, profile_id: str, session_id: str) -> Session:
        row = await self.db.fetchrow("SELECT * FROM sessions WHERE id = ?", session_id)
        if row is None:
            now = self.clock()
            try:
                await self.db.execute(
                    "INSERT INTO sessions (id, profile_id, created_at, last_active_at, message_count) "
                    "VALUES (?, ?, ?, ?, 0)", session_id, profile_id, now, now)
            except UniqueViolation:
                pass  # created by a concurrent request
            row = await self.db.fetchrow("SELECT * FROM sessions WHERE id = ?", session_id)
        if row["profile_id"] != profile_id:
            raise SessionNotFound(session_id)
        return _session(row)

    async def get_session(self, profile_id: str, session_id: str) -> Session | None:
        row = await self.db.fetchrow("SELECT * FROM sessions WHERE id = ? AND profile_id = ?",
                                     session_id, profile_id)
        return _session(row) if row else None

    async def list_sessions(self, profile_id: str, limit: int = 100) -> list[Session]:
        rows = await self.db.fetch(
            "SELECT * FROM sessions WHERE profile_id = ? ORDER BY last_active_at DESC, id LIMIT ?",
            profile_id, limit)
        return [_session(row) for row in rows]

    async def set_session_prompt(self, session_id: str, system_prompt: str, provider: str, model: str) -> Session:
        """Store a session's system prompt once; the first writer wins and later calls read it back."""
        await self.db.execute(
            "UPDATE sessions SET system_prompt = ?, provider = ?, model = ? WHERE id = ? AND system_prompt IS NULL",
            system_prompt, provider, model, session_id)
        row = await self.db.fetchrow("SELECT * FROM sessions WHERE id = ?", session_id)
        if row is None:
            raise SessionNotFound(session_id)
        return _session(row)

    async def set_session_cwd(self, session_id: str, cwd: str) -> None:
        await self.db.execute("UPDATE sessions SET cwd = ? WHERE id = ?", cwd, session_id)

    async def set_context_anchor(self, session_id: str, tokens: int, message_id: int) -> None:
        """The input tokens the provider reported for the session's last model call, and the newest message in it."""
        await self.db.execute("UPDATE sessions SET context_tokens = ?, context_message_id = ? WHERE id = ?",
                              tokens, message_id, session_id)

    async def set_pruned(self, session_id: str, pruned_id: int, *, failed_at: float | None = None) -> None:
        """Move the placeholder boundary; the estimate anchor no longer matches what is sent, so it is cleared.
        `failed_at` records a failed summary call."""
        await self.db.execute(
            "UPDATE sessions SET pruned_id = ?, context_tokens = NULL, context_message_id = NULL, "
            "compaction_failed_at = COALESCE(?, compaction_failed_at) WHERE id = ?",
            pruned_id, failed_at, session_id)

    async def compact_session(self, session_id: str, *, summary: str, head_id: int, tail_id: int,
                              system_prompt: str) -> None:
        """Record a compaction. Unlike set_session_prompt, this overwrites the system prompt."""
        await self.db.execute(
            "UPDATE sessions SET summary = ?, summary_head_id = ?, summary_tail_id = ?, pruned_id = ?, "
            "system_prompt = ?, context_tokens = NULL, context_message_id = NULL, compaction_failed_at = NULL "
            "WHERE id = ?",
            summary, head_id, tail_id, tail_id, system_prompt, session_id)

    async def set_title(self, profile_id: str, session_id: str, title: str, source: str) -> Session:
        async with self.db.transaction():
            if source not in TITLE_RANK:
                raise ValueError(f"unknown title source {source!r}")
            allowed = [name for name, rank in TITLE_RANK.items() if rank <= TITLE_RANK[source]]
            marks = ", ".join("?" for _ in allowed)
            try:
                await self.db.execute(
                    "UPDATE sessions SET title = ?, title_source = ? WHERE id = ? AND profile_id = ? "
                    f"AND (title_source IS NULL OR title_source IN ({marks}))",
                    title, source, session_id, profile_id, *allowed)
            except UniqueViolation as exc:
                raise TitleInUse(title) from exc
        session = await self.get_session(profile_id, session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    async def delete_session(self, profile_id: str, session_id: str) -> bool:
        async with self.db.transaction():
            row = await self.db.fetchrow("SELECT id FROM sessions WHERE id = ? AND profile_id = ?",
                                         session_id, profile_id)
            if row is None:
                return False
            busy = await self.db.fetchrow(
                f"SELECT id FROM runs WHERE session_id = ? AND status IN {_IN_FLIGHT} LIMIT 1", session_id)
            if busy is not None:
                raise SessionBusy(session_id)
            await self.db.execute(
                "DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE session_id = ?)", session_id)
            await self.db.execute("DELETE FROM runs WHERE session_id = ?", session_id)
            await self.db.execute("DELETE FROM messages WHERE session_id = ?", session_id)
            await self.db.execute("DELETE FROM blobs WHERE session_id = ?", session_id)
            await self.db.execute("DELETE FROM sessions WHERE id = ?", session_id)
        return True

    # messages

    async def append_message(self, session_id: str, run_id: str | None, message: Message) -> StoredMessage:
        now = self.clock()
        async with self.db.transaction():
            row = await self.db.fetchrow(
                "INSERT INTO messages (session_id, run_id, role, parts, tool_calls, tool_call_id, tool_name, "
                "reasoning, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?) RETURNING id",
                session_id, run_id, message.role, _j([part_to_dict(p) for p in message.parts]),
                _j([tool_call_to_dict(c) for c in message.tool_calls]), message.tool_call_id,
                message.tool_name, _j([reasoning_to_dict(r) for r in message.reasoning]), now)
            await self.db.execute(
                "UPDATE sessions SET message_count = message_count + 1, last_active_at = ? WHERE id = ?",
                now, session_id)
        return StoredMessage(row["id"], session_id, run_id, message, now)

    async def messages(self, session_id: str) -> list[StoredMessage]:
        rows = await self.db.fetch("SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
                                   session_id)
        return [_message(row) for row in rows]

    async def messages_page(self, session_id: str, limit: int, offset: int) -> list[StoredMessage]:
        """One page counted back from the newest message, returned oldest first."""
        rows = await self.db.fetch(
            "SELECT * FROM messages WHERE session_id = ? AND active = 1 ORDER BY id DESC LIMIT ? OFFSET ?",
            session_id, limit, offset)
        return [_message(row) for row in reversed(rows)]

    async def last_message_id(self, session_id: str) -> int:
        row = await self.db.fetchrow("SELECT COALESCE(MAX(id), 0) AS id FROM messages WHERE session_id = ?",
                                     session_id)
        return int(row["id"])

    async def messages_after(self, session_id: str, after_id: int) -> list[StoredMessage]:
        rows = await self.db.fetch("SELECT * FROM messages WHERE session_id = ? AND id > ? ORDER BY id",
                                   session_id, after_id)
        return [_message(row) for row in rows]

    # blobs

    async def put_blob(self, profile_id: str, session_id: str, media_type: str, data_b64: str) -> str:
        blob_id = "blob_" + uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO blobs (id, profile_id, session_id, media_type, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            blob_id, profile_id, session_id, media_type, data_b64, self.clock())
        return blob_id

    async def get_blobs(self, blob_ids: list[str]) -> dict[str, tuple[str, str]]:
        if not blob_ids:
            return {}
        marks = ", ".join("?" for _ in blob_ids)
        rows = await self.db.fetch(f"SELECT id, media_type, data FROM blobs WHERE id IN ({marks})", *blob_ids)
        return {row["id"]: (row["media_type"], row["data"]) for row in rows}

    async def delete_blobs(self, blob_ids: list[str]) -> None:
        if blob_ids:
            marks = ", ".join("?" for _ in blob_ids)
            await self.db.execute(f"DELETE FROM blobs WHERE id IN ({marks})", *blob_ids)

    # runs

    async def create_run(self, *, profile_id: str, session_id: str, provider: str, model: str,
                         reasoning_effort: str | None, input_parts: list[dict], instructions: str | None) -> Run:
        run_id = "run_" + uuid.uuid4().hex
        try:
            await self._insert_run(run_id, profile_id, session_id, provider, model, reasoning_effort,
                                   input_parts, instructions)
        except UniqueViolation as exc:
            raise SessionBusy(session_id) from exc
        return await self.get_run_by_id(run_id)

    async def _insert_run(self, run_id, profile_id, session_id, provider, model, reasoning_effort,
                          input_parts, instructions) -> None:
        await self.db.execute(
            "INSERT INTO runs (id, profile_id, session_id, status, provider, model, reasoning_effort, input, "
            "instructions, output, usage, created_at) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, '', '{}', ?)",
            run_id, profile_id, session_id, provider, model, reasoning_effort, _j(input_parts), instructions,
            self.clock())

    async def get_run(self, profile_id: str, run_id: str) -> Run | None:
        row = await self.db.fetchrow("SELECT * FROM runs WHERE id = ? AND profile_id = ?", run_id, profile_id)
        return _run(row) if row else None

    async def get_run_by_id(self, run_id: str) -> Run | None:
        row = await self.db.fetchrow("SELECT * FROM runs WHERE id = ?", run_id)
        return _run(row) if row else None

    async def list_runs(self, profile_id: str, statuses: tuple[str, ...]) -> list[Run]:
        if not statuses:
            return []
        marks = ", ".join("?" for _ in statuses)
        rows = await self.db.fetch(
            f"SELECT * FROM runs WHERE profile_id = ? AND status IN ({marks}) ORDER BY created_at, id",
            profile_id, *statuses)
        return [_run(row) for row in rows]

    async def count_in_flight(self) -> int:
        row = await self.db.fetchrow(f"SELECT COUNT(*) AS n FROM runs WHERE status IN {_IN_FLIGHT}")
        return int(row["n"])

    async def in_flight_profiles(self) -> set[str]:
        rows = await self.db.fetch(f"SELECT DISTINCT profile_id FROM runs WHERE status IN {_IN_FLIGHT}")
        return {row["profile_id"] for row in rows}

    async def active_run_for_session(self, session_id: str) -> Run | None:
        row = await self.db.fetchrow(
            f"SELECT * FROM runs WHERE session_id = ? AND status IN {_IN_FLIGHT} ORDER BY created_at LIMIT 1",
            session_id)
        return _run(row) if row else None

    async def claim_run(self, owner: str, lease_until: float) -> Run | None:
        lock = " FOR UPDATE SKIP LOCKED" if self.db.dialect == "postgres" else ""
        row = await self.db.fetchrow(
            "UPDATE runs SET status = 'running', lease_owner = ?, lease_expires_at = ?, started_at = ? "
            "WHERE id = (SELECT id FROM runs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
            f"{lock}) RETURNING *",
            owner, lease_until, self.clock())
        return _run(row) if row else None

    async def renew_lease(self, run_id: str, owner: str, lease_until: float) -> str | None:
        row = await self.db.fetchrow(
            "UPDATE runs SET lease_expires_at = ? WHERE id = ? AND lease_owner = ? "
            "AND status IN ('running', 'stopping') RETURNING status",
            lease_until, run_id, owner)
        return row["status"] if row else None

    async def request_stop(self, profile_id: str, run_id: str) -> Run | None:
        await self.db.execute(
            "UPDATE runs SET status = 'cancelled', finished_at = ? WHERE id = ? AND profile_id = ? "
            "AND status = 'queued'", self.clock(), run_id, profile_id)
        await self.db.execute(
            "UPDATE runs SET status = 'stopping' WHERE id = ? AND profile_id = ? AND status = 'running'",
            run_id, profile_id)
        return await self.get_run(profile_id, run_id)

    async def set_current_exec(self, run_id: str, exec_id: str | None) -> None:
        await self.db.execute("UPDATE runs SET current_exec_id = ? WHERE id = ?", exec_id, run_id)

    async def finish_run(self, run_id: str, status: str, *, output: str = "", usage: Usage = Usage(),
                         error_code: str | None = None, error: str | None = None) -> bool:
        row = await self.db.fetchrow(
            "UPDATE runs SET status = ?, output = ?, usage = ?, error_code = ?, error = ?, finished_at = ?, "
            f"lease_owner = NULL, lease_expires_at = NULL WHERE id = ? AND status IN {_IN_FLIGHT} RETURNING id",
            status, output, _j(usage.to_dict()), error_code, error, self.clock(), run_id)
        return row is not None

    async def expire_leases(self) -> list[Run]:
        now = self.clock()
        rows = await self.db.fetch(
            "UPDATE runs SET status = 'failed', error_code = 'worker_lost', error = ?, finished_at = ?, "
            "lease_owner = NULL, lease_expires_at = NULL "
            "WHERE status IN ('running', 'stopping') AND lease_expires_at < ? RETURNING *",
            WORKER_LOST_MESSAGE, now, now)
        return [_run(row) for row in rows]

    # events

    async def append_event(self, run_id: str, type_: str, data: dict) -> Event:
        now = self.clock()
        for attempt in range(3):
            try:
                async with self.db.transaction():
                    row = await self.db.fetchrow(
                        "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM run_events WHERE run_id = ?", run_id)
                    seq = int(row["seq"])
                    await self.db.execute(
                        "INSERT INTO run_events (run_id, seq, type, data, at) VALUES (?, ?, ?, ?, ?)",
                        run_id, seq, type_, _j(data), now)
                return Event(run_id, seq, type_, data, now)
            except UniqueViolation:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    async def events_after(self, run_id: str, after_seq: int) -> list[Event]:
        rows = await self.db.fetch("SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                                   run_id, after_seq)
        return [_event(row) for row in rows]

    async def prune_events(self, finished_before: float, batch: int = 10_000) -> int:
        """Delete the events of runs that finished before `finished_before`, `batch` rows per statement so no pass
        holds a long transaction. Runs, their output and the session's messages are kept."""
        total = 0
        while True:
            rows = await self.db.fetch(
                "DELETE FROM run_events WHERE (run_id, seq) IN (SELECT e.run_id, e.seq FROM run_events e "
                "JOIN runs r ON r.id = e.run_id WHERE r.finished_at < ? LIMIT ?) RETURNING seq",
                finished_before, batch)
            total += len(rows)
            if len(rows) < batch:
                return total
