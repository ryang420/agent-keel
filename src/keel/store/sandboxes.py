"""Guarded SQL transitions for sandbox admission, usage leases and lifecycle ownership.

Every method is a short transaction: no Docker or model call is awaited while a transaction is open. Decisions read
database time after taking the profile guard, and every conditional update rechecks the identities recorded by its
caller, so ordinary lease or state loss returns None instead of raising. Rows retain their evidence; a stale result
only ever attaches an immutable identity to its original attempt.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal

from keel.models import Usage
from keel.sandbox.base import PolicyError, SandboxPolicy, parse_policy
from keel.sandbox.records import (
    MAX_DIAGNOSTIC_CHARS, BoundSandbox, CleanupReceipt, ContainerObservation, DockerOutcome, ExecTicket,
    LeaseStamp, LifecycleAttempt, RecoveryAction, RunClaim, SandboxBinding, SandboxBusy, SandboxKey,
    SandboxLeaseLost, SandboxUsage, UsageBinding, UseHandle,
)
from keel.sandbox.runtime import HELPER_PROTOCOL_VERSION
from keel.store.base import Run, UniqueViolation
from keel.store.deletions import ProfileDeletionAuthority

_PREPARATION_KINDS = ("create", "install", "start")
_RENEWABLE_USAGE_STATES = ("preparing", "active", "cleaning")
_UNRELEASED_USAGE_STATES = ("preparing", "active", "cleaning")
_UNRESOLVED_EXEC_STATES = ("pending", "running", "uncertain")
# Stored error codes that withhold new admission for a profile until the recorded conflict is resolved.
_INVENTORY_BLOCK_CODES = ("sandbox_inventory_conflict", "sandbox_retired_create_running",
                          "sandbox_daemon_mismatch")
_SANDBOX_IDENTITY = "deployment = ? AND profile_id = ?"
_USAGE_IDENTITY = "id = ? AND token = ? AND owner = ? AND deployment = ? AND profile_id = ? AND daemon_id = ?"
_BINDING_COLUMNS = ("generation", "generation_token", "container_id", "container_name")


def _bounded(text: str | None) -> str | None:
    return None if text is None else text[:MAX_DIAGNOSTIC_CHARS]


def _delete_ids(table: str, ids: list[str]) -> str:
    """A bounded delete over internal table names only; callers append their own eligibility predicates."""
    return f"DELETE FROM {table} WHERE id IN ({','.join('?' for _ in ids)})"


def _usage_binding_is_null(row: dict) -> bool:
    return all(row[column] is None for column in _BINDING_COLUMNS)


def _usage_matches_binding(row: dict, binding: SandboxBinding) -> bool:
    return ((row["generation"], row["generation_token"], row["container_id"], row["container_name"])
            == (binding.generation, binding.generation_token, binding.container_id, binding.container_name))


def _generation_draining(usages: list[dict], execs: list[dict], operations: list[dict], token: str,
                         now: float) -> bool:
    """True while a usage lease, execution or current operation still forbids restoring `ready`.

    An active usage with an unexpired lease is the ordinary shared-generation case. An expired, preparing,
    cleaning or orphaned usage, an unresolved execution or an unresolved current operation is a drain reason.
    """
    if any(use["state"] in ("preparing", "cleaning", "orphaned")
           or (use["state"] != "released"
               and (use["lease_expires_at"] is None or use["lease_expires_at"] <= now))
           for use in usages):
        return True
    if any(ex["generation_token"] == token and ex["state"] in _UNRESOLVED_EXEC_STATES for ex in execs):
        return True
    return any(op["generation_token"] == token and op["authority"] == "current"
               and op["outcome"] in ("planned", "dispatched", "uncertain")
               and op["confirmed_absent_at"] is None for op in operations)


def _linked_to_usage(row: dict, usage_id: str, create_id: str | None) -> bool:
    """True for preparation rows this usage owns, both before and after its create is recorded."""
    return row["usage_id"] == usage_id or (create_id is not None
                                           and row["original_create_operation_id"] == create_id)


def _unbound_retirement_blocker(usage: dict, create_op: dict | None, operations: list[dict],
                                execs: list[dict]) -> str | None:
    """A reason section 11.1 cannot release this unbound preparation, or None when it may."""
    if not _usage_binding_is_null(usage):
        return "the usage is bound to a generation"
    if usage["state"] not in ("preparing", "orphaned"):
        return "the usage is not an unbound preparation"
    create_id = usage["create_operation_id"]
    if create_id is not None:
        if create_op is None or create_op["kind"] != "create" or create_op["usage_id"] != usage["id"]:
            return "the recorded create operation is missing or conflicting"
    elif any(row["kind"] == "create" and row["usage_id"] == usage["id"] for row in operations):
        return "a create operation is linked without a recorded pointer"
    if any(row["usage_id"] == usage["id"] and row["state"] != "retired" for row in execs):
        return "an execution record is not retired"
    for row in operations:
        if not _linked_to_usage(row, usage["id"], create_id):
            continue
        if (row["kind"] == "start" and row["outcome"] != "definite_failure"
                and row["confirmed_absent_at"] is None):
            return "an authorized start remains"
        if (row["kind"] == "install" and row["outcome"] in ("dispatched", "succeeded", "uncertain")
                and row["container_id"] is not None and row["confirmed_absent_at"] is None):
            return "an install against a known target is not confirmed absent"
    return None


def _run(row: dict) -> Run:
    return Run(
        row["id"], row["profile_id"], row["session_id"], row["status"], row["provider"], row["model"],
        row["reasoning_effort"], json.loads(row["input"]), row["instructions"], row["output"],
        Usage.from_dict(json.loads(row["usage"])), row["error_code"], row["error"], row["lease_owner"],
        row["lease_expires_at"], row["created_at"], row["started_at"], row["finished_at"],
        row["current_exec_id"], kind=row["kind"],
        command=None if row["command"] is None else json.loads(row["command"]),
    )


def _key(row: dict) -> SandboxKey:
    return SandboxKey(row["deployment"], row["profile_id"], row["daemon_id"])


def _usage(row: dict) -> SandboxUsage:
    binding = None
    if all(row[column] is not None for column in
           ("generation", "generation_token", "container_id", "container_name")):
        binding = UsageBinding(row["generation"], row["generation_token"], row["container_id"],
                               row["container_name"])
    return SandboxUsage(
        id=row["id"], token=row["token"], key=_key(row), run_id=row["run_id"], owner=row["owner"],
        state=row["state"], heartbeat_at=row["heartbeat_at"], lease_expires_at=row["lease_expires_at"],
        session_id=row["session_id"], binding=binding, create_operation_id=row["create_operation_id"],
        released_at=row["released_at"], release_reason=row["release_reason"],
    )


def _attempt(row: dict) -> LifecycleAttempt:
    return LifecycleAttempt(
        id=row["id"], kind=row["kind"], key=_key(row), generation=row["generation"],
        generation_token=row["generation_token"], authority=row["authority"], outcome=row["outcome"],
        owner=row["owner"], owner_token=row["owner_token"], created_at=row["created_at"],
        updated_at=row["updated_at"], intended_name=row["intended_name"],
        original_create_operation_id=row["original_create_operation_id"], usage_id=row["usage_id"],
        usage_token=row["usage_token"], container_id=row["container_id"],
        lease_expires_at=row["lease_expires_at"], retired_at=row["retired_at"],
        retirement_reason=row["retirement_reason"], result_code=row["result_code"],
        result_message=row["result_message"], discovered_container_id=row["discovered_container_id"],
        cleanup_operation_id=row["cleanup_operation_id"], confirmed_absent_at=row["confirmed_absent_at"],
        resolved_at=row["resolved_at"], run_id=row["run_id"], session_id=row["session_id"],
    )


def _effective_digest(sandbox: dict) -> str | None:
    """The profile's canonical policy digest, or None when the policy cannot be compared here."""
    try:
        return parse_policy(sandbox, mount_root="/").digest()
    except (PolicyError, ValueError, TypeError):
        return None


def _intended_name(profile_id: str, generation_token: str) -> str:
    profile_hash = hashlib.sha256(profile_id.encode()).hexdigest()[:12]
    return f"keel-sbx-{profile_hash}-{generation_token[:16]}"


def _has_preparation_in_flight(sandbox_row: dict | None, operations: list[dict]) -> bool:
    """True while another claim owns a create/install/start step for the current generation.

    Once a generation is `ready`, its succeeded preparation steps are complete: only a newly planned or dispatched
    step (for example a reinstall) still blocks admission. Before readiness any non-definite attempt does.
    """
    published = sandbox_row["generation"] if sandbox_row is not None and sandbox_row["state"] == "ready" else None
    for operation in operations:
        if operation["kind"] not in _PREPARATION_KINDS:
            continue
        if published is not None and operation["generation"] == published:
            if operation["outcome"] in ("planned", "dispatched"):
                return True
            continue
        if operation["outcome"] != "definite_failure":
            return True
    return False


