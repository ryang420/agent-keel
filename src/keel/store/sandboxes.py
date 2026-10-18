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
from typing import AsyncIterator

from keel.models import Usage
from keel.sandbox.base import PolicyError, parse_policy
from keel.sandbox.records import (
    DockerOutcome, LeaseStamp, LifecycleAttempt, RunClaim, SandboxBinding, SandboxBusy, SandboxKey,
    SandboxLeaseLost, SandboxUsage, UsageBinding, UseHandle,
)
from keel.store.base import Run, UniqueViolation

_PREPARATION_KINDS = ("create", "install", "start")
_RENEWABLE_USAGE_STATES = ("preparing", "active", "cleaning")
_SANDBOX_IDENTITY = "deployment = ? AND profile_id = ?"


def _run(row: dict) -> Run:
    return Run(
        row["id"], row["profile_id"], row["session_id"], row["status"], row["provider"], row["model"],
        row["reasoning_effort"], json.loads(row["input"]), row["instructions"], row["output"],
        Usage.from_dict(json.loads(row["usage"])), row["error_code"], row["error"], row["lease_owner"],
        row["lease_expires_at"], row["created_at"], row["started_at"], row["finished_at"],
        row["current_exec_id"],
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
                lock = " FOR UPDATE SKIP LOCKED" if skip_locked else " FOR UPDATE"
                row = await self.db.fetchrow(f"SELECT id FROM profiles WHERE id = ?{lock}", profile_id)
            else:
                row = await self.db.fetchrow("SELECT id FROM profiles WHERE id = ?", profile_id)
            yield row is not None

    def _lock_suffix(self) -> str:
        """Postgres row locks explicitly; SQLite's BEGIN IMMEDIATE write transaction already serializes."""
        return " FOR UPDATE" if self.db.dialect == "postgres" else ""

    # admission

    async def claim_run_with_usage(self, owner: str, lease_seconds: float, *, deployment: str,
                                   daemon_id: str) -> RunClaim | None:
        """Claim one queued run and reserve its usage under the profile guard.

        Candidates are read without write locks. Each profile is tried with `SKIP LOCKED`, so a blocked or busy
        profile at the head of the queue does not stop a healthy later profile. A ready compatible generation is
        shared immediately; otherwise this claim becomes the only preparation owner.
        """
        async with self.db.transaction():
            candidates = await self.db.fetch(
                "SELECT id, profile_id FROM runs WHERE status = 'queued' ORDER BY created_at, id")
            for candidate in candidates:
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
                    if sandbox:
                        # A record naming another daemon is a configuration/migration conflict, never an absence.
                        # It only matters to a sandboxed profile; chat-only runs need no Docker coordination.
                        if sandbox_row is not None and sandbox_row["daemon_id"] != daemon_id:
                            continue
                        if sandbox_row is not None and sandbox_row["state"] in ("draining", "removing", "blocked"):
                            continue
                        preparing = await self.db.fetchrow(
                            "SELECT id FROM sandbox_uses WHERE deployment = ? AND profile_id = ? "
                            "AND state = 'preparing' LIMIT 1", deployment, candidate["profile_id"])
                        if preparing is not None:
                            continue
                        operations = await self.db.fetch(
                            "SELECT kind, generation, outcome, authority FROM sandbox_operations "
                            "WHERE deployment = ? AND profile_id = ? AND authority = 'current' ORDER BY id",
                            deployment, candidate["profile_id"])
                        if _has_preparation_in_flight(sandbox_row, operations):
                            continue
                    now = await self.coordination_now()
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
                            updated, sandbox, sandbox_row, owner, deployment, daemon_id, now, expires_at)
                    return RunClaim(_run(updated), use, LeaseStamp(now, expires_at))
            return None

    async def _queued_run_row(self, run_id: str, profile_id: str) -> dict | None:
        lock = " FOR UPDATE" if self.db.dialect == "postgres" else ""
        return await self.db.fetchrow(
            f"SELECT * FROM runs WHERE id = ? AND profile_id = ? AND status = 'queued'{lock}",
            run_id, profile_id)

    async def _reserve_usage(self, run_row: dict, sandbox: dict, sandbox_row: dict | None, owner: str,
                             deployment: str, daemon_id: str, now: float, expires_at: float) -> UseHandle:
        digest = _effective_digest(sandbox)
        ready = (sandbox_row is not None and sandbox_row["state"] == "ready"
                 and sandbox_row["daemon_id"] == daemon_id and digest is not None
                 and sandbox_row["policy_digest"] == digest
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

        A current generation is identified by the sandbox row; an unpublished current generation is identified by
        its succeeded create attempt. A historical generation needs a matching operation row. Recording removal
        also retires every current preparation attempt for that exact target, so no planned start survives it.
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

        A current dispatched attempt advances its outcome and may attach a newly discovered exact ID. A stale or
        retired attempt only ever attaches matching evidence that is still null: it never changes authority or
        outcome, publishes, or touches another generation's pointer.
        """
        discovered = outcome.observation.container_id if outcome.observation is not None else None
        async with self.db.transaction():
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
                "AND (CAST(? AS TEXT) IS NULL OR container_id IS NULL OR container_id = ?) "
                "AND (CAST(? AS TEXT) IS NULL OR discovered_container_id IS NULL "
                "OR discovered_container_id = ?) RETURNING *",
                outcome.state, outcome.diagnostic, resolved_at, discovered, discovered, now, attempt.id,
                attempt.owner_token, discovered, discovered, discovered, discovered)
            if row is not None:
                return _attempt(row)
            current = await self.db.fetchrow(
                "SELECT * FROM sandbox_operations WHERE id = ?", attempt.id)
            if current is None:
                raise SandboxLeaseLost(f"operation {attempt.id!r} no longer exists")
            if current["owner_token"] != attempt.owner_token or discovered is None:
                return _attempt(current)
            evidence = await self.db.fetchrow(
                "UPDATE sandbox_operations SET "
                "container_id = CASE WHEN container_id IS NULL THEN ? ELSE container_id END, "
                "discovered_container_id = CASE WHEN discovered_container_id IS NULL THEN ? "
                "ELSE discovered_container_id END, updated_at = ? "
                "WHERE id = ? AND owner_token = ? "
                "AND (CAST(? AS TEXT) IS NULL OR container_id IS NULL OR container_id = ?) "
                "AND (CAST(? AS TEXT) IS NULL OR discovered_container_id IS NULL "
                "OR discovered_container_id = ?) RETURNING *",
                discovered, discovered, now, attempt.id, attempt.owner_token, discovered, discovered,
                discovered, discovered)
            return _attempt(evidence if evidence is not None else current)

    # read models

    async def get_usage(self, usage_id: str) -> SandboxUsage | None:
        row = await self.db.fetchrow("SELECT * FROM sandbox_uses WHERE id = ?", usage_id)
        return _usage(row) if row is not None else None

    async def get_operation(self, operation_id: str) -> LifecycleAttempt | None:
        row = await self.db.fetchrow("SELECT * FROM sandbox_operations WHERE id = ?", operation_id)
        return _attempt(row) if row is not None else None
