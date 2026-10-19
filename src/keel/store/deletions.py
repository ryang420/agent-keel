"""Tenant-owned profile deletion fences, receipts and owned-data purge."""

from __future__ import annotations

from dataclasses import dataclass

from keel.store.base import (
    ProfileDeletionBusy,
    ProfileDeletionConflict,
    ProfileTenantMismatch,
    UniqueViolation,
)

PHASES = ("blocked", "draining", "purged")
NEXT_PHASE = {"blocked": "draining", "draining": "purged"}


@dataclass(frozen=True)
class DeletionRecord:
    profile_id: str
    tenant_id: int
    operation_id: str
    phase: str


class ProfileDeletionStoreMixin:
    """Durable fence, progress and completion receipts for one profile deletion."""

    async def begin_profile_deletion(self, profile_id: str, tenant_id: int,
                                     operation_id: str) -> DeletionRecord:
        """Claim the profile for one operation, or return the same operation's record.

        A profile that was never prepared is still fenced: the operation and its
        tenant are durable, so no later PUT may create data during cleanup.
        """
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be a non-empty string")
        # Two instances may race to insert the fence for an absent profile row.
        # The loser re-reads the winner's committed row instead of surfacing a
        # unique-constraint error.
        for attempt in range(2):
            try:
                return await self._begin_once(profile_id, tenant_id, operation_id)
            except UniqueViolation:
                if attempt == 1:
                    raise ProfileDeletionBusy(profile_id) from None
        raise ProfileDeletionBusy(profile_id)

    async def _begin_once(self, profile_id: str, tenant_id: int, operation_id: str) -> DeletionRecord:
        async with self.db.transaction():
            profile = await self.db.fetchrow("SELECT tenant_id FROM profiles WHERE id = ?", profile_id)
            if profile is not None and profile["tenant_id"] is not None:
                if int(profile["tenant_id"]) != int(tenant_id):
                    raise ProfileTenantMismatch(profile_id)
            row = await self.db.fetchrow(
                "SELECT * FROM profile_deletions WHERE profile_id = ?", profile_id)
            if row is not None:
                if row["operation_id"] != operation_id:
                    raise ProfileDeletionBusy(profile_id)
                if int(row["tenant_id"]) != int(tenant_id):
                    raise ProfileTenantMismatch(profile_id)
                return _record(row)
            receipt = await self.db.fetchrow(
                "SELECT tenant_id FROM profile_deletion_receipts WHERE profile_id = ? AND operation_id = ?",
                profile_id, operation_id)
            if receipt is not None:
                if int(receipt["tenant_id"]) != int(tenant_id):
                    raise ProfileTenantMismatch(profile_id)
                return DeletionRecord(profile_id, int(tenant_id), operation_id, "complete")
            now = self.clock()
            await self.db.execute(
                "INSERT INTO profile_deletions (profile_id, tenant_id, operation_id, phase, created_at, updated_at) "
                "VALUES (?, ?, ?, 'blocked', ?, ?)",
                profile_id, int(tenant_id), operation_id, now, now)
            return DeletionRecord(profile_id, int(tenant_id), operation_id, "blocked")

    async def get_profile_deletion(self, profile_id: str) -> DeletionRecord | None:
        row = await self.db.fetchrow("SELECT * FROM profile_deletions WHERE profile_id = ?", profile_id)
        return _record(row) if row is not None else None

    async def assert_profile_writable(self, profile_id: str) -> None:
        """Refuse a profile-owned write while a deletion fence exists."""
        row = await self.db.fetchrow(
            "SELECT 1 AS fenced FROM profile_deletions WHERE profile_id = ? LIMIT 1", profile_id)
        if row is not None:
            raise ProfileDeletionBusy(profile_id)

    async def assert_profile_admitted(self, profile_id: str) -> None:
        """Refuse data writes while fenced, or after a completed reset left no profile.

        A receipt blocks a late writer that still holds an old profile id even
        after the fence is released. A newly provisioned profile row makes the
        receipt historical, so the same deterministic id may be used again.
        """
        await self.assert_profile_writable(profile_id)
        receipt = await self.db.fetchrow(
            "SELECT 1 AS done FROM profile_deletion_receipts WHERE profile_id = ? LIMIT 1", profile_id)
        if receipt is not None:
            profile = await self.db.fetchrow("SELECT 1 AS ok FROM profiles WHERE id = ?", profile_id)
            if profile is None:
                raise ProfileDeletionBusy(profile_id)

    async def advance_profile_deletion(self, record: DeletionRecord, next_phase: str) -> DeletionRecord:
        """Compare-and-set one phase transition; only the owning operation may advance."""
        if next_phase not in PHASES or NEXT_PHASE.get(record.phase) != next_phase:
            raise ValueError(f"invalid profile deletion phase transition {record.phase!r} -> {next_phase!r}")
        async with self.db.transaction():
            row = await self.db.fetchrow(
                "SELECT * FROM profile_deletions WHERE profile_id = ?", record.profile_id)
            if row is None or row["operation_id"] != record.operation_id or row["phase"] != record.phase:
                raise ProfileDeletionConflict(record.profile_id)
            updated = await self.db.fetchrow(
                "UPDATE profile_deletions SET phase = ?, updated_at = ? "
                "WHERE profile_id = ? AND operation_id = ? AND phase = ? RETURNING *",
                next_phase, self.clock(), record.profile_id, record.operation_id, record.phase)
            if updated is None:
                raise ProfileDeletionConflict(record.profile_id)
            return _record(updated)

    async def purge_profile_data(self, record: DeletionRecord) -> None:
        """Remove every profile-owned transcript, memory, blob and profile row.

        Runs only once the runtime and its sandboxes are proven gone. Missing
        rows are success, so a retry after a partial process leaves no residue.
        """
        async with self.db.transaction():
            row = await self.db.fetchrow(
                "SELECT * FROM profile_deletions WHERE profile_id = ?", record.profile_id)
            if row is None or row["operation_id"] != record.operation_id or row["phase"] != "purged":
                raise ProfileDeletionConflict(record.profile_id)
            await self.db.execute(
                "DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE profile_id = ?)",
                record.profile_id)
            await self.db.execute(
                "DELETE FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE profile_id = ?)",
                record.profile_id)
            await self.db.execute("DELETE FROM runs WHERE profile_id = ?", record.profile_id)
            await self.db.execute("DELETE FROM sessions WHERE profile_id = ?", record.profile_id)
            await self.db.execute("DELETE FROM blobs WHERE profile_id = ?", record.profile_id)
            await self.db.execute("DELETE FROM memories WHERE profile_id = ?", record.profile_id)
            await self.db.execute("DELETE FROM profiles WHERE id = ?", record.profile_id)

    async def complete_profile_deletion(self, profile_id: str, tenant_id: int,
                                        operation_id: str) -> None:
        """Release this exact operation's fence and keep a minimal completion receipt.

        A retried completion for an already acknowledged operation is a no-op
        that cannot touch a newer fence or a newer profile incarnation.
        """
        async with self.db.transaction():
            row = await self.db.fetchrow(
                "SELECT * FROM profile_deletions WHERE profile_id = ?", profile_id)
            if row is not None:
                if int(row["tenant_id"]) != int(tenant_id):
                    raise ProfileTenantMismatch(profile_id)
                if row["operation_id"] != operation_id:
                    receipt = await self.db.fetchrow(
                        "SELECT 1 AS done FROM profile_deletion_receipts "
                        "WHERE profile_id = ? AND operation_id = ?", profile_id, operation_id)
                    if receipt is not None:
                        return
                    raise ProfileDeletionConflict(profile_id)
                if row["phase"] != "purged":
                    raise ProfileDeletionConflict(profile_id)
                await self.db.execute(
                    "DELETE FROM profile_deletions WHERE profile_id = ? AND operation_id = ?",
                    profile_id, operation_id)
                await self.db.execute(
                    "INSERT INTO profile_deletion_receipts "
                    "(profile_id, tenant_id, operation_id, completed_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (profile_id, operation_id) DO NOTHING",
                    profile_id, int(tenant_id), operation_id, self.clock())
                return
            receipt = await self.db.fetchrow(
                "SELECT tenant_id FROM profile_deletion_receipts WHERE profile_id = ? AND operation_id = ?",
                profile_id, operation_id)
            if receipt is not None:
                if int(receipt["tenant_id"]) != int(tenant_id):
                    raise ProfileTenantMismatch(profile_id)
                return
            raise ProfileDeletionConflict(profile_id)


def _record(row: dict) -> DeletionRecord:
    return DeletionRecord(row["profile_id"], int(row["tenant_id"]), row["operation_id"], row["phase"])