class SandboxStoreMixin:
    """Coordination methods mixed into `SqlStore`; `self.db` and `coordination_now` come from there."""

    @asynccontextmanager
    async def profile_guard(self, profile_id: str, *, skip_locked: bool = False) -> AsyncIterator[bool]:
        """Serialize admission/policy work on one profile; yields whether the profile row exists.

        PostgreSQL locks the profile row (`SKIP LOCKED` when asked, so a busy profile is not waited on). SQLite's
        BEGIN IMMEDIATE write transaction serializes writers. An already open transaction is joined, not nested.
        """
        async with self.db.transaction():
            if self.db.dialect == "postgres":
                if skip_locked:
                    locked = await self.db.fetchrow(
                        "SELECT pg_try_advisory_xact_lock(hashtext('keel.profile'), hashtext(?)) AS locked",
                        profile_id)
                    if not locked["locked"]:
                        yield False
                        return
                else:
                    await self.lock_profile(profile_id)
            if self.db.dialect == "postgres":
                lock = " FOR UPDATE SKIP LOCKED" if skip_locked else " FOR UPDATE"
                row = await self.db.fetchrow(f"SELECT id FROM profiles WHERE id = ?{lock}", profile_id)
            else:
                row = await self.db.fetchrow("SELECT id FROM profiles WHERE id = ?", profile_id)
            yield row is not None

    def _lock_suffix(self) -> str:
        """Postgres row locks explicitly; SQLite's BEGIN IMMEDIATE write transaction already serializes."""
        return " FOR UPDATE" if self.db.dialect == "postgres" else ""

    @asynccontextmanager
    async def coordination_guard(self, profile_id: str, *, skip_locked: bool = False) -> AsyncIterator[bool]:
        """Serialize coordination work for a profile that may already have been deleted.

        Cleanup, release and recovery must finish after a terminal session/run/profile deletion, so unlike
        `profile_guard` this never refuses work. PostgreSQL locks the profile row when it exists; the sandbox row
        and SQLite's write transaction carry the serialization otherwise.
        """
        async with self.db.transaction():
            if self.db.dialect == "postgres":
                if skip_locked:
                    locked = await self.db.fetchrow(
                        "SELECT pg_try_advisory_xact_lock(hashtext('keel.profile'), hashtext(?)) AS locked",
                        profile_id)
                    if not locked["locked"]:
                        yield False
                        return
                else:
                    await self.lock_profile(profile_id)
                suffix = " FOR UPDATE SKIP LOCKED" if skip_locked else " FOR UPDATE"
                row = await self.db.fetchrow("SELECT id FROM profiles WHERE id = ?" + suffix, profile_id)
                if row is None and skip_locked:
                    exists = await self.db.fetchrow("SELECT id FROM profiles WHERE id = ?", profile_id)
                    if exists is not None:
                        yield False
                        return
                    guard = await self.db.fetchrow(
                        "SELECT profile_id FROM sandboxes WHERE profile_id = ? FOR UPDATE SKIP LOCKED", profile_id)
                    if guard is None and await self.db.fetchrow(
                            "SELECT profile_id FROM sandboxes WHERE profile_id = ?", profile_id) is not None:
                        yield False
                        return
            yield True

    async def _lock_sandbox(self, key: SandboxKey, lock: str) -> dict | None:
        return await self.db.fetchrow(
            f"SELECT * FROM sandboxes WHERE {_SANDBOX_IDENTITY}{lock}", key.deployment, key.profile_id)

    async def _lock_operations(self, key: SandboxKey, lock: str) -> list[dict]:
        return await self.db.fetch(
            f"SELECT * FROM sandbox_operations WHERE deployment = ? AND profile_id = ? ORDER BY id{lock}",
            key.deployment, key.profile_id)

    async def _lock_usages(self, key: SandboxKey, lock: str) -> list[dict]:
        return await self.db.fetch(
            f"SELECT * FROM sandbox_uses WHERE deployment = ? AND profile_id = ? ORDER BY id{lock}",
            key.deployment, key.profile_id)

    async def _lock_execs(self, key: SandboxKey, lock: str) -> list[dict]:
        return await self.db.fetch(
            f"SELECT * FROM sandbox_execs WHERE deployment = ? AND profile_id = ? ORDER BY id{lock}",
            key.deployment, key.profile_id)

    # admission

    async def claim_run_with_usage(self, owner: str, lease_seconds: float, *, deployment: str,
                                   daemon_id: str | None, helper_digest: str | None = None) -> RunClaim | None:
        """Claim one queued run and reserve its usage under the profile guard.

        Candidates are read without write locks. Each profile is tried with `SKIP LOCKED`, so a blocked or busy
        profile at the head of the queue does not stop a healthy later profile. A ready compatible generation is
        shared immediately; otherwise this claim becomes the only preparation owner. `helper_digest` is the
        caller's local helper artifact: a generation published with another one is not shared, and recovery must
        replace it. A NULL published digest (a row from before helper recording) stays usable. A None `daemon_id`
        means sandbox admission is withheld (Docker or the helper cannot be served): only chat-only runs are claimed.
        """
        async with self.db.transaction():
            candidates = await self.db.fetch(
                "SELECT id, profile_id FROM runs WHERE status = 'queued' ORDER BY created_at, id")
            for candidate in candidates:
                if await self.db.fetchrow(
                        "SELECT 1 AS fenced FROM profile_deletions WHERE profile_id = ? LIMIT 1",
                        candidate["profile_id"]):
                    continue
                async with self.profile_guard(candidate["profile_id"], skip_locked=True) as available:
                    if not available:
                        continue
                    run_row = await self._queued_run_row(candidate["id"], candidate["profile_id"])
                    if run_row is None:
                        continue
                    profile_row = await self.db.fetchrow(
                        "SELECT sandbox FROM profiles WHERE id = ?", candidate["profile_id"])
                    if profile_row is None:
                        continue
                    sandbox = json.loads(profile_row["sandbox"])
                    sandbox_row = await self.db.fetchrow(
                        f"SELECT * FROM sandboxes WHERE {_SANDBOX_IDENTITY}",
                        deployment, candidate["profile_id"])
                    now = await self.coordination_now()
                    if sandbox and daemon_id is None:
                        continue
                    if sandbox:
                        # A record naming another daemon is a configuration/migration conflict, never an absence.
                        # It only matters to a sandboxed profile; chat-only runs need no Docker coordination.
                        if sandbox_row is not None and sandbox_row["daemon_id"] != daemon_id:
                            continue
                        if sandbox_row is not None and sandbox_row["state"] in ("draining", "removing", "blocked"):
                            continue
                        if sandbox_row is not None and sandbox_row["state"] == "ready":
                            if (sandbox_row["policy_digest"] != _effective_digest(sandbox)
                                    or sandbox_row["helper_protocol"] not in (None, HELPER_PROTOCOL_VERSION)
                                    or (helper_digest is not None
                                        and sandbox_row["helper_digest"] not in (None, helper_digest))):
                                continue
                        preparing = await self.db.fetchrow(
                            "SELECT id FROM sandbox_uses WHERE deployment = ? AND profile_id = ? "
                            "AND state = 'preparing' LIMIT 1", deployment, candidate["profile_id"])
                        if preparing is not None:
                            continue
                        # An expired owner must not slip a new claim through between lease expiry and the
                        # maintenance scan that will orphan it; recovery owns that mutation.
                        expired = await self.db.fetchrow(
                            "SELECT id FROM sandbox_uses WHERE deployment = ? AND profile_id = ? "
                            "AND state IN ('preparing', 'active', 'cleaning') AND lease_expires_at < ? LIMIT 1",
                            deployment, candidate["profile_id"], now)
                        if expired is not None:
                            continue
                        operations = await self.db.fetch(
                            "SELECT kind, generation, outcome, authority FROM sandbox_operations "
                            "WHERE deployment = ? AND profile_id = ? AND authority = 'current' ORDER BY id",
                            deployment, candidate["profile_id"])
                        if _has_preparation_in_flight(sandbox_row, operations):
                            continue
                    expires_at = now + lease_seconds
                    updated = await self.db.fetchrow(
                        "UPDATE runs SET status = 'running', lease_owner = ?, lease_expires_at = ?, "
                        "started_at = ? WHERE id = ? AND status = 'queued' RETURNING *",
                        owner, expires_at, self.clock(), run_row["id"])
                    if updated is None:
                        continue
                    use = None
                    if sandbox:
                        use = await self._reserve_usage(
                            updated, sandbox, sandbox_row, owner, deployment, daemon_id, now, expires_at,
                            helper_digest)
                    return RunClaim(_run(updated), use, LeaseStamp(now, expires_at))
            return None

    async def _queued_run_row(self, run_id: str, profile_id: str) -> dict | None:
        lock = " FOR UPDATE" if self.db.dialect == "postgres" else ""
        return await self.db.fetchrow(
            f"SELECT * FROM runs WHERE id = ? AND profile_id = ? AND status = 'queued'{lock}",
            run_id, profile_id)

    async def _reserve_usage(self, run_row: dict, sandbox: dict, sandbox_row: dict | None, owner: str,
                             deployment: str, daemon_id: str, now: float, expires_at: float,
                             helper_digest: str | None) -> UseHandle:
        digest = _effective_digest(sandbox)
        ready = (sandbox_row is not None and sandbox_row["state"] == "ready"
                 and sandbox_row["daemon_id"] == daemon_id and digest is not None
                 and sandbox_row["policy_digest"] == digest
                 and (sandbox_row["helper_protocol"] is None
                      or sandbox_row["helper_protocol"] == HELPER_PROTOCOL_VERSION)
                 and (helper_digest is None or sandbox_row["helper_digest"] in (None, helper_digest))
                 and sandbox_row["generation_token"] is not None
                 and sandbox_row["container_id"] is not None and sandbox_row["container_name"] is not None)
        usage_id = "use_" + uuid.uuid4().hex
        token = uuid.uuid4().hex
        state = "active" if ready else "preparing"
        generation = sandbox_row["generation"] if ready else None
        generation_token = sandbox_row["generation_token"] if ready else None
        exact_id = sandbox_row["container_id"] if ready else None
        container_name = sandbox_row["container_name"] if ready else None
        await self.db.execute(
            "INSERT INTO sandbox_uses (id, token, deployment, profile_id, daemon_id, run_id, session_id, owner, "
            "state, generation, generation_token, container_id, container_name, create_operation_id, heartbeat_at, "
            "lease_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            usage_id, token, deployment, run_row["profile_id"], daemon_id, run_row["id"], run_row["session_id"],
            owner, state, generation, generation_token, exact_id, container_name, now, expires_at)
        key = SandboxKey(deployment, run_row["profile_id"], daemon_id)
        return UseHandle(key, usage_id, token, owner)

    async def renew_claim(self, claim: RunClaim, lease_seconds: float) -> LeaseStamp | None:
        """Renew a running/stopping run's lease for its matching owner, using database time."""
        owner = claim.run.lease_owner
        if owner is None:
            return None
        async with self.db.transaction():
            locked = await self.db.fetchrow(
                "SELECT id, lease_expires_at FROM runs WHERE id = ? AND lease_owner = ? "
                f"AND status IN ('running', 'stopping'){self._lock_suffix()}", claim.run.id, owner)
            if locked is None:
                return None
            now = await self.coordination_now()
            if locked["lease_expires_at"] is None or locked["lease_expires_at"] < now:
                return None
            expires_at = now + lease_seconds
            row = await self.db.fetchrow(
                "UPDATE runs SET lease_expires_at = ? WHERE id = ? AND lease_owner = ? "
                "AND status IN ('running', 'stopping') AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at >= ? RETURNING id", expires_at, claim.run.id, owner, now)
            return LeaseStamp(now, expires_at) if row is not None else None

    async def renew_usage(self, use: UseHandle, lease_seconds: float) -> LeaseStamp | None:
        """Renew an unexpired, matching usage; never resurrect an expired, orphaned or released row."""
        async with self.db.transaction():
            locked = await self.db.fetchrow(
                "SELECT id FROM sandbox_uses WHERE id = ? AND token = ? AND owner = ? AND deployment = ? "
                "AND profile_id = ? AND daemon_id = ? AND state IN ('preparing', 'active', 'cleaning')"
                f"{self._lock_suffix()}", use.usage_id, use.token, use.owner, use.key.deployment,
                use.key.profile_id, use.key.daemon_id)
            if locked is None:
                return None
            now = await self.coordination_now()
            expires_at = now + lease_seconds
            row = await self.db.fetchrow(
                "UPDATE sandbox_uses SET heartbeat_at = ?, lease_expires_at = ? WHERE id = ? AND token = ? "
                "AND owner = ? AND deployment = ? AND profile_id = ? AND daemon_id = ? "
                "AND state IN ('preparing', 'active', 'cleaning') AND lease_expires_at >= ? RETURNING id",
                now, expires_at, use.usage_id, use.token, use.owner, use.key.deployment, use.key.profile_id,
                use.key.daemon_id, now)
            return LeaseStamp(now, expires_at) if row is not None else None

    # coordinated executions

    async def begin_exec(self, sandbox: BoundSandbox, exec_id: str) -> ExecTicket:
        """Admit one execution to a published generation before its Docker client starts.

        The usage, the sandbox row and the exact binding are rechecked under the profile guard; the pending row is
        committed before the caller may dispatch. A duplicate exec ID is rejected rather than rebound.
        """
        use = sandbox.use
        binding = sandbox.binding
        async with self.profile_guard(use.key.profile_id) as available:
            if not available:
                raise SandboxBusy(f"profile {use.key.profile_id!r} is unavailable")
            lock = self._lock_suffix()
            now = await self.coordination_now()
            sandbox_row = await self._lock_sandbox(use.key, lock)
            usage = await self.db.fetchrow(
                f"SELECT * FROM sandbox_uses WHERE {_USAGE_IDENTITY}{lock}", use.usage_id, use.token, use.owner,
                use.key.deployment, use.key.profile_id, use.key.daemon_id)
            if (usage is None or usage["state"] != "active" or usage["lease_expires_at"] < now
                    or not _usage_matches_binding(usage, binding)):
                raise SandboxLeaseLost(f"usage {use.usage_id!r} is not a valid execution owner")
            if (sandbox_row is None or sandbox_row["daemon_id"] != use.key.daemon_id
                    or sandbox_row["state"] not in ("ready", "draining")
                    or not _usage_matches_binding(sandbox_row, binding)):
                raise SandboxBusy("the sandbox is not the published generation of this usage")
            existing = await self.db.fetchrow(
                "SELECT id FROM sandbox_execs WHERE id = ?", exec_id)
            if existing is not None:
                raise SandboxBusy(f"exec {exec_id!r} already exists")
            control_token = uuid.uuid4().hex
            try:
                await self.db.execute(
                    "INSERT INTO sandbox_execs (id, control_token, usage_id, usage_token, deployment, "
                    "profile_id, daemon_id, run_id, session_id, generation, generation_token, container_id, "
                    "state, protocol, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)",
                    exec_id, control_token, use.usage_id, use.token, use.key.deployment, use.key.profile_id,
                    use.key.daemon_id, usage["run_id"], usage["session_id"], binding.generation,
                    binding.generation_token, binding.container_id, now)
            except UniqueViolation as exc:
                raise SandboxBusy(f"exec {exec_id!r} already exists") from exc
            return ExecTicket(exec_id, use, binding, control_token)

    async def resolve_exec(self, sandbox: BoundSandbox, exec_id: str) -> ExecTicket:
        """Resolve the original durable control ticket under current usage and generation authority.

        Cleaning owners may finish their executions; expired, revoked, uncertain and retired identities cannot
        acquire signal authority. No cached ticket or reusable container name substitutes for these checks.
        """
        use, binding = sandbox.use, sandbox.binding
        async with self.coordination_guard(use.key.profile_id):
            lock = self._lock_suffix()
            current = await self._lock_sandbox(use.key, lock)
            usage = await self.db.fetchrow(
                f"SELECT * FROM sandbox_uses WHERE {_USAGE_IDENTITY}{lock}", use.usage_id, use.token, use.owner,
                use.key.deployment, use.key.profile_id, use.key.daemon_id)
            now = await self.coordination_now()
            if (usage is None or usage["state"] not in ("active", "cleaning")
                    or usage["lease_expires_at"] < now or not _usage_matches_binding(usage, binding)
                    or current is None or current["daemon_id"] != use.key.daemon_id
                    or current["state"] not in ("ready", "draining")
                    or not _usage_matches_binding(current, binding)):
                raise SandboxLeaseLost("the execution no longer has valid usage authority")
            row = await self.db.fetchrow(f"SELECT * FROM sandbox_execs WHERE id = ?{lock}", exec_id)
            if row is None or row["state"] not in ("pending", "running", "finished"):
                raise SandboxLeaseLost("the execution control identity is unavailable")
            ticket = ExecTicket(exec_id, use, binding, row["control_token"])
            if not self._exec_matches_ticket(row, ticket):
                raise SandboxLeaseLost("the execution control binding does not match its usage")
            return ticket

    async def finish_exec(self, ticket: ExecTicket, receipt: CleanupReceipt) -> bool:
        """Record one verified cleanup receipt, returning True only for the finished transition.

        A mismatched receipt never reaches the row. A successful receipt requires a closed control channel, proven
        descendant completion and no uncertainty; anything else marks the exec uncertain and drains the exact
        generation it ran in. The second finish of one exec changes nothing.
        """
        if receipt.exec_id != ticket.exec_id or receipt.control_token != ticket.control_token:
            return False
        success = (receipt.uncertain_reason is None and receipt.control_closed
                   and receipt.descendants_complete)
        async with self.coordination_guard(ticket.use.key.profile_id):
            lock = self._lock_suffix()
            now = await self.coordination_now()
            await self._lock_sandbox(ticket.use.key, lock)
            row = await self.db.fetchrow(
                f"SELECT * FROM sandbox_execs WHERE id = ? AND control_token = ?{lock}",
                ticket.exec_id, ticket.control_token)
            if row is None or not self._exec_matches_ticket(row, ticket):
                return False
            if success:
                updated = await self.db.fetchrow(
                    "UPDATE sandbox_execs SET state = 'finished', finished_at = ?, cleanup_outcome = 'complete', "
                    "uncertainty_reason = NULL, diagnostic = NULL WHERE id = ? AND control_token = ? "
                    "AND state IN ('pending', 'running') RETURNING id",
                    now, ticket.exec_id, ticket.control_token)
                return updated is not None
            reason = _bounded(receipt.uncertain_reason or "cleanup_incomplete")
            updated = await self.db.fetchrow(
                "UPDATE sandbox_execs SET state = 'uncertain', finished_at = ?, cleanup_outcome = 'uncertain', "
                "uncertainty_reason = ? WHERE id = ? AND control_token = ? "
                "AND state IN ('pending', 'running') RETURNING id",
                now, reason, ticket.exec_id, ticket.control_token)
            if updated is None:
                return False
            await self._drain_matching_generation(ticket, now)
            return False

    async def mark_exec_uncertain(self, ticket: ExecTicket, reason: str) -> None:
        """Move a pending/running exec to uncertain and drain its exact generation; never downgrade a finish."""
        async with self.coordination_guard(ticket.use.key.profile_id):
            lock = self._lock_suffix()
            now = await self.coordination_now()
            await self._lock_sandbox(ticket.use.key, lock)
            row = await self.db.fetchrow(
                f"SELECT * FROM sandbox_execs WHERE id = ? AND control_token = ?{lock}",
                ticket.exec_id, ticket.control_token)
            if row is None or not self._exec_matches_ticket(row, ticket):
                return
            updated = await self.db.fetchrow(
                "UPDATE sandbox_execs SET state = 'uncertain', finished_at = ?, cleanup_outcome = 'uncertain', "
                "uncertainty_reason = ? WHERE id = ? AND control_token = ? "
                "AND state IN ('pending', 'running') RETURNING id",
                now, _bounded(reason), ticket.exec_id, ticket.control_token)
            if updated is not None:
                await self._drain_matching_generation(ticket, now)

    def _exec_matches_ticket(self, row: dict, ticket: ExecTicket) -> bool:
        binding = ticket.binding
        return (row["usage_id"] == ticket.use.usage_id
                and row["usage_token"] == ticket.use.token
                and row["deployment"] == ticket.use.key.deployment
                and row["profile_id"] == ticket.use.key.profile_id
                and row["daemon_id"] == ticket.use.key.daemon_id
                and row["generation"] == binding.generation
                and row["generation_token"] == binding.generation_token
                and row["container_id"] == binding.container_id)

    async def _drain_matching_generation(self, ticket: ExecTicket, now: float) -> None:
        """Drain only the exact generation the exec ran in; a newer generation is never touched."""
        binding = ticket.binding
        await self.db.execute(
            "UPDATE sandboxes SET state = 'draining', transitioned_at = ? "
            f"WHERE {_SANDBOX_IDENTITY} AND daemon_id = ? AND generation = ? AND generation_token = ? "
            "AND container_id = ? AND state IN ('ready', 'creating')",
            now, ticket.use.key.deployment, ticket.use.key.profile_id, ticket.use.key.daemon_id,
            binding.generation, binding.generation_token, binding.container_id)

    # usage cleanup and release

    async def begin_usage_cleanup(self, use: UseHandle) -> bool:
        """Move a still-valid active usage to cleaning; releases the protective usage only later."""
        async with self.coordination_guard(use.key.profile_id):
            lock = self._lock_suffix()
            now = await self.coordination_now()
            usage = await self.db.fetchrow(
                f"SELECT * FROM sandbox_uses WHERE {_USAGE_IDENTITY}{lock}", use.usage_id, use.token, use.owner,
                use.key.deployment, use.key.profile_id, use.key.daemon_id)
            if usage is None or usage["state"] != "active" or usage["lease_expires_at"] < now:
                return False
            updated = await self.db.fetchrow(
                "UPDATE sandbox_uses SET state = 'cleaning' WHERE id = ? AND token = ? AND owner = ? "
                "AND deployment = ? AND profile_id = ? AND daemon_id = ? AND state = 'active' "
                "AND lease_expires_at >= ? RETURNING id",
                use.usage_id, use.token, use.owner, use.key.deployment, use.key.profile_id, use.key.daemon_id, now)
            return updated is not None

    async def release_usage(self, use: UseHandle) -> bool:
        """Release a cleaning usage, or an orphaned one whose bound generation is already gone.

        A valid cleaning lease is required for a normal release; an orphaned usage is released only after its
        generation disappeared. Unresolved execs block both, and live preparation authority blocks a normal
        release so cancellation cannot strand a planned/dispatched create, install or start.
        """
        async with self.coordination_guard(use.key.profile_id):
            lock = self._lock_suffix()
            now = await self.coordination_now()
            sandbox_row = await self._lock_sandbox(use.key, lock)
            usage = await self.db.fetchrow(
                f"SELECT * FROM sandbox_uses WHERE {_USAGE_IDENTITY}{lock}", use.usage_id, use.token, use.owner,
                use.key.deployment, use.key.profile_id, use.key.daemon_id)
            if usage is None:
                return False
            cleaning = usage["state"] == "cleaning"
            orphaned = usage["state"] == "orphaned"
            if cleaning:
                if usage["lease_expires_at"] < now:
                    return False
            elif orphaned:
                if not self._orphaned_generation_gone(usage, sandbox_row):
                    return False
            else:
                return False
            unresolved = await self.db.fetchrow(
                "SELECT id FROM sandbox_execs WHERE usage_id = ? AND state IN ('pending', 'running', 'uncertain') "
                "LIMIT 1", use.usage_id)
            if unresolved is not None:
                return False
            if cleaning:
                live_preparation = await self.db.fetchrow(
                    "SELECT id FROM sandbox_operations WHERE deployment = ? AND profile_id = ? AND usage_id = ? "
                    "AND authority = 'current' AND kind IN ('create', 'install', 'start') "
                    "AND outcome IN ('planned', 'dispatched') LIMIT 1",
                    use.key.deployment, use.key.profile_id, use.usage_id)
                if live_preparation is not None:
                    return False
            reason = "normal_release" if cleaning else "generation_removed"
            updated = await self.db.fetchrow(
                "UPDATE sandbox_uses SET state = 'released', released_at = ?, release_reason = ? "
                "WHERE id = ? AND token = ? AND owner = ? AND deployment = ? AND profile_id = ? AND daemon_id = ? "
                "AND state = ? RETURNING id",
                now, reason, use.usage_id, use.token, use.owner, use.key.deployment, use.key.profile_id,
                use.key.daemon_id, usage["state"])
            if updated is not None:
                await self.db.execute(
                    "UPDATE sandboxes SET idle_since = ? WHERE deployment = ? AND profile_id = ? "
                    "AND state = 'ready' AND container_id = ? AND NOT EXISTS "
                    "(SELECT 1 FROM sandbox_uses WHERE deployment = ? AND profile_id = ? AND state != 'released')",
                    now, use.key.deployment, use.key.profile_id, usage["container_id"],
                    use.key.deployment, use.key.profile_id)
            return updated is not None

    def _orphaned_generation_gone(self, usage: dict, sandbox_row: dict | None) -> bool:
        """An orphan may release only when its bound generation is absent or was replaced.

        An unbound orphan carries no generation; section 11.1 is the only transition that may release it.
        """
        if _usage_binding_is_null(usage):
            return False
        if sandbox_row is None:
            return True
        if sandbox_row["state"] == "absent" and sandbox_row["container_id"] is None:
            return True
        return (sandbox_row["generation"] != usage["generation"]
                or sandbox_row["generation_token"] != usage["generation_token"])

    # lifecycle intent

    async def begin_lifecycle(self, use: UseHandle, kind: str, *, target: SandboxBinding | None,
                              create_operation_id: str | None,
                              lease_seconds: float) -> LifecycleAttempt:
        """Persist one create/install/start/remove intent under the profile guard.

        Locks are taken in the design's order (profile, sandbox, operations, usage). Identity, state and lease are
        rechecked inside the guard; a lost usage raises `SandboxLeaseLost`, a state conflict `SandboxBusy`.
        """
        if kind not in ("create", "install", "start", "remove"):
            raise ValueError(f"unknown lifecycle kind {kind!r}")
        if kind in ("install", "start", "remove") and target is None:
            raise ValueError(f"a {kind} operation needs an exact target")
        async with self.profile_guard(use.key.profile_id) as available:
            if not available:
                raise SandboxBusy(f"profile {use.key.profile_id!r} is unavailable")
            lock = self._lock_suffix()
            sandbox_row = await self.db.fetchrow(
                f"SELECT * FROM sandboxes WHERE {_SANDBOX_IDENTITY}{lock}",
                use.key.deployment, use.key.profile_id)
            operations = await self.db.fetch(
                f"SELECT * FROM sandbox_operations WHERE deployment = ? AND profile_id = ? ORDER BY id{lock}",
                use.key.deployment, use.key.profile_id)
            usage = await self.db.fetchrow(
                "SELECT * FROM sandbox_uses WHERE id = ? AND token = ? AND owner = ? AND deployment = ? "
                f"AND profile_id = ? AND daemon_id = ?{lock}", use.usage_id, use.token, use.owner,
                use.key.deployment, use.key.profile_id, use.key.daemon_id)
            now = await self.coordination_now()
            allowed_states = _RENEWABLE_USAGE_STATES if kind == "remove" else ("preparing", "active")
            if (usage is None or usage["state"] not in allowed_states
                    or usage["lease_expires_at"] < now):
                raise SandboxLeaseLost(f"usage {use.usage_id!r} is not valid")
            if sandbox_row is not None and sandbox_row["daemon_id"] != use.key.daemon_id:
                raise SandboxBusy("the recorded sandbox belongs to a different Docker daemon")
            if kind == "create":
                return await self._begin_create(use, usage, operations, now, lease_seconds)
            if kind in ("install", "start"):
                return await self._begin_preparation_step(
                    use, usage, sandbox_row, operations, target, create_operation_id, kind, now, lease_seconds)
            return await self._begin_remove(
                use, usage, sandbox_row, operations, target, create_operation_id, now, lease_seconds)

    async def _begin_create(self, use: UseHandle, usage: dict, operations: list[dict], now: float,
                            lease_seconds: float) -> LifecycleAttempt:
        if usage["state"] != "preparing" or any(
                usage[column] is not None
                for column in ("generation", "generation_token", "container_id", "container_name")):
            raise SandboxBusy("a create needs an unbound preparing usage")
        if any(operation["kind"] in _PREPARATION_KINDS and operation["authority"] == "current"
               and operation["outcome"] != "definite_failure" and operation["usage_id"] == usage["id"]
               for operation in operations):
            raise SandboxBusy("this usage already owns a preparation")
        await self.db.execute(
            "INSERT INTO sandboxes (deployment, profile_id, daemon_id, generation, generation_token, state, "
            "transitioned_at) VALUES (?, ?, ?, 0, NULL, 'absent', ?) "
            "ON CONFLICT (deployment, profile_id) DO NOTHING",
            use.key.deployment, use.key.profile_id, use.key.daemon_id, now)
        sandbox_row = await self.db.fetchrow(
            f"SELECT * FROM sandboxes WHERE {_SANDBOX_IDENTITY}", use.key.deployment, use.key.profile_id)
        if sandbox_row["state"] != "absent" or sandbox_row["container_id"] is not None:
            raise SandboxBusy("the sandbox already has a current target or operation")
        generation = int(sandbox_row["generation"]) + 1
        generation_token = uuid.uuid4().hex
        operation_row = await self._insert_operation(
            use, usage, kind="create", generation=generation, generation_token=generation_token,
            intended_name=_intended_name(use.key.profile_id, generation_token),
            original_create_operation_id=None, container_id=None, now=now, lease_seconds=lease_seconds)
        linked = await self.db.fetchrow(
            "UPDATE sandbox_uses SET create_operation_id = ? WHERE id = ? AND state = 'preparing' "
            "AND generation IS NULL AND container_id IS NULL RETURNING id", operation_row["id"], usage["id"])
        if linked is None:
            raise SandboxLeaseLost("the preparing usage changed while allocating a generation")
        updated = await self.db.fetchrow(
            "UPDATE sandboxes SET state = 'creating', generation = ?, generation_token = ?, container_id = NULL, "
            "container_name = NULL, current_operation_id = ?, transitioned_at = ?, policy_digest = NULL, "
            "error_code = NULL, error_message = NULL "
            "WHERE deployment = ? AND profile_id = ? AND generation = ? AND state = 'absent' "
            "RETURNING deployment",
            generation, generation_token, operation_row["id"], now, use.key.deployment, use.key.profile_id,
            sandbox_row["generation"])
        if updated is None:
            raise SandboxBusy("the sandbox changed while allocating a generation")
        return _attempt(operation_row)

    async def _begin_preparation_step(self, use: UseHandle, usage: dict, sandbox_row: dict | None,
                                      operations: list[dict], target: SandboxBinding, create_operation_id: str | None,
                                      kind: str, now: float, lease_seconds: float) -> LifecycleAttempt:
        if sandbox_row is None:
            raise SandboxBusy("no sandbox record exists")
        if sandbox_row["state"] not in ("creating", "ready"):
            raise SandboxBusy(f"the sandbox is {sandbox_row['state']}")
        if (sandbox_row["generation"] != target.generation
                or sandbox_row["generation_token"] != target.generation_token):
            raise SandboxBusy("the target is not the sandbox's current generation")
        # Only the owner of the immediately preceding step may continue preparation. Earlier completed
        # steps need not stay leased while a later, independently heartbeating operation is in flight.
        previous = next((row for row in operations if row["id"] == sandbox_row["current_operation_id"]), None)
        if (previous is not None and previous["kind"] in _PREPARATION_KINDS
                and (previous["owner"] != use.owner or previous["usage_token"] != use.token
                     or previous["lease_expires_at"] is None or previous["lease_expires_at"] < now)):
            raise SandboxLeaseLost("the previous preparation owner lost its lease")
        create_id = create_operation_id or usage["create_operation_id"]
        create = next((operation for operation in operations if operation["id"] == create_id), None)
        if create is None:
            raise SandboxBusy("no original create operation is recorded")
        if (create["kind"] != "create" or create["authority"] != "current"
                or create["outcome"] != "succeeded" or create["usage_id"] != usage["id"]
                or create["generation"] != target.generation
                or create["generation_token"] != target.generation_token
                or create["container_id"] != target.container_id):
            raise SandboxBusy("the original create does not authorize this target")
        bound = (usage["generation"], usage["generation_token"], usage["container_id"], usage["container_name"])
        if bound != (None, None, None, None) and bound != (
                target.generation, target.generation_token, target.container_id, target.container_name):
            raise SandboxBusy("the usage is bound to a different generation")
        operation_row = await self._insert_operation(
            use, usage, kind=kind, generation=target.generation, generation_token=target.generation_token,
            intended_name=create["intended_name"], original_create_operation_id=create["id"],
            container_id=target.container_id, now=now, lease_seconds=lease_seconds)
        updated = await self.db.fetchrow(
            "UPDATE sandboxes SET state = CASE WHEN state = 'ready' THEN state ELSE 'creating' END, "
            "current_operation_id = ?, transitioned_at = ? WHERE deployment = ? AND profile_id = ? "
            "AND generation = ? RETURNING deployment", operation_row["id"], now, use.key.deployment,
            use.key.profile_id, target.generation)
        if updated is None:
            raise SandboxBusy("the sandbox changed while recording the preparation step")
        return _attempt(operation_row)

    async def _begin_remove(self, use: UseHandle, usage: dict, sandbox_row: dict | None,
                            operations: list[dict], target: SandboxBinding, create_operation_id: str | None,
                            now: float, lease_seconds: float) -> LifecycleAttempt:
        """Record removal only against a target this profile already recorded.

        A current generation is identified by the sandbox row; an unpublished current generation is authorized by a
        create attempt, including a retired or uncertain create whose exact observed ID is retained as evidence. A
        historical generation needs a matching operation row. Recording removal also retires every current
        preparation attempt for that exact target, so no planned start survives it.
        """
        if not target.container_id:
            raise ValueError("a remove operation needs an exact container ID")
        if not target.generation_token:
            raise ValueError("a remove operation needs a generation token")
        current = (sandbox_row is not None
                   and sandbox_row["generation"] == target.generation
                   and sandbox_row["generation_token"] == target.generation_token)
        if current:
            if sandbox_row["state"] == "absent":
                raise SandboxBusy("the sandbox is absent")
            recorded_id = sandbox_row["container_id"]
            if recorded_id is not None and recorded_id != target.container_id:
                raise SandboxBusy("the target is not the sandbox's recorded container")
            if recorded_id is None and not any(
                    operation["kind"] == "create" and operation["daemon_id"] == use.key.daemon_id
                    and operation["generation"] == target.generation
                    and operation["generation_token"] == target.generation_token
                    and operation["container_id"] == target.container_id
                    for operation in operations):
                # Recovery removes an observed exact ID after retiring its create, so a retired or uncertain
                # create with the recorded ID still authorizes this target; the evidence is never weakened.
                raise SandboxBusy("no recorded create authorizes this removal target")
        else:
            same_target = [operation for operation in operations
                           if operation["daemon_id"] == use.key.daemon_id
                           and operation["container_id"] == target.container_id]
            matching = [operation for operation in same_target
                        if operation["generation"] == target.generation
                        and operation["generation_token"] == target.generation_token]
            if not matching or len(matching) != len(same_target):
                raise SandboxBusy("no recorded operation authorizes this removal target")
        await self.db.execute(
            "UPDATE sandbox_operations SET authority = 'retired', retired_at = ?, "
            "retirement_reason = 'generation_removal', updated_at = ? "
            "WHERE deployment = ? AND profile_id = ? AND daemon_id = ? AND authority = 'current' "
            "AND kind IN ('create', 'install', 'start') AND generation = ? AND generation_token = ? "
            "AND (container_id = ? OR (kind = 'create' AND container_id IS NULL))",
            now, now, use.key.deployment, use.key.profile_id, use.key.daemon_id, target.generation,
            target.generation_token, target.container_id)
        others = await self.db.fetchrow(
            "SELECT COUNT(*) AS n FROM sandbox_uses WHERE deployment = ? AND profile_id = ? AND id <> ? "
            "AND generation = ? AND state IN ('preparing', 'active', 'cleaning')",
            use.key.deployment, use.key.profile_id, usage["id"], target.generation)
        operation_row = await self._insert_operation(
            use, usage, kind="remove", generation=target.generation,
            generation_token=target.generation_token, intended_name=target.container_name,
            original_create_operation_id=create_operation_id, container_id=target.container_id, now=now,
            lease_seconds=lease_seconds)
        if current:
            state = "draining" if int(others["n"]) > 0 else "removing"
            updated = await self.db.fetchrow(
                "UPDATE sandboxes SET state = ?, current_operation_id = ?, transitioned_at = ? "
                "WHERE deployment = ? AND profile_id = ? AND generation = ? RETURNING deployment",
                state, operation_row["id"], now, use.key.deployment, use.key.profile_id, target.generation)
            if updated is None:
                raise SandboxBusy("the sandbox changed while recording removal intent")
        return _attempt(operation_row)

    async def _insert_operation(self, use: UseHandle, usage: dict, *, kind: str, generation: int,
                                generation_token: str, intended_name: str | None,
                                original_create_operation_id: str | None, container_id: str | None, now: float,
                                lease_seconds: float) -> dict:
        try:
            return await self.db.fetchrow(
                "INSERT INTO sandbox_operations (id, kind, deployment, profile_id, daemon_id, run_id, session_id, "
                "generation, generation_token, intended_name, original_create_operation_id, usage_id, usage_token, "
                "container_id, owner, owner_token, lease_expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                "op_" + uuid.uuid4().hex, kind, use.key.deployment, use.key.profile_id, use.key.daemon_id,
                usage["run_id"], usage["session_id"], generation, generation_token, intended_name,
                original_create_operation_id, usage["id"], usage["token"], container_id, use.owner,
                uuid.uuid4().hex, now + lease_seconds, now, now)
        except UniqueViolation as exc:
            raise SandboxBusy("another lifecycle operation already owns this identity") from exc

    async def authorize_dispatch(self, attempt: LifecycleAttempt) -> bool:
        """Move one planned attempt to dispatched exactly once, under its owner token and unexpired lease."""
        async with self.db.transaction():
            locked = await self.db.fetchrow(
                "SELECT id FROM sandbox_operations WHERE id = ? AND owner_token = ? AND authority = 'current' "
                f"AND outcome = 'planned'{self._lock_suffix()}", attempt.id, attempt.owner_token)
            if locked is None:
                return False
            now = await self.coordination_now()
            row = await self.db.fetchrow(
                "UPDATE sandbox_operations SET outcome = 'dispatched', updated_at = ? WHERE id = ? "
                "AND owner_token = ? AND authority = 'current' AND outcome = 'planned' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at >= ? RETURNING id",
                now, attempt.id, attempt.owner_token, now)
            return row is not None

    async def renew_operation(self, attempt: LifecycleAttempt, lease_seconds: float) -> LeaseStamp | None:
        """Renew an unexpired, unresolved attempt owned by this token; never resurrect a lapsed lease."""
        async with self.db.transaction():
            locked = await self.db.fetchrow(
                "SELECT id, lease_expires_at FROM sandbox_operations WHERE id = ? AND owner_token = ? "
                "AND authority = 'current' AND outcome IN ('planned', 'dispatched', 'uncertain')"
                f"{self._lock_suffix()}", attempt.id, attempt.owner_token)
            if locked is None:
                return None
            now = await self.coordination_now()
            if locked["lease_expires_at"] is None or locked["lease_expires_at"] < now:
                return None
            expires_at = now + lease_seconds
            row = await self.db.fetchrow(
                "UPDATE sandbox_operations SET lease_expires_at = ?, updated_at = ? WHERE id = ? "
                "AND owner_token = ? AND authority = 'current' "
                "AND outcome IN ('planned', 'dispatched', 'uncertain') "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at >= ? RETURNING id",
                expires_at, now, attempt.id, attempt.owner_token, now)
            return LeaseStamp(now, expires_at) if row is not None else None

    async def record_lifecycle_result(self, attempt: LifecycleAttempt,
                                      outcome: DockerOutcome) -> LifecycleAttempt:
        """Record an external result conditionally.

        An unexpired current dispatched attempt advances its outcome and may attach a newly discovered exact ID.
        Expired owners retain matching evidence without advancing their outcome. A stale or
        retired attempt only ever attaches matching evidence that is still null: it never changes authority or
        outcome, publishes, or touches another generation's pointer.
        """
        discovered = outcome.observation.container_id if outcome.observation is not None else None
        async with self.db.transaction():
            await self.db.fetchrow(
                f"SELECT id FROM sandbox_operations WHERE id = ?{self._lock_suffix()}", attempt.id)
            now = await self.coordination_now()
            # A created container stays unresolved until its cleanup/absence is reconciled; only a definite
            # failure or a completed install/start/remove is resolved by its own result.
            succeeded_create = outcome.state == "succeeded" and attempt.kind == "create"
            resolved_at = None if outcome.state == "uncertain" or succeeded_create else now
            row = await self.db.fetchrow(
                "UPDATE sandbox_operations SET outcome = ?, result_code = NULL, result_message = ?, "
                "resolved_at = ?, "
                "container_id = CASE WHEN container_id IS NULL THEN ? ELSE container_id END, "
                "discovered_container_id = CASE WHEN discovered_container_id IS NULL THEN ? "
                "ELSE discovered_container_id END, updated_at = ? "
                "WHERE id = ? AND owner_token = ? AND authority = 'current' AND outcome = 'dispatched' "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at >= ? "
                "AND (CAST(? AS TEXT) IS NULL OR container_id IS NULL OR container_id = ?) "
                "AND (CAST(? AS TEXT) IS NULL OR discovered_container_id IS NULL "
                "OR discovered_container_id = ?) RETURNING *",
                outcome.state, outcome.diagnostic, resolved_at, discovered, discovered, now, attempt.id,
                attempt.owner_token, now, discovered, discovered, discovered, discovered)
            if row is not None:
                return _attempt(row)
            current = await self.db.fetchrow(
                "SELECT * FROM sandbox_operations WHERE id = ?", attempt.id)
            if current is None:
                raise SandboxLeaseLost(f"operation {attempt.id!r} no longer exists")
            if current["owner_token"] != attempt.owner_token:
                return _attempt(current)
            # Keep the late diagnostic as evidence even without a discovered container; the first recorded
            # message stands, and authority and outcome are never touched here.
            evidence = await self.db.fetchrow(
                "UPDATE sandbox_operations SET "
                "result_message = CASE WHEN result_message IS NULL THEN ? ELSE result_message END, "
                "container_id = CASE WHEN container_id IS NULL THEN ? ELSE container_id END, "
                "discovered_container_id = CASE WHEN discovered_container_id IS NULL THEN ? "
                "ELSE discovered_container_id END, updated_at = ? "
                "WHERE id = ? AND owner_token = ? "
                "AND (CAST(? AS TEXT) IS NULL OR container_id IS NULL OR container_id = ?) "
                "AND (CAST(? AS TEXT) IS NULL OR discovered_container_id IS NULL "
                "OR discovered_container_id = ?) RETURNING *",
                _bounded(outcome.diagnostic), discovered, discovered, now, attempt.id, attempt.owner_token,
                discovered, discovered, discovered, discovered)
            return _attempt(evidence if evidence is not None else current)

    async def publish_generation(self, use: UseHandle, binding: SandboxBinding,
                                 start_operation_id: str, *, start_owner_token: str | None = None) -> BoundSandbox:
        """Publish one verified preparation as the profile's current usable generation.

        Everything is rechecked under the profile guard: the preparing usage still holds its lease and is unbound,
        the sandbox still creates this exact generation, and both the original create and the recorded start are
        current, succeeded attempts against this target. A stale caller publishes nothing; the conditional updates
        and their rollback leave another generation's identity, pointer and valid usages untouched.
        """
        async with self.profile_guard(use.key.profile_id) as available:
            if not available:
                raise SandboxBusy(f"profile {use.key.profile_id!r} is unavailable")
            lock = self._lock_suffix()
            sandbox_row = await self._lock_sandbox(use.key, lock)
            operations = await self._lock_operations(use.key, lock)
            usage = await self.db.fetchrow(
                f"SELECT * FROM sandbox_uses WHERE {_USAGE_IDENTITY}{lock}", use.usage_id, use.token, use.owner,
                use.key.deployment, use.key.profile_id, use.key.daemon_id)
            now = await self.coordination_now()
            if (usage is None or usage["state"] != "preparing" or usage["lease_expires_at"] < now
                    or not _usage_binding_is_null(usage)):
                raise SandboxLeaseLost(f"usage {use.usage_id!r} is not a valid preparation owner")
            if (sandbox_row is None or sandbox_row["daemon_id"] != use.key.daemon_id
                    or sandbox_row["state"] != "creating"
                    or sandbox_row["generation"] != binding.generation
                    or sandbox_row["generation_token"] != binding.generation_token
                    or sandbox_row["container_id"] is not None
                    or sandbox_row["container_name"] is not None):
                raise SandboxBusy("the sandbox is not creating this generation")
            create_id = usage["create_operation_id"]
            create = next((row for row in operations if row["id"] == create_id), None)
            if (create is None or create["kind"] != "create" or create["authority"] != "current"
                    or create["outcome"] != "succeeded" or create["usage_id"] != usage["id"]
                    or create["generation"] != binding.generation
                    or create["generation_token"] != binding.generation_token
                    or create["container_id"] != binding.container_id):
                raise SandboxBusy("the original create does not authorize this generation")
            start = next((row for row in operations if row["id"] == start_operation_id), None)
            if (start is None or start["kind"] != "start" or start["authority"] != "current"
                    or start["outcome"] != "succeeded"
                    or start["original_create_operation_id"] != create["id"]
                    or start["usage_id"] != usage["id"]
                    or start["generation"] != binding.generation
                    or start["generation_token"] != binding.generation_token
                    or start["container_id"] != binding.container_id):
                raise SandboxBusy("the recorded start does not authorize publication")
            if ((start_owner_token is not None and start["owner_token"] != start_owner_token)
                    or start["owner"] != use.owner or start["usage_token"] != use.token
                    or start["lease_expires_at"] is None or start["lease_expires_at"] < now):
                raise SandboxLeaseLost("the start owner lost its lease before publication")
            if start["intended_name"] not in (None, binding.container_name):
                raise SandboxBusy("the recorded start names another container")
            if sandbox_row["current_operation_id"] != start["id"]:
                raise SandboxBusy("another operation is current for this generation")
            if any(row["kind"] in ("install", "start") and row["authority"] == "current"
                   and row["outcome"] in ("planned", "dispatched") for row in operations):
                raise SandboxBusy("another preparation step is still in flight")
            bound_usage = await self.db.fetchrow(
                "UPDATE sandbox_uses SET state = 'active', generation = ?, generation_token = ?, container_id = ?, "
                "container_name = ? WHERE id = ? AND token = ? AND owner = ? AND deployment = ? AND profile_id = ? "
                "AND daemon_id = ? AND state = 'preparing' AND generation IS NULL AND generation_token IS NULL "
                "AND container_id IS NULL AND container_name IS NULL AND lease_expires_at >= ? RETURNING id",
                binding.generation, binding.generation_token, binding.container_id, binding.container_name,
                use.usage_id, use.token, use.owner, use.key.deployment, use.key.profile_id, use.key.daemon_id, now)
            if bound_usage is None:
                raise SandboxLeaseLost(f"usage {use.usage_id!r} changed during publication")
            ready = await self.db.fetchrow(
                "UPDATE sandboxes SET state = 'ready', container_id = ?, container_name = ?, policy_digest = ?, "
                "helper_protocol = ?, helper_digest = ?, current_operation_id = NULL, idle_since = NULL, "
                "error_code = NULL, error_message = NULL, transitioned_at = ? "
                "WHERE deployment = ? AND profile_id = ? AND daemon_id = ? AND generation = ? "
                "AND generation_token = ? AND state = 'creating' AND container_id IS NULL "
                "AND current_operation_id = ? RETURNING deployment",
                binding.container_id, binding.container_name, binding.policy.digest(), HELPER_PROTOCOL_VERSION,
                binding.helper_digest, now, use.key.deployment, use.key.profile_id, use.key.daemon_id,
                binding.generation, binding.generation_token, start["id"])
            if ready is None:
                raise SandboxBusy("the sandbox changed during publication")
            return BoundSandbox(
                profile_id=use.key.profile_id, name=binding.container_id, policy=binding.policy, use=use,
                binding=binding)

    # unbound preparation retirement

    async def retire_unbound_preparation(self, use: UseHandle, *, abandon: bool = False) -> bool:
        """Release a never-published preparation under design section 11.1.

        The guard tolerates a deleted profile. Every linked attempt must prove that nothing executable was
        authorized; unresolved create evidence may remain as a tombstone. The usage token is revoked and all
        current preparation authority retired in one transaction, or nothing changes.
        """
        async with self.coordination_guard(use.key.profile_id):
            lock = self._lock_suffix()
            now = await self.coordination_now()
            sandbox_row = await self._lock_sandbox(use.key, lock)
            operations = await self._lock_operations(use.key, lock)
            usages = await self._lock_usages(use.key, lock)
            execs = await self._lock_execs(use.key, lock)
            usage = next((row for row in usages if row["id"] == use.usage_id), None)
            if not self._usage_handle_matches(usage, use):
                return False
            return await self._retire_unbound_rows(
                use, usage, sandbox_row, operations, usages, execs, now, abandon)

    def _usage_handle_matches(self, row: dict | None, use: UseHandle) -> bool:
        return (row is not None and row["id"] == use.usage_id and row["token"] == use.token
                and row["owner"] == use.owner and row["deployment"] == use.key.deployment
                and row["profile_id"] == use.key.profile_id and row["daemon_id"] == use.key.daemon_id)

    async def _retire_unbound_rows(self, use: UseHandle, usage: dict, sandbox_row: dict | None,
                                   operations: list[dict], usages: list[dict], execs: list[dict], now: float,
                                   abandon: bool) -> bool:
        if usage["state"] not in ("preparing", "orphaned"):
            return False
        if usage["lease_expires_at"] >= now and not abandon:
            return False
        create_id = usage["create_operation_id"]
        create_op = next((row for row in operations if row["id"] == create_id), None)
        if _unbound_retirement_blocker(usage, create_op, operations, execs) is not None:
            return False
        # Undispatched intent cannot be proven undelivered, so cancel it before retiring authority.
        await self.db.execute(
            "UPDATE sandbox_operations SET outcome = 'uncertain', updated_at = ? "
            "WHERE deployment = ? AND profile_id = ? AND authority = 'current' AND outcome = 'planned' "
            "AND kind IN ('create', 'install', 'start') AND (usage_id = ? OR original_create_operation_id = ?)",
            now, use.key.deployment, use.key.profile_id, usage["id"], create_id)
        retired = await self.db.fetch(
            "UPDATE sandbox_operations SET authority = 'retired', retired_at = ?, "
            "retirement_reason = 'preparation_retired', updated_at = ? "
            "WHERE deployment = ? AND profile_id = ? AND authority = 'current' "
            "AND kind IN ('create', 'install', 'start') AND (usage_id = ? OR original_create_operation_id = ?) "
            "RETURNING id",
            now, now, use.key.deployment, use.key.profile_id, usage["id"], create_id)
        released = await self.db.fetchrow(
            "UPDATE sandbox_uses SET state = 'released', released_at = ?, "
            "release_reason = 'preparation_retired_without_execution' "
            "WHERE id = ? AND token = ? AND owner = ? AND deployment = ? AND profile_id = ? AND daemon_id = ? "
            "AND state IN ('preparing', 'orphaned') RETURNING id",
            now, use.usage_id, use.token, use.owner, use.key.deployment, use.key.profile_id, use.key.daemon_id)
        if released is None:
            # Roll back the retirements: a failed call must not strand authority it did not revoke.
            raise SandboxLeaseLost(f"usage {use.usage_id!r} changed during retirement")
        retired_ids = [row["id"] for row in retired]
        pointer_cleared = False
        if sandbox_row is not None and sandbox_row["current_operation_id"] in retired_ids:
            cleared = await self.db.fetchrow(
                "UPDATE sandboxes SET current_operation_id = NULL, transitioned_at = ? "
                "WHERE deployment = ? AND profile_id = ? AND current_operation_id = ? RETURNING deployment",
                now, use.key.deployment, use.key.profile_id, sandbox_row["current_operation_id"])
            pointer_cleared = cleared is not None
        await self._maybe_mark_absent(use.key, usage, create_op, now, pointer_cleared)
        return True

    async def _maybe_mark_absent(self, key: SandboxKey, usage: dict, create_op: dict | None, now: float,
                                 pointer_cleared: bool) -> None:
        """Move an empty sandbox to absent without erasing another generation or usage's evidence."""
        sandbox_row = await self.db.fetchrow(
            f"SELECT * FROM sandboxes WHERE {_SANDBOX_IDENTITY}", key.deployment, key.profile_id)
        if sandbox_row is None or sandbox_row["state"] == "absent" or sandbox_row["container_id"] is not None:
            return
        our_token = create_op["generation_token"] if create_op is not None else None
        if not pointer_cleared and not (
                (our_token is not None and sandbox_row["generation_token"] == our_token)
                or (our_token is None and sandbox_row["generation_token"] is None)):
            return
        known_target = await self.db.fetchrow(
            "SELECT id FROM sandbox_operations WHERE deployment = ? AND profile_id = ? "
            "AND (container_id IS NOT NULL OR discovered_container_id IS NOT NULL) "
            "AND confirmed_absent_at IS NULL AND generation_token = ? LIMIT 1",
            key.deployment, key.profile_id, our_token)
        if known_target is not None:
            return
        other_usage = await self.db.fetchrow(
            "SELECT id FROM sandbox_uses WHERE deployment = ? AND profile_id = ? "
            "AND state IN ('preparing', 'active', 'cleaning', 'orphaned') LIMIT 1",
            key.deployment, key.profile_id)
        if other_usage is not None:
            return
        live_operation = await self.db.fetchrow(
            "SELECT id FROM sandbox_operations WHERE deployment = ? AND profile_id = ? AND authority = 'current' "
            "AND kind IN ('install', 'start', 'remove') AND outcome IN ('planned', 'dispatched', 'uncertain') "
            "LIMIT 1", key.deployment, key.profile_id)
        if live_operation is not None:
            return
        await self.db.fetchrow(
            "UPDATE sandboxes SET state = 'absent', generation_token = NULL, container_id = NULL, "
            "container_name = NULL, policy_digest = NULL, helper_protocol = NULL, helper_digest = NULL, "
            "error_code = NULL, error_message = NULL, current_operation_id = NULL, idle_since = NULL, "
            "transitioned_at = ? WHERE deployment = ? AND profile_id = ? AND generation = ? "
            "AND state <> 'absent' RETURNING deployment",
            now, key.deployment, key.profile_id, sandbox_row["generation"])

    # recovery planning and commit

    async def plan_recovery(self, key: SandboxKey, *, owner: str,
                            lease_seconds: float,
                            deletion_authority: ProfileDeletionAuthority | None = None) -> list[RecoveryAction]:
        """One guarded recovery pass; returns work for external dispatch and commit.

        Mutations: expired unreleased usages become `orphaned` and a matching `ready`/`creating` generation moves to
        `draining`; a triggered removal retires the generation's preparation authority, either takes over an expired
        remove lease or inserts a fresh current remove, sets the sandbox pointer, and moves it to `removing`; a
        blocked sandbox whose exact remove failed definitively has that attempt retired and is re-armed with a new
        remove. `plan_recovery` re-arms on every pass, so retry pacing belongs to the caller. The returned actions
        are dispatched and committed externally through `commit_recovery`. The guard and locks work after the
        profile row was deleted.
        """
        async with self.coordination_guard(key.profile_id, skip_locked=deletion_authority is None) as available:
            if not available:
                return []
            if deletion_authority is not None:
                await self._assert_deletion_authority(key.profile_id, deletion_authority)
            lock = self._lock_suffix()
            now = await self.coordination_now()
            sandbox_row = await self._lock_sandbox(key, lock)
            operations = await self._lock_operations(key, lock)
            usages = await self._lock_usages(key, lock)
            execs = await self._lock_execs(key, lock)
            for usage in usages:
                if usage["state"] not in _UNRELEASED_USAGE_STATES or usage["lease_expires_at"] >= now:
                    continue
                orphaned = await self.db.fetchrow(
                    "UPDATE sandbox_uses SET state = 'orphaned' WHERE id = ? AND state = ? "
                    "AND lease_expires_at < ? RETURNING id", usage["id"], usage["state"], now)
                if orphaned is None:
                    continue
                usage["state"] = "orphaned"
                if sandbox_row is not None and sandbox_row["state"] in ("ready", "creating") and (
                        self._usage_matches_generation(usage, sandbox_row, operations)):
                    drained = await self.db.fetchrow(
                        "UPDATE sandboxes SET state = 'draining', transitioned_at = ? "
                        "WHERE deployment = ? AND profile_id = ? AND generation = ? AND generation_token = ? "
                        "AND state IN ('ready', 'creating') RETURNING deployment",
                        now, key.deployment, key.profile_id, sandbox_row["generation"],
                        sandbox_row["generation_token"])
                    if drained is not None:
                        sandbox_row["state"] = "draining"
            actions: list[RecoveryAction] = []
            if sandbox_row is not None:
                action = await self._plan_remove(
                    key, owner, lease_seconds, sandbox_row, operations, usages, execs, now)
                if action is not None:
                    actions.append(action)
                    # The remove retired preparation authority; re-read it so unbound retirement does not
                    # mistake that retirement for live start/install authority in this same pass.
                    operations = await self._lock_operations(key, lock)
            for usage in usages:
                if usage["state"] != "orphaned":
                    continue
                create_id = usage["create_operation_id"]
                create_op = next((row for row in operations if row["id"] == create_id), None)
                if _usage_binding_is_null(usage):
                    if _unbound_retirement_blocker(usage, create_op, operations, execs) is None:
                        actions.append(RecoveryAction(
                            key=key, action="retire_unbound", usage_id=usage["id"], usage_token=usage["token"],
                            generation=usage["generation"], generation_token=usage["generation_token"],
                            reason="unbound_preparation"))
                    continue
                if (self._generation_gone(usage, sandbox_row)
                        and not any(row["usage_id"] == usage["id"] and row["state"] in _UNRESOLVED_EXEC_STATES
                                    for row in execs)):
                    actions.append(RecoveryAction(
                        key=key, action="release_orphan", usage_id=usage["id"], usage_token=usage["token"],
                        generation=usage["generation"], generation_token=usage["generation_token"],
                        reason="generation_removed"))
            return actions

    def _usage_matches_generation(self, usage: dict, sandbox_row: dict, operations: list[dict]) -> bool:
        """True when an expired usage is bound to, or preparing in, the sandbox's current generation."""
        if usage["generation"] is not None:
            return (usage["generation"] == sandbox_row["generation"]
                    and usage["generation_token"] == sandbox_row["generation_token"])
        create_id = usage["create_operation_id"]
        create_op = next((row for row in operations if row["id"] == create_id), None)
        return (create_op is not None and create_op["generation"] == sandbox_row["generation"]
                and create_op["generation_token"] == sandbox_row["generation_token"])

    async def _plan_remove(self, key: SandboxKey, owner: str, lease_seconds: float, sandbox_row: dict,
                           operations: list[dict], usages: list[dict], execs: list[dict],
                           now: float) -> RecoveryAction | None:
        """Plan at most one exact-container removal for a triggered sandbox.

        A removal is triggered by `draining`/`removing`/`blocked`, an orphaned usage or an unresolved exec; a healthy
        idle `ready` generation is left for the coordinator's idle policy. A definitively failed current remove for
        the exact target is retired (`remove_retry`) so a fresh current remove can be recorded; a succeeded one
        means the generation is already gone. Each pass re-arms a retry; pacing is the caller's.
        """
        if sandbox_row["state"] == "absent" or sandbox_row.get("error_code") in _INVENTORY_BLOCK_CODES:
            return None
        if any(row["state"] in _UNRELEASED_USAGE_STATES for row in usages):
            return None
        if not self._recovery_triggered(sandbox_row, operations, usages, execs):
            return None
        target = sandbox_row["container_id"]
        create_op = next((row for row in operations
                          if row["kind"] == "create" and row["generation"] == sandbox_row["generation"]
                          and row["generation_token"] == sandbox_row["generation_token"]
                          and row["discovered_container_id"] is not None
                          and row["confirmed_absent_at"] is None), None)
        if target is None and create_op is None:
            return None
        if target is None:
            target = create_op["discovered_container_id"]
        exact = [row for row in operations
                 if row["kind"] == "remove" and row["authority"] == "current" and row["container_id"] == target]
        live = next((row for row in exact if row["outcome"] in ("planned", "dispatched", "uncertain")
                     and row["lease_expires_at"] is not None and row["lease_expires_at"] >= now), None)
        if live is not None:
            return None
        candidate = next((row for row in exact if row["outcome"] in ("planned", "dispatched", "uncertain")),
                         None)
        if candidate is None and exact:
            previous = exact[0]
            if previous["outcome"] != "definite_failure":
                # A succeeded (or otherwise resolved) current remove has already settled this target.
                return None
            # Retire the failed attempt to free the partial unique index, retaining its outcome and diagnostics,
            # and record a fresh current remove below.
            await self.db.execute(
                "UPDATE sandbox_operations SET authority = 'retired', retired_at = ?, "
                "retirement_reason = 'remove_retry', updated_at = ? "
                "WHERE id = ? AND authority = 'current' AND outcome = 'definite_failure'",
                now, now, previous["id"])
        await self.db.execute(
            "UPDATE sandbox_operations SET authority = 'retired', retired_at = ?, "
            "retirement_reason = 'generation_removal', updated_at = ? "
            "WHERE deployment = ? AND profile_id = ? AND daemon_id = ? AND authority = 'current' "
            "AND kind IN ('create', 'install', 'start') AND generation = ? AND generation_token = ? "
            "AND (container_id = ? OR discovered_container_id = ? OR (kind = 'create' AND container_id IS NULL))",
            now, now, key.deployment, key.profile_id, key.daemon_id, sandbox_row["generation"],
            sandbox_row["generation_token"], target, target)
        if candidate is not None:
            operation_id = candidate["id"]
            owner_token = uuid.uuid4().hex
            await self.db.execute(
                "UPDATE sandbox_operations SET owner = ?, owner_token = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND authority = 'current' AND outcome IN ('planned', 'dispatched', 'uncertain')",
                owner, owner_token, now + lease_seconds, now, operation_id)
        else:
            operation_id = "op_" + uuid.uuid4().hex
            owner_token = uuid.uuid4().hex
            await self.db.execute(
                "INSERT INTO sandbox_operations (id, kind, deployment, profile_id, daemon_id, run_id, session_id, "
                "generation, generation_token, intended_name, original_create_operation_id, usage_id, usage_token, "
                "container_id, owner, owner_token, lease_expires_at, created_at, updated_at) "
                "VALUES (?, 'remove', ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)",
                operation_id, key.deployment, key.profile_id, key.daemon_id, sandbox_row["generation"],
                sandbox_row["generation_token"], sandbox_row["container_name"],
                create_op["id"] if create_op is not None else None, target, owner, owner_token,
                now + lease_seconds, now, now)
        await self.db.execute(
            "UPDATE sandboxes SET state = 'removing', current_operation_id = ?, error_code = NULL, "
            "error_message = NULL, transitioned_at = ? "
            "WHERE deployment = ? AND profile_id = ? AND generation = ? AND generation_token = ?",
            operation_id, now, key.deployment, key.profile_id, sandbox_row["generation"],
            sandbox_row["generation_token"])
        sandbox_row["state"] = "removing"
        sandbox_row["current_operation_id"] = operation_id
        return RecoveryAction(
            key=key, action="remove", operation_id=operation_id, container_id=target,
            generation=sandbox_row["generation"], generation_token=sandbox_row["generation_token"],
            owner_token=owner_token, reason="generation_removal")

    def _recovery_triggered(self, sandbox_row: dict, operations: list[dict], usages: list[dict],
                            execs: list[dict]) -> bool:
        """True when recovery (not the idle reaper) owns this sandbox's removal.

        `creating` is a recovery trigger because an unbound preparation can retire after its create already
        discovered an exact container; the discovered target must still be removed. Only orphans and execs from
        the sandbox's current generation count: a stale row produces its own release/retire action, never removal
        of a healthy current generation.
        """
        if sandbox_row["state"] in ("creating", "draining", "removing", "blocked"):
            return True
        return (any(row["state"] == "orphaned"
                    and self._usage_matches_generation(row, sandbox_row, operations) for row in usages)
                or any(row["state"] in _UNRESOLVED_EXEC_STATES
                       and row["generation"] == sandbox_row["generation"]
                       and row["generation_token"] == sandbox_row["generation_token"] for row in execs))

    def _generation_gone(self, usage: dict, sandbox_row: dict | None) -> bool:
        if sandbox_row is None:
            return True
        if sandbox_row["state"] == "absent" and sandbox_row["container_id"] is None:
            return True
        return (sandbox_row["generation"] != usage["generation"]
                or sandbox_row["generation_token"] != usage["generation_token"])

    async def commit_recovery(self, action: RecoveryAction, outcome: DockerOutcome, *,
                              deletion_authority: ProfileDeletionAuthority | None = None) -> bool:
        """Apply one planned recovery action.

        Removal records its inline outcome under the action's owner token and exact target; retirement and orphan
        release rerun their guarded predicates. Stale or duplicate actions change nothing and return False.
        """
        if action.action == "remove":
            return await self._commit_remove(action, outcome, deletion_authority=deletion_authority)
        if action.action == "retire_unbound":
            return await self._commit_retire_unbound(action, deletion_authority=deletion_authority)
        if action.action == "release_orphan":
            return await self._commit_release_orphan(action, deletion_authority=deletion_authority)
        return False

    async def _commit_remove(self, action: RecoveryAction, outcome: DockerOutcome, *,
                             deletion_authority: ProfileDeletionAuthority | None = None) -> bool:
        async with self.coordination_guard(action.key.profile_id):
            if deletion_authority is not None:
                await self._assert_deletion_authority(action.key.profile_id, deletion_authority)
            lock = self._lock_suffix()
            now = await self.coordination_now()
            sandbox_row = await self._lock_sandbox(action.key, lock)
            operation = await self.db.fetchrow(
                f"SELECT * FROM sandbox_operations WHERE id = ? AND owner_token = ?{lock}",
                action.operation_id, action.owner_token)
            if (operation is None or operation["kind"] != "remove" or operation["authority"] != "current"
                    or operation["outcome"] not in ("dispatched", "uncertain")
                    or operation["container_id"] != action.container_id
                    or operation["generation"] != action.generation
                    or operation["generation_token"] != action.generation_token
                    or operation["deployment"] != action.key.deployment
                    or operation["profile_id"] != action.key.profile_id
                    or operation["daemon_id"] != action.key.daemon_id):
                return False
            if outcome.state == "succeeded":
                await self._commit_remove_success(action, operation, sandbox_row, now)
                return True
            if outcome.state == "definite_failure":
                await self.db.execute(
                    "UPDATE sandbox_operations SET outcome = 'definite_failure', result_message = ?, "
                    "resolved_at = ?, updated_at = ? WHERE id = ? AND owner_token = ? "
                    "AND outcome IN ('dispatched', 'uncertain')",
                    _bounded(outcome.diagnostic), now, now, action.operation_id, action.owner_token)
                if sandbox_row is not None and sandbox_row["generation"] == action.generation and (
                        sandbox_row["generation_token"] == action.generation_token):
                    await self.db.execute(
                        "UPDATE sandboxes SET state = 'blocked', error_code = 'sandbox_remove_failed', "
                        "error_message = ?, transitioned_at = ? WHERE deployment = ? AND profile_id = ? "
                        "AND generation = ? AND generation_token = ?",
                        _bounded(outcome.diagnostic or "container removal failed"), now,
                        action.key.deployment, action.key.profile_id, action.generation,
                        action.generation_token)
                return False
            await self.db.execute(
                "UPDATE sandbox_operations SET outcome = 'uncertain', result_message = ?, resolved_at = NULL, "
                "updated_at = ? WHERE id = ? AND owner_token = ? AND outcome IN ('dispatched', 'uncertain')",
                _bounded(outcome.diagnostic), now, action.operation_id, action.owner_token)
            return False

    async def _commit_remove_success(self, action: RecoveryAction, operation: dict, sandbox_row: dict | None,
                                     now: float) -> None:
        await self.db.execute(
            "UPDATE sandbox_operations SET outcome = 'succeeded', result_message = ?, resolved_at = ?, "
            "confirmed_absent_at = ?, updated_at = ? WHERE id = ? AND owner_token = ? "
            "AND outcome IN ('dispatched', 'uncertain')",
            _bounded("container absence confirmed"), now, now, now, action.operation_id, action.owner_token)
        await self.db.execute(
            "UPDATE sandbox_uses SET state = 'released', released_at = ?, release_reason = 'generation_removed' "
            "WHERE deployment = ? AND profile_id = ? AND state = 'orphaned' AND generation = ? "
            "AND generation_token = ?",
            now, action.key.deployment, action.key.profile_id, action.generation, action.generation_token)
        await self.db.execute(
            "UPDATE sandbox_execs SET state = 'retired', finished_at = ?, cleanup_outcome = 'retired', "
            "uncertainty_reason = 'generation_removed' WHERE deployment = ? AND profile_id = ? "
            "AND generation = ? AND generation_token = ? AND state IN ('pending', 'running', 'uncertain')",
            now, action.key.deployment, action.key.profile_id, action.generation, action.generation_token)
        await self.db.execute(
            "UPDATE sandbox_operations SET confirmed_absent_at = COALESCE(confirmed_absent_at, ?), "
            "resolved_at = COALESCE(resolved_at, ?), updated_at = ? "
            "WHERE deployment = ? AND profile_id = ? AND generation = ? AND generation_token = ? "
            "AND (container_id = ? OR (kind = 'create' AND discovered_container_id = ?))",
            now, now, now, action.key.deployment, action.key.profile_id, action.generation,
            action.generation_token, action.container_id, action.container_id)
        if sandbox_row is not None and sandbox_row["generation"] == action.generation and (
                sandbox_row["generation_token"] == action.generation_token):
            await self.db.execute(
                "UPDATE sandboxes SET state = 'absent', generation_token = NULL, container_id = NULL, "
                "container_name = NULL, policy_digest = NULL, helper_protocol = NULL, helper_digest = NULL, "
                "error_code = NULL, error_message = NULL, current_operation_id = NULL, idle_since = NULL, "
                "transitioned_at = ? WHERE deployment = ? AND profile_id = ? AND generation = ? "
                "AND generation_token = ?",
                now, action.key.deployment, action.key.profile_id, action.generation, action.generation_token)

    async def _commit_retire_unbound(self, action: RecoveryAction, *,
                                    deletion_authority: ProfileDeletionAuthority | None = None) -> bool:
        async with self.coordination_guard(action.key.profile_id):
            if deletion_authority is not None:
                await self._assert_deletion_authority(action.key.profile_id, deletion_authority)
            lock = self._lock_suffix()
            now = await self.coordination_now()
            sandbox_row = await self._lock_sandbox(action.key, lock)
            operations = await self._lock_operations(action.key, lock)
            usages = await self._lock_usages(action.key, lock)
            execs = await self._lock_execs(action.key, lock)
            usage = next((row for row in usages if row["id"] == action.usage_id), None)
            if usage is None or usage["token"] != action.usage_token:
                return False
            use = UseHandle(action.key, usage["id"], usage["token"], usage["owner"])
            return await self._retire_unbound_rows(
                use, usage, sandbox_row, operations, usages, execs, now, False)

    async def _commit_release_orphan(self, action: RecoveryAction, *,
                                    deletion_authority: ProfileDeletionAuthority | None = None) -> bool:
        async with self.coordination_guard(action.key.profile_id):
            if deletion_authority is not None:
                await self._assert_deletion_authority(action.key.profile_id, deletion_authority)
            lock = self._lock_suffix()
            now = await self.coordination_now()
            sandbox_row = await self._lock_sandbox(action.key, lock)
            usage = await self.db.fetchrow(
                f"SELECT * FROM sandbox_uses WHERE id = ? AND token = ? AND deployment = ? AND profile_id = ? "
                f"AND daemon_id = ?{lock}", action.usage_id, action.usage_token, action.key.deployment,
                action.key.profile_id, action.key.daemon_id)
            if (usage is None or usage["state"] != "orphaned"
                    or not self._generation_gone(usage, sandbox_row)):
                return False
            unresolved = await self.db.fetchrow(
                "SELECT id FROM sandbox_execs WHERE usage_id = ? AND state IN ('pending', 'running', 'uncertain') "
                "LIMIT 1", action.usage_id)
            if unresolved is not None:
                return False
            released = await self.db.fetchrow(
                "UPDATE sandbox_uses SET state = 'released', released_at = ?, "
                "release_reason = 'generation_removed' WHERE id = ? AND token = ? AND state = 'orphaned' "
                "RETURNING id", now, action.usage_id, action.usage_token)
            return released is not None

    async def recovery_candidates(self, *, limit: int = 100,
                                  purpose: Literal["recovery", "idle"] = "recovery") -> list[SandboxKey]:
        """Rotate independent bounded scan cursors so idle scans cannot consume recovery's next batch."""
        if purpose not in ("recovery", "idle"):
            raise ValueError("unknown sandbox scan purpose")
        limit = max(0, min(limit, 100))
        if not limit:
            return []
        cursors = getattr(self, "_sandbox_scan_cursors", {})
        cursor = cursors.get(purpose, ("", "", ""))
        query = (
            "SELECT deployment, profile_id, daemon_id FROM sandboxes "
            "UNION SELECT deployment, profile_id, daemon_id FROM sandbox_uses WHERE state != 'released' "
            "OR (release_reason IN ('generation_removed', 'preparation_retired_without_execution') AND EXISTS "
            "(SELECT 1 FROM runs r WHERE r.id = sandbox_uses.run_id AND r.status IN "
            "('queued', 'running', 'stopping'))) "
            "UNION SELECT deployment, profile_id, daemon_id FROM sandbox_operations WHERE resolved_at IS NULL")
        rows = await self.db.fetch(
            "SELECT * FROM (" + query + ") AS candidates WHERE (deployment, profile_id, daemon_id) > (?, ?, ?) "
            "ORDER BY deployment, profile_id, daemon_id LIMIT ?", *cursor, limit)
        if len(rows) < limit and cursor != ("", "", ""):
            rows += await self.db.fetch(
                "SELECT * FROM (" + query + ") AS candidates "
                "WHERE (deployment, profile_id, daemon_id) <= (?, ?, ?) "
                "ORDER BY deployment, profile_id, daemon_id LIMIT ?", *cursor, limit - len(rows))
        if rows:
            cursors[purpose] = (rows[-1]["deployment"], rows[-1]["profile_id"], rows[-1]["daemon_id"])
            self._sandbox_scan_cursors = cursors
        return [_key(row) for row in rows]

    async def get_run_usage(self, run_id: str) -> SandboxUsage | None:
        row = await self.db.fetchrow("SELECT * FROM sandbox_uses WHERE run_id = ?", run_id)
        return _usage(row) if row is not None else None

    async def profile_sandbox_keys(self, profile_id: str, *, deployment: str) -> list[SandboxKey]:
        """Every coordination identity recorded for one profile on this deployment."""
        rows = await self.db.fetch(
            "SELECT daemon_id FROM sandboxes WHERE deployment = ? AND profile_id = ? ORDER BY daemon_id, profile_id",
            deployment, profile_id)
        return [SandboxKey(deployment, profile_id, row["daemon_id"]) for row in rows]

    async def recovery_snapshot(self, key: SandboxKey) -> tuple[dict | None, list[SandboxUsage]]:
        """Observation only; all decisions re-read these identities under the guard."""
        row = await self.db.fetchrow(
            f"SELECT * FROM sandboxes WHERE {_SANDBOX_IDENTITY}", key.deployment, key.profile_id)
        uses = await self.db.fetch(
            "SELECT * FROM sandbox_uses WHERE deployment = ? AND profile_id = ? AND (state != 'released' "
            "OR (release_reason IN ('generation_removed', 'preparation_retired_without_execution') AND EXISTS "
            "(SELECT 1 FROM runs r WHERE r.id = sandbox_uses.run_id AND r.status IN "
            "('queued', 'running', 'stopping'))))",
            key.deployment, key.profile_id)
        return row, [_usage(use) for use in uses]

    async def recovery_diagnostics(self, deployment: str) -> tuple[int, float | None]:
        row = await self.db.fetchrow(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM sandbox_operations "
            "WHERE deployment = ? AND kind = 'create' AND authority = 'retired' AND resolved_at IS NULL",
            deployment)
        now = await self.coordination_now()
        return row["n"], max(0.0, now - row["oldest"]) if row["oldest"] is not None else None

    async def classify_inventory(self, observation: ContainerObservation, *, owner: str,
                                 lease_seconds: float,
                                 deletion_authority: ProfileDeletionAuthority | None = None
                                 ) -> RecoveryAction | None:
        """Classify under the guard; only a fully matched retired attempt permits quarantine removal."""
        labels = dict(observation.labels)
        deployment, profile = labels.get("keel.deployment"), labels.get("keel.profile")
        # Prefer recorded exact-ID ownership over mutable observation labels. A conflicting profile or
        # deployment label must block the original owner, including when the managed label was removed.
        recorded = await self.db.fetchrow(
            "SELECT deployment, profile_id, daemon_id FROM sandboxes WHERE container_id = ? "
            "UNION SELECT deployment, profile_id, daemon_id FROM sandbox_operations "
            "WHERE container_id = ? OR discovered_container_id = ? LIMIT 1",
            observation.container_id, observation.container_id, observation.container_id)
        if recorded is not None:
            key = _key(recorded)
        elif labels.get("keel.managed") == "1" and deployment and profile:
            key = SandboxKey(deployment, profile, observation.daemon_id)
        else:
            return None
        profile = key.profile_id
        async with self.coordination_guard(profile, skip_locked=deletion_authority is None) as available:
            if not available:
                # Another instance holds the profile: nothing was classified, so an inventory cycle stays open.
                return RecoveryAction(key, "skipped", container_id=observation.container_id)
            if deletion_authority is not None:
                await self._assert_deletion_authority(profile, deletion_authority)
            lock = self._lock_suffix()
            now = await self.coordination_now()
            sandbox = await self._lock_sandbox(key, lock)
            operations = await self._lock_operations(key, lock)
            usages = await self._lock_usages(key, lock)
            execs = await self._lock_execs(key, lock)
            create = next((op for op in operations if op["id"] == labels.get("keel.create-operation")
                           and op["kind"] == "create"), None)
            matches = (labels.get("keel.managed") == "1" and deployment == key.deployment
                       and labels.get("keel.profile") == key.profile_id
                       and create is not None and create["daemon_id"] == observation.daemon_id
                       and create["intended_name"] == observation.container_name
                       and str(create["generation"]) == labels.get("keel.generation")
                       and create["generation_token"] == labels.get("keel.generation-token")
                       and create["container_id"] in (None, observation.container_id)
                       and create["discovered_container_id"] in (None, observation.container_id)
                       # Both values come from this container's own inspect: the comparison proves its policy
                       # record is internally consistent, not that it equals the profile policy (publication
                       # and reuse check the recorded digest separately).
                       and observation.policy_digest == labels.get("keel.policy")
                       and observation.policy_digest is not None and len(observation.policy_digest) == 16
                       and all(char in "0123456789abcdef" for char in observation.policy_digest)
                       and len(labels) == len(observation.labels))
            if sandbox is not None and sandbox["daemon_id"] != observation.daemon_id:
                return await self._block_inventory(key, observation, now, "sandbox_daemon_mismatch")
            if not matches:
                return await self._block_inventory(key, observation, now, "sandbox_inventory_conflict")
            if (sandbox is not None and sandbox["container_id"] == observation.container_id
                    and sandbox["generation_token"] == create["generation_token"]
                    and sandbox["policy_digest"] != observation.policy_digest):
                return await self._block_inventory(key, observation, now, "sandbox_inventory_conflict")
            await self.db.execute(
                "UPDATE sandbox_operations SET container_id = COALESCE(container_id, ?), "
                "discovered_container_id = COALESCE(discovered_container_id, ?), updated_at = ? WHERE id = ?",
                observation.container_id, observation.container_id, now, create["id"])
            # A conflict recorded on this very container is reconciled by a fully matching observation of it:
            # the operator restored the labels or the earlier inspect was wrong, and the published generation
            # was verified end to end above. Conflicts recorded on another container clear only after that
            # container is proven absent by exact ID (recovery does that after the scan). A generation still
            # draining for another reason keeps its state and loses only the conflict evidence.
            if (sandbox is not None and sandbox["error_code"] == "sandbox_inventory_conflict"
                    and sandbox["error_message"] == observation.container_id
                    and sandbox["container_id"] == observation.container_id
                    and sandbox["generation_token"] == create["generation_token"]
                    and create["authority"] == "current" and sandbox["state"] in ("draining", "blocked")):
                if _generation_draining(usages, execs, operations, sandbox["generation_token"], now):
                    await self.db.execute(
                        "UPDATE sandboxes SET error_code = NULL, error_message = NULL, transitioned_at = ? "
                        "WHERE deployment = ? AND profile_id = ? AND container_id = ? AND generation_token = ?",
                        now, key.deployment, key.profile_id, observation.container_id,
                        create["generation_token"])
                    sandbox = dict(sandbox, error_code=None, error_message=None)
                else:
                    await self.db.execute(
                        "UPDATE sandboxes SET state = 'ready', error_code = NULL, error_message = NULL, "
                        "transitioned_at = ? WHERE deployment = ? AND profile_id = ? AND container_id = ? "
                        "AND generation_token = ? AND state IN ('draining', 'blocked')",
                        now, key.deployment, key.profile_id, observation.container_id,
                        create["generation_token"])
                    sandbox = dict(sandbox, state="ready", error_code=None, error_message=None)
            if create["authority"] != "retired":
                return RecoveryAction(key, "protected", container_id=observation.container_id,
                                      operation_id=create["id"], reason="current_preparation")
            linked = [op for op in operations if op["original_create_operation_id"] == create["id"]]
            started = any(op["kind"] == "start" and op["confirmed_absent_at"] is None
                          and op["outcome"] != "definite_failure" for op in linked)
            executed = any(ex["generation_token"] == create["generation_token"]
                           and ex["state"] in _UNRESOLVED_EXEC_STATES for ex in execs)
            if observation.running and not (started or executed):
                return await self._block_inventory(key, observation, now, "sandbox_retired_create_running")
            if sandbox is not None and sandbox["error_code"] in _INVENTORY_BLOCK_CODES:
                return RecoveryAction(key, "blocked", reason=sandbox["error_code"])
            # A synthetic historical generation selects only that exact target. Conditional updates in
            # _plan_remove/_commit_remove_success cannot overwrite a newer generation's pointer or usages.
            historical = dict(sandbox or {})
            historical.update(generation=create["generation"], generation_token=create["generation_token"],
                              container_id=observation.container_id, container_name=observation.container_name,
                              state="draining")
            action = await self._plan_remove(key, owner, lease_seconds, historical, operations,
                                             [use for use in usages if use["container_id"] == observation.container_id],
                                             execs, now)
            if action is not None:
                await self.db.execute("UPDATE sandbox_operations SET cleanup_operation_id = ? WHERE id = ?",
                                      action.operation_id, create["id"])
            return action

    async def _block_inventory(self, key: SandboxKey, observation: ContainerObservation, now: float,
                               reason: str) -> RecoveryAction:
        # An unknown extra target or a conflict on the published current container blocks new admission. The
        # recorded exact-ID generation keeps its execution and control authority while it drains: exec dispatch
        # targets that ID, so a label disagreement cannot redirect it, and an in-flight command must stay
        # cancellable. A row without a verified current target is blocked outright.
        current = await self._lock_sandbox(key, self._lock_suffix())
        verified_current = (current is not None and current["container_id"] is not None
                            and current["daemon_id"] == observation.daemon_id
                            and current["state"] in ("ready", "draining"))
        state = "draining" if verified_current else "blocked"
        # Store only fixed reasons and exact identities, never raw Docker output or model-controlled labels.
        await self.db.execute(
            "INSERT INTO sandboxes (deployment, profile_id, daemon_id, generation, state, error_code, "
            "error_message, transitioned_at) VALUES (?, ?, ?, 0, 'blocked', ?, ?, ?) "
            "ON CONFLICT (deployment, profile_id) DO UPDATE SET state = ?, error_code = ?, "
            "error_message = ?, transitioned_at = ?",
            key.deployment, key.profile_id, key.daemon_id, reason, observation.container_id, now,
            state, reason, observation.container_id, now)
        return RecoveryAction(key, "blocked", container_id=observation.container_id, reason=reason)

    async def blocked_inventory_targets(self, keys: list[SandboxKey], *,
                                        daemon_id: str) -> list[tuple[SandboxKey, str]]:
        """Recorded conflicting container IDs for verified profiles that still withhold admission.

        Only blocks recorded on a container other than the published one are returned; a conflict recorded on
        the published container itself clears only when that container is verified matching again.
        """
        targets = []
        for key in keys:
            if key.daemon_id != daemon_id:
                continue
            async with self.coordination_guard(key.profile_id, skip_locked=True) as available:
                if not available:
                    continue
                sandbox = await self._lock_sandbox(key, self._lock_suffix())
                if (sandbox is None or sandbox["daemon_id"] != daemon_id
                        or sandbox["state"] not in ("draining", "blocked")
                        or sandbox["error_code"] not in _INVENTORY_BLOCK_CODES
                        or sandbox["container_id"] is None or sandbox["generation_token"] is None
                        or sandbox["error_message"] is None
                        or sandbox["error_message"] == sandbox["container_id"]):
                    continue
                targets.append((key, sandbox["error_message"]))
        return targets

    async def recorded_container_ids(self, key: SandboxKey) -> set[str]:
        """Every exact container ID the ledger attributes to this profile: its current one and every attempt's."""
        rows = await self.db.fetch(
            "SELECT container_id AS id FROM sandboxes WHERE deployment = ? AND profile_id = ? "
            "UNION SELECT container_id FROM sandbox_operations WHERE deployment = ? AND profile_id = ? "
            "UNION SELECT discovered_container_id FROM sandbox_operations WHERE deployment = ? AND profile_id = ?",
            key.deployment, key.profile_id, key.deployment, key.profile_id, key.deployment, key.profile_id)
        return {row["id"] for row in rows if row["id"]}

    async def unpublished_inventory_block(self, key: SandboxKey) -> str | None:
        """The conflicting container ID recorded on a `blocked` profile that has no published generation.

        A legacy or unknown container blocks such a profile; the block can clear only after the caller proves this
        exact ID absent (see `clear_resolved_inventory_block`).
        """
        row = await self.db.fetchrow(f"SELECT * FROM sandboxes WHERE {_SANDBOX_IDENTITY}", key.deployment,
                                     key.profile_id)
        if (row is None or row["daemon_id"] != key.daemon_id or row["state"] != "blocked"
                or row["container_id"] is not None or row["error_code"] not in _INVENTORY_BLOCK_CODES
                or not row["error_message"]):
            return None
        return row["error_message"]

    async def clear_resolved_inventory_block(self, key: SandboxKey, *, daemon_id: str,
                                             offending: str | None = None,
                                             deletion_authority: ProfileDeletionAuthority | None = None) -> bool:
        """Clear an admission block after the caller proves the recorded conflicting container absent.

        The exact-ID absence evidence must come from this same daemon. A profile still draining for another
        reason (expired, preparing, cleaning or orphaned usage, unresolved execution or current operation) keeps
        its state and loses only the conflict evidence, so this never reopens admission on a draining generation.
        """
        async with self.coordination_guard(key.profile_id, skip_locked=deletion_authority is None) as available:
            if not available:
                return False
            if deletion_authority is not None:
                await self._assert_deletion_authority(key.profile_id, deletion_authority)
            lock = self._lock_suffix()
            sandbox = await self._lock_sandbox(key, lock)
            if (sandbox is not None and sandbox["container_id"] is None and offending is not None
                    and sandbox["error_message"] == offending):
                return await self._clear_unpublished_block(key, sandbox, daemon_id, lock)
            if (sandbox is None or sandbox["daemon_id"] != daemon_id
                    or sandbox["state"] not in ("draining", "blocked")
                    or sandbox["error_code"] not in _INVENTORY_BLOCK_CODES
                    or sandbox["container_id"] is None or sandbox["generation_token"] is None
                    or sandbox["error_message"] is None
                    or sandbox["error_message"] == sandbox["container_id"]
                    or (offending is not None and sandbox["error_message"] != offending)):
                return False
            usages = await self._lock_usages(key, lock)
            execs = await self._lock_execs(key, lock)
            operations = await self._lock_operations(key, lock)
            now = await self.coordination_now()
            token = sandbox["generation_token"]
            if _generation_draining(usages, execs, operations, token, now):
                await self.db.execute(
                    "UPDATE sandboxes SET error_code = NULL, error_message = NULL, transitioned_at = ? "
                    "WHERE deployment = ? AND profile_id = ? AND container_id = ? AND generation_token = ?",
                    now, key.deployment, key.profile_id, sandbox["container_id"], token)
                return False
            await self.db.execute(
                "UPDATE sandboxes SET state = 'ready', error_code = NULL, error_message = NULL, "
                "transitioned_at = ? WHERE deployment = ? AND profile_id = ? AND container_id = ? "
                "AND generation_token = ? AND state IN ('draining', 'blocked')",
                now, key.deployment, key.profile_id, sandbox["container_id"], token)
            return True

    async def _clear_unpublished_block(self, key: SandboxKey, sandbox: dict, daemon_id: str, lock: str) -> bool:
        """Move a blocked profile without a published generation to `absent` once its conflict is proven gone.

        Any unreleased usage, unresolved execution, unresolved current operation or known unconfirmed target keeps
        the block: those still need their own recovery before a fresh generation may be prepared.
        """
        if (sandbox["daemon_id"] != daemon_id or sandbox["state"] != "blocked"
                or sandbox["error_code"] not in _INVENTORY_BLOCK_CODES):
            return False
        usages = await self._lock_usages(key, lock)
        execs = await self._lock_execs(key, lock)
        operations = await self._lock_operations(key, lock)
        if any(use["state"] != "released" for use in usages):
            return False
        if any(ex["state"] in _UNRESOLVED_EXEC_STATES for ex in execs):
            return False
        token = sandbox["generation_token"]
        if any(op["authority"] == "current" and op["outcome"] in ("planned", "dispatched", "uncertain")
               and op["confirmed_absent_at"] is None for op in operations):
            return False
        if token is not None and any(
                op["generation_token"] == token and op["confirmed_absent_at"] is None
                and (op["container_id"] is not None or op["discovered_container_id"] is not None)
                for op in operations):
            return False
        now = await self.coordination_now()
        cleared = await self.db.fetchrow(
            "UPDATE sandboxes SET state = 'absent', generation_token = NULL, container_name = NULL, "
            "policy_digest = NULL, helper_protocol = NULL, helper_digest = NULL, current_operation_id = NULL, "
            "idle_since = NULL, error_code = NULL, error_message = NULL, transitioned_at = ? "
            "WHERE deployment = ? AND profile_id = ? AND daemon_id = ? AND state = 'blocked' "
            "AND container_id IS NULL AND error_message = ? RETURNING deployment",
            now, key.deployment, key.profile_id, daemon_id, sandbox["error_message"])
        return cleared is not None

    async def confirm_generation_missing(self, key: SandboxKey, container_id: str, *,
                                         deletion_authority: ProfileDeletionAuthority | None = None) -> bool:
        """Revoke users only after the caller has authoritative exact-ID absence evidence."""
        async with self.coordination_guard(key.profile_id, skip_locked=deletion_authority is None) as available:
            if not available:
                return False
            if deletion_authority is not None:
                await self._assert_deletion_authority(key.profile_id, deletion_authority)
            sandbox = await self._lock_sandbox(key, self._lock_suffix())
            if sandbox is None or sandbox["daemon_id"] != key.daemon_id or sandbox["container_id"] != container_id:
                return False
            await self._lock_operations(key, self._lock_suffix())
            await self._lock_usages(key, self._lock_suffix())
            await self._lock_execs(key, self._lock_suffix())
            now = await self.coordination_now()
            await self.db.execute(
                "UPDATE sandbox_uses SET state = 'orphaned' WHERE deployment = ? AND profile_id = ? "
                "AND container_id = ? AND state IN ('active', 'cleaning')", key.deployment, key.profile_id,
                container_id)
            await self.db.execute(
                "UPDATE sandboxes SET state = 'draining', transitioned_at = ? "
                "WHERE deployment = ? AND profile_id = ?", now, key.deployment, key.profile_id)
            return True

    async def plan_idle_removal(self, key: SandboxKey, *, owner: str, lease_seconds: float,
                                idle_seconds: float, helper_digest: str,
                                deletion_authority: ProfileDeletionAuthority | None = None
                                ) -> list[RecoveryAction]:
        """Remove expired idle or incompatible ready generations, after every valid user finishes."""
        async with self.coordination_guard(key.profile_id, skip_locked=deletion_authority is None) as available:
            if not available:
                return []
            if deletion_authority is not None:
                await self._assert_deletion_authority(key.profile_id, deletion_authority)
            lock = self._lock_suffix()
            sandbox = await self._lock_sandbox(key, lock)
            operations = await self._lock_operations(key, lock)
            usages = await self._lock_usages(key, lock)
            execs = await self._lock_execs(key, lock)
            if sandbox is None or sandbox["daemon_id"] != key.daemon_id or sandbox["state"] != "ready":
                return []
            if any(use["state"] != "released" for use in usages):
                return []
            current = [op for op in operations if op["generation_token"] == sandbox["generation_token"]]
            if any(op["outcome"] in ("planned", "dispatched", "uncertain")
                   and op["confirmed_absent_at"] is None for op in current):
                return []
            profile = await self.db.fetchrow("SELECT sandbox FROM profiles WHERE id = ?", key.profile_id)
            digest = _effective_digest(json.loads(profile["sandbox"])) if profile is not None else None
            incompatible = (sandbox["policy_digest"] != digest
                            or sandbox["helper_protocol"] not in (None, HELPER_PROTOCOL_VERSION)
                            or sandbox["helper_digest"] not in (None, helper_digest))
            now = await self.coordination_now()
            if not incompatible and (sandbox["idle_since"] is None or sandbox["idle_since"] + idle_seconds > now):
                return []
            sandbox["state"] = "draining"
            action = await self._plan_remove(key, owner, lease_seconds, sandbox, operations, usages, execs, now)
            return [action] if action is not None else []

    async def rearm_recovery_remove(self, action: RecoveryAction, *,
                                    deletion_authority: ProfileDeletionAuthority | None = None
                                    ) -> RecoveryAction | None:
        """After observing a still-present target, retire the old dispatch before a fresh remove intent."""
        async with self.coordination_guard(action.key.profile_id, skip_locked=deletion_authority is None) as available:
            if not available:
                return None
            if deletion_authority is not None:
                await self._assert_deletion_authority(action.key.profile_id, deletion_authority)
            lock = self._lock_suffix()
            sandbox = await self._lock_sandbox(action.key, lock)
            operations = await self._lock_operations(action.key, lock)
            operation = next((op for op in operations if op["id"] == action.operation_id), None)
            if (operation is None or operation["owner_token"] != action.owner_token
                    or operation["authority"] != "current" or operation["outcome"] not in ("dispatched", "uncertain")
                    or operation["container_id"] != action.container_id):
                return None
            now = await self.coordination_now()
            operation_id, token = "op_" + uuid.uuid4().hex, uuid.uuid4().hex
            await self.db.execute(
                "UPDATE sandbox_operations SET authority = 'retired', retired_at = ?, "
                "retirement_reason = 'remove_retry', updated_at = ? WHERE id = ? AND owner_token = ?",
                now, now, action.operation_id, action.owner_token)
            await self.db.execute(
                "INSERT INTO sandbox_operations (id, kind, deployment, profile_id, daemon_id, generation, "
                "generation_token, intended_name, original_create_operation_id, container_id, owner, owner_token, "
                "lease_expires_at, created_at, updated_at) VALUES (?, 'remove', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                operation_id, action.key.deployment, action.key.profile_id, action.key.daemon_id,
                action.generation, action.generation_token, operation["intended_name"],
                operation["original_create_operation_id"], action.container_id, operation["owner"], token,
                operation["lease_expires_at"], now, now)
            if sandbox is not None and sandbox["current_operation_id"] == action.operation_id:
                await self.db.execute(
                    "UPDATE sandboxes SET current_operation_id = ? WHERE deployment = ? AND profile_id = ? "
                    "AND current_operation_id = ?", operation_id, action.key.deployment,
                    action.key.profile_id, action.operation_id)
            await self.db.execute(
                "UPDATE sandbox_operations SET cleanup_operation_id = ? WHERE cleanup_operation_id = ?",
                operation_id, action.operation_id)
            return RecoveryAction(key=action.key, action="remove", operation_id=operation_id,
                                  container_id=action.container_id, generation=action.generation,
                                  generation_token=action.generation_token, owner_token=token, reason=action.reason)

    async def authorize_recovery_remove(self, action: RecoveryAction, *,
                                        deletion_authority: ProfileDeletionAuthority | None = None) -> bool:
        """Recheck exact-target bindings immediately before dispatch under the same admission guard."""
        # This is an already-owned exact-ID removal, so wait for the short guard rather than dropping
        # authorization on transient contention. Discovery and claim paths remain nonblocking.
        async with self.coordination_guard(action.key.profile_id) as available:
            if not available:
                return False
            if deletion_authority is not None:
                await self._assert_deletion_authority(
                    action.key.profile_id, deletion_authority)
            sandbox = await self._lock_sandbox(action.key, self._lock_suffix())
            if sandbox is not None and (sandbox["daemon_id"] != action.key.daemon_id
                                        or sandbox["error_code"] in _INVENTORY_BLOCK_CODES):
                return False
            operation = await self.get_operation(action.operation_id)
            now = await self.coordination_now()
            if (operation is None or operation.owner_token != action.owner_token or operation.authority != "current"
                    or operation.container_id != action.container_id or operation.lease_expires_at is None
                    or operation.lease_expires_at < now):
                return False
            users = await self._lock_usages(action.key, self._lock_suffix())
            if any(use["container_id"] == action.container_id and use["state"] in _UNRELEASED_USAGE_STATES
                   for use in users):
                return False
            if operation.outcome == "planned":
                return await self.authorize_dispatch(operation)
            return operation.outcome in ("dispatched", "uncertain")

    # evidence retention

    async def prune_sandbox_records(self, retention_seconds: float, *, limit: int = 100) -> int:
        """Delete at most `limit` resolved, unreferenced coordination rows older than the cutoff.

        Execs go first, then operations, then usages, so references disappear before their dependents. Unresolved
        creates, current pointers, unreleased usages and unresolved execs are never candidates; ineligible rows are
        excluded before the limit, so they cannot starve a newer eligible row.
        """
        if limit <= 0:
            return 0
        async with self.db.transaction():
            now = await self.coordination_now()
            cutoff = now - retention_seconds
            deleted = 0
            remaining = limit
            exec_rows = await self.db.fetch(
                "SELECT id, COALESCE(finished_at, created_at) AS resolved FROM sandbox_execs "
                "WHERE state IN ('finished', 'retired') AND COALESCE(finished_at, created_at) < ? "
                "ORDER BY resolved, id LIMIT ?", cutoff, remaining)
            if exec_rows:
                ids = [row["id"] for row in exec_rows]
                removed = await self.db.fetch(
                    _delete_ids("sandbox_execs", ids) +
                    " AND state IN ('finished', 'retired') "
                    "AND COALESCE(finished_at, created_at) < ? RETURNING id", *ids, cutoff)
                deleted += len(removed)
                remaining = limit - deleted
            if remaining > 0:
                # The reference check runs before LIMIT so a blocked dependency can never starve an eligible row;
                # a referred-to row becomes prunable on the next pass, after its referrer is gone.
                candidates = await self.db.fetch(
                    "SELECT o.id FROM sandbox_operations o WHERE o.resolved_at IS NOT NULL "
                    "AND o.resolved_at < ? "
                    "AND NOT EXISTS (SELECT 1 FROM sandboxes s WHERE s.current_operation_id = o.id) "
                    "AND (o.usage_id IS NULL OR NOT EXISTS (SELECT 1 FROM sandbox_uses u WHERE u.id = o.usage_id) "
                    "OR EXISTS (SELECT 1 FROM sandbox_uses u WHERE u.id = o.usage_id AND u.state = 'released')) "
                    "AND NOT EXISTS (SELECT 1 FROM sandbox_operations r WHERE r.id <> o.id "
                    "AND (r.original_create_operation_id = o.id OR r.cleanup_operation_id = o.id)) "
                    "ORDER BY o.resolved_at, o.id LIMIT ?", cutoff, remaining)
                if candidates:
                    ids = [row["id"] for row in candidates]
                    removed = await self.db.fetch(
                        _delete_ids("sandbox_operations", ids) +
                        " AND resolved_at IS NOT NULL AND resolved_at < ? RETURNING id",
                        *ids, cutoff)
                    deleted += len(removed)
                    remaining = limit - deleted
            if remaining > 0:
                usage_rows = await self.db.fetch(
                    "SELECT id FROM sandbox_uses u WHERE state = 'released' AND released_at < ? "
                    "AND NOT EXISTS (SELECT 1 FROM sandbox_execs e WHERE e.usage_id = u.id) "
                    "AND NOT EXISTS (SELECT 1 FROM sandbox_operations o WHERE o.usage_id = u.id) "
                    "ORDER BY released_at, id LIMIT ?", cutoff, remaining)
                if usage_rows:
                    ids = [row["id"] for row in usage_rows]
                    removed = await self.db.fetch(
                        _delete_ids("sandbox_uses", ids) +
                        " AND state = 'released' AND released_at < ? RETURNING id", *ids, cutoff)
                    deleted += len(removed)
            return deleted

    async def get_published_generation(self, key: SandboxKey, policy: SandboxPolicy) -> SandboxBinding | None:
        """The published generation for a compatible policy, or None.

        A usage binding records identity only, so the coordinator reconstructs the applied policy from the profile
        only while its canonical digest still equals the published `policy_digest`. The helper digest comes from
        the published row, never from the caller's local artifact. A row from before helper recording (NULL
        protocol/digest) remains usable; a row publishing another helper protocol does not.
        """
        row = await self.db.fetchrow(
            f"SELECT * FROM sandboxes WHERE {_SANDBOX_IDENTITY}", key.deployment, key.profile_id)
        if (row is None or row["daemon_id"] != key.daemon_id or row["state"] != "ready"
                or row["generation_token"] is None or row["container_id"] is None
                or row["container_name"] is None or row["policy_digest"] != policy.digest()):
            return None
        if row["helper_protocol"] is not None and row["helper_protocol"] != HELPER_PROTOCOL_VERSION:
            return None
        return SandboxBinding(
            row["generation"], row["generation_token"], row["container_id"], row["container_name"], policy,
            row["helper_digest"] or "")

    async def get_usage(self, usage_id: str) -> SandboxUsage | None:
        row = await self.db.fetchrow("SELECT * FROM sandbox_uses WHERE id = ?", usage_id)
        return _usage(row) if row is not None else None

    async def get_operation(self, operation_id: str) -> LifecycleAttempt | None:
        row = await self.db.fetchrow("SELECT * FROM sandbox_operations WHERE id = ?", operation_id)
        return _attempt(row) if row is not None else None
