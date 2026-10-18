"""Every query the service runs, written once for SQLite and Postgres (`?` placeholders)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Callable, Sequence

from keel.memory import apply_entries
from keel.models import (
    Message, Usage, part_from_dict, part_to_dict, reasoning_from_dict, reasoning_to_dict,
    tool_call_from_dict, tool_call_to_dict,
)
from keel.store.base import (
    TERMINAL, TERMINAL_EVENT, TITLE_RANK, WORKER_LOST_MESSAGE, Event, ImportResult, Profile, ProfileTenantMismatch,
    Run, RunTransition, Session, SessionBusy, SessionNotFound, StoredMessage, TerminalEventType, TitleInUse,
    UniqueViolation, UnknownRun,
)
from keel.store.migrations import migrate
from keel.store.sandboxes import SandboxStoreMixin

_IN_FLIGHT = "('queued', 'running', 'stopping')"


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _profile(row: dict) -> Profile:
    return Profile(id=row["id"], models=json.loads(row["models"]), sandbox=json.loads(row["sandbox"]),
                   soul=row["soul"], skills=json.loads(row["skills"]), updated_at=row["updated_at"],
                   memory=bool(row["memory"]), tenant_id=row["tenant_id"])


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


class SqlStore(SandboxStoreMixin):
    def __init__(self, db, clock: Callable[[], float] = time.time,
                 coordination_clock: Callable[[], float] | None = None) -> None:
        self.db = db
        self.clock = clock  # legacy transcript time
        self.coordination_clock = coordination_clock  # test-only override for database coordination time

    async def init(self) -> None:
        await migrate(self.db)

    async def ping(self) -> bool:
        row = await self.db.fetchrow("SELECT 1 AS ok")
        return row is not None and row["ok"] == 1

    async def coordination_now(self) -> float:
        """Database time for coordination deadlines, or the injected test clock when supplied."""
        if self.coordination_clock is not None:
            return self.coordination_clock()
        return await self.db.database_now()

    async def close(self) -> None:
        await self.db.close()

    # profiles

    async def upsert_profile(self, profile_id: str, *, models: list[dict], sandbox: dict, soul: str,
                             skills: list[str], memory: bool = False, tenant_id: int | None = None) -> Profile:
        await self.db.execute(
            "INSERT INTO profiles (id, models, sandbox, soul, skills, updated_at, memory, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET models = excluded.models, sandbox = excluded.sandbox, "
            "soul = excluded.soul, skills = excluded.skills, updated_at = excluded.updated_at, "
            "memory = excluded.memory, tenant_id = COALESCE(profiles.tenant_id, excluded.tenant_id) "
            "WHERE excluded.tenant_id IS NULL OR profiles.tenant_id IS NULL "
            "OR profiles.tenant_id = excluded.tenant_id",
            profile_id, _j(models), _j(sandbox), soul, _j(skills), self.clock(), int(memory), tenant_id)
        saved = await self.get_profile(profile_id)
        if tenant_id is not None and (saved is None or saved.tenant_id != tenant_id):
            raise ProfileTenantMismatch(profile_id)
        return saved

    async def get_profile(self, profile_id: str) -> Profile | None:
        row = await self.db.fetchrow("SELECT * FROM profiles WHERE id = ?", profile_id)
        return _profile(row) if row else None

    async def profile_sandboxes(self) -> list[tuple[str, dict]]:
        rows = await self.db.fetch("SELECT id, sandbox FROM profiles")
        return [(row["id"], json.loads(row["sandbox"])) for row in rows]

    # memory

    async def apply_memory(self, profile_id: str, target: str, limit: int, ops: list[dict]) -> dict:
        """Read, modify and write one target in a single transaction (a row lock on Postgres)."""
        if target not in ("memory", "user"):
            return {"success": False, "error": "target must be memory or user.", "entries": []}
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO memories (profile_id, target, entries, updated_at) VALUES (?, ?, '[]', ?) "
                "ON CONFLICT (profile_id, target) DO NOTHING",
                profile_id, target, self.clock())
            lock = " FOR UPDATE" if self.db.dialect == "postgres" else ""
            row = await self.db.fetchrow(
                f"SELECT entries FROM memories WHERE profile_id = ? AND target = ?{lock}", profile_id, target)
            current = json.loads(row["entries"])
            updated, result = apply_entries(current, ops, limit)
            if result["success"] and updated != current:
                await self.db.execute(
                    "UPDATE memories SET entries = ?, updated_at = ? WHERE profile_id = ? AND target = ?",
                    _j(updated), self.clock(), profile_id, target)
            return result

    async def memory_entries(self, profile_id: str) -> dict[str, list[str]]:
        rows = await self.db.fetch("SELECT target, entries FROM memories WHERE profile_id = ?", profile_id)
        found = {row["target"]: json.loads(row["entries"]) for row in rows}
        return {"memory": found.get("memory", []), "user": found.get("user", [])}

    async def reset_memory_turns(self, session_id: str) -> None:
        await self.db.execute("UPDATE sessions SET turns_since_memory = 0 WHERE id = ?", session_id)

    async def note_memory_turn(self, session_id: str, threshold: int) -> bool:
        """Count one completed run toward the next review. At the threshold the counter returns to 0 and this
        is True."""
        row = await self.db.fetchrow(
            "UPDATE sessions SET turns_since_memory = CASE WHEN turns_since_memory + 1 >= ? THEN 0 "
            "ELSE turns_since_memory + 1 END WHERE id = ? RETURNING turns_since_memory",
            threshold, session_id)
        return row is not None and row["turns_since_memory"] == 0

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

    async def import_session(self, *, profile_id: str, tenant_id: int, session_id: str, title: str | None,
                             title_source: str | None, created_at: float, last_active_at: float,
                             messages: Sequence[tuple[Message, float]]) -> ImportResult:
        """Write one conversation copied from another system as it was: its title, times and messages.

        A session id that exists already, under any profile, is left alone. A missing profile is created empty for
        `tenant_id`; the service's next profile update fills it in. One transaction: a failure writes nothing."""
        async with self.db.transaction():
            if await self.db.fetchrow("SELECT id FROM sessions WHERE id = ?", session_id) is not None:
                return ImportResult("exists", False, ())
            profile = await self.get_profile(profile_id)
            if profile is None:
                await self.upsert_profile(profile_id, models=[], sandbox={}, soul="", skills=[], tenant_id=tenant_id)
            elif profile.tenant_id != tenant_id:
                raise ProfileTenantMismatch(profile_id)
            title = await self._free_title(profile_id, title)
            await self.db.execute(
                "INSERT INTO sessions (id, profile_id, title, title_source, created_at, last_active_at, "
                "message_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                session_id, profile_id, title, title_source if title is not None else None, created_at,
                last_active_at, len(messages))
            ids = []
            for message, at in messages:
                row = await self.db.fetchrow(
                    "INSERT INTO messages (session_id, run_id, role, parts, tool_calls, tool_call_id, tool_name, "
                    "reasoning, active, created_at) VALUES (?, NULL, ?, ?, ?, ?, ?, '[]', 1, ?) RETURNING id",
                    session_id, message.role, _j([part_to_dict(p) for p in message.parts]),
                    _j([tool_call_to_dict(c) for c in message.tool_calls]), message.tool_call_id,
                    message.tool_name, at)
                ids.append(row["id"])
        return ImportResult("imported", profile is None, tuple(ids), title)

    async def _free_title(self, profile_id: str, title: str | None) -> str | None:
        """`title`, or `title (n)` for the first n from 2 the profile does not use (titles are unique per profile)."""
        if title is None:
            return None
        candidate, n = title, 2
        while await self.db.fetchrow("SELECT id FROM sessions WHERE profile_id = ? AND title = ?",
                                     profile_id, candidate) is not None:
            candidate, n = f"{title} ({n})", n + 1
        return candidate

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

    async def get_blobs(self, blob_ids: list[str], *, profile_id: str,
                        session_id: str) -> dict[str, tuple[str, str]]:
        if not blob_ids:
            return {}
        marks = ", ".join("?" for _ in blob_ids)
        rows = await self.db.fetch(
            f"SELECT id, media_type, data FROM blobs WHERE id IN ({marks}) AND profile_id = ? AND session_id = ?",
            *blob_ids, profile_id, session_id)
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

    async def completed_run_count(self, session_id: str, *, except_id: str) -> int:
        row = await self.db.fetchrow(
            "SELECT COUNT(*) AS n FROM runs WHERE session_id = ? AND status = 'completed' AND id <> ?",
            session_id, except_id)
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

    async def _locked_run(self, run_id: str, profile_id: str | None = None) -> dict | None:
        """Read a run under the transaction's row lock (Postgres) or its BEGIN IMMEDIATE write lock (SQLite)."""
        lock = " FOR UPDATE" if self.db.dialect == "postgres" else ""
        if profile_id is None:
            return await self.db.fetchrow(f"SELECT * FROM runs WHERE id = ?{lock}", run_id)
        return await self.db.fetchrow(f"SELECT * FROM runs WHERE id = ? AND profile_id = ?{lock}",
                                      run_id, profile_id)

    async def request_stop(self, profile_id: str, run_id: str) -> RunTransition | None:
        """Move a run toward cancellation, committing the queued-run transition and its event together.

        A queued run becomes cancelled with its `run.cancelled` event; a running run becomes stopping with no event
        yet; a stopping or terminal run is returned as it is. Exactly one caller can commit the cancellation.
        """
        async with self.db.transaction():
            row = await self._locked_run(run_id, profile_id)
            if row is None:
                return None
            if row["status"] == "queued":
                now = self.clock()
                updated = await self.db.fetchrow(
                    "UPDATE runs SET status = 'cancelled', finished_at = ?, lease_owner = NULL, "
                    "lease_expires_at = NULL WHERE id = ? AND status = 'queued' RETURNING *", now, run_id)
                if updated is None:  # unreachable under the lock; never invent an event
                    return RunTransition(_run(row), None)
                event = await self._insert_event_locked(run_id, "run.cancelled", {}, now)
                return RunTransition(_run(updated), event)
            if row["status"] == "running":
                updated = await self.db.fetchrow(
                    "UPDATE runs SET status = 'stopping' WHERE id = ? AND status = 'running' RETURNING *", run_id)
                return RunTransition(_run(updated if updated is not None else row), None)
            return RunTransition(_run(row), None)

    async def set_current_exec(self, run_id: str, exec_id: str | None) -> None:
        await self.db.execute("UPDATE runs SET current_exec_id = ? WHERE id = ?", exec_id, run_id)

    async def finish_run(self, run_id: str, status: str, *, output: str = "", usage: Usage = Usage(),
                         error_code: str | None = None, error: str | None = None,
                         incomplete: bool = False) -> RunTransition | None:
        """Finish an active run and insert its terminal event in one transaction.

        A missing run returns None. An already-terminal run is returned unchanged with no event, so retrying a
        transition cannot overwrite its outcome. Outcome arguments are validated before anything is read or written.
        """
        if status not in TERMINAL:
            raise ValueError(f"unknown terminal status {status!r}")
        if status == "failed" and (not error_code or not error):
            raise ValueError("a failed run needs an error code and a message")
        if incomplete and status != "completed":
            raise ValueError("only a completed run can be incomplete")
        async with self.db.transaction():
            row = await self._locked_run(run_id)
            if row is None:
                return None
            if row["status"] in TERMINAL:
                return RunTransition(_run(row), None)
            now = self.clock()
            updated = await self.db.fetchrow(
                "UPDATE runs SET status = ?, output = ?, usage = ?, error_code = ?, error = ?, finished_at = ?, "
                f"lease_owner = NULL, lease_expires_at = NULL WHERE id = ? AND status IN {_IN_FLIGHT} RETURNING *",
                status, output, _j(usage.to_dict()), error_code, error, now, run_id)
            if updated is None:  # unreachable under the lock; never invent an event
                return RunTransition(_run(row), None)
            if status == "completed":
                data = {"output": output, "usage": usage.to_dict()}
                if incomplete:
                    data["incomplete"] = True
            elif status == "failed":
                data = {"error_code": error_code, "message": error}
            else:
                data = {}
            event = await self._insert_event_locked(run_id, TERMINAL_EVENT[status], data, now)
            return RunTransition(_run(updated), event)

    async def expire_leases(self, *, batch: int = 100) -> list[RunTransition]:
        """Fail one bounded batch of runs whose lease expired, their terminal events committing with them.

        Postgres locks the selected rows with SKIP LOCKED, so a sweep, a renewal and a finish serialise on the run
        row. SQLite serialises writers with BEGIN IMMEDIATE. A failure rolls the whole batch back; the next pass
        selects the same runs again.
        """
        now = self.clock()
        async with self.db.transaction():
            lock = " FOR UPDATE SKIP LOCKED" if self.db.dialect == "postgres" else ""
            rows = await self.db.fetch(
                "SELECT * FROM runs WHERE status IN ('running', 'stopping') AND lease_expires_at < ? "
                f"ORDER BY id LIMIT ?{lock}", now, batch)
            transitions = []
            for row in rows:
                updated = await self.db.fetchrow(
                    "UPDATE runs SET status = 'failed', error_code = 'worker_lost', error = ?, finished_at = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL WHERE id = ? AND status IN ('running', 'stopping') "
                    "AND lease_expires_at < ? RETURNING *", WORKER_LOST_MESSAGE, now, row["id"], now)
                if updated is None:  # renewed or finished despite the lock (defense in depth)
                    continue
                event = await self._insert_event_locked(
                    row["id"], "run.failed", {"error_code": "worker_lost", "message": WORKER_LOST_MESSAGE}, now)
                transitions.append(RunTransition(_run(updated), event))
            return transitions

    # events

    async def _insert_event_locked(self, run_id: str, type_: str, data: dict, at: float) -> Event:
        """Insert one event. The caller holds the transaction and the run lock, so sequence allocation serialises."""
        row = await self.db.fetchrow(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM run_events WHERE run_id = ?", run_id)
        seq = int(row["seq"])
        await self.db.execute(
            "INSERT INTO run_events (run_id, seq, type, data, at) VALUES (?, ?, ?, ?, ?)",
            run_id, seq, type_, _j(data), at)
        return Event(run_id, seq, type_, data, at)

    async def append_event(self, run_id: str, type_: str, data: dict) -> Event | None:
        """Append an ordinary event, serialising with termination on the run row.

        Terminal event types belong to the lifecycle operations and are rejected; an unknown run is rejected
        rather than left with orphan events. A terminal run returns None, which a late writer can stop quietly on.
        """
        if type_ in TERMINAL_EVENT.values():
            raise TerminalEventType(f"terminal event {type_!r} is written by the run transition itself")
        async with self.db.transaction():
            row = await self._locked_run(run_id)
            if row is None:
                raise UnknownRun(f"no such run {run_id!r}")
            if row["status"] in TERMINAL:
                return None
            return await self._insert_event_locked(run_id, type_, data, self.clock())

    async def events_after(self, run_id: str, after_seq: int) -> list[Event]:
        rows = await self.db.fetch("SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                                   run_id, after_seq)
        return [_event(row) for row in rows]

    async def prune_events(self, finished_before: float, finished_after: float | None = None,
                           batch: int = 10_000) -> int:
        """Delete the events of runs that finished before `finished_before` (and at or after `finished_after`, so a
        regular pass does not rescan all history), `batch` rows per statement so no pass holds a long transaction.
        Runs, their output and the session's messages are kept."""
        bound, args = ("", ()) if finished_after is None else (" AND r.finished_at >= ?", (finished_after,))
        total = 0
        while True:
            rows = await self.db.fetch(
                "DELETE FROM run_events WHERE (run_id, seq) IN (SELECT e.run_id, e.seq FROM run_events e "
                f"JOIN runs r ON r.id = e.run_id WHERE r.finished_at < ?{bound} LIMIT ?) RETURNING seq",
                finished_before, *args, batch)
            total += len(rows)
            if len(rows) < batch:
                return total
