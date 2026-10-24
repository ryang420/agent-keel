"""Assign every profile to a tenant in one transaction, then enforce the constraint.

A failed assignment rolls back, including tenant rows inserted for this attempt.
"""

from __future__ import annotations

from keel.store.base import OwnershipError
from keel.store.migrations import _PG_LOCK


async def require_tenant_ownership(db, tenant_ids: tuple[int, ...], assignment: dict[str, int]) -> None:
    async with db.transaction():
        await db.execute(_PG_LOCK)
        for tenant_id in dict.fromkeys(tenant_ids):
            await db.execute("INSERT INTO tenants (id) VALUES (?) ON CONFLICT (id) DO NOTHING", tenant_id)
        profiles = await db.fetch("SELECT id, tenant_id FROM profiles")
        provisioned_tenants = set(tenant_ids)
        by_id = {row["id"]: row["tenant_id"] for row in profiles}
        pending = [profile_id for profile_id, tenant_id in by_id.items() if tenant_id is None]
        if pending and not assignment:
            raise OwnershipError("profiles have no tenant and KEEL_TENANT_BACKFILL is empty")
        if assignment:
            unknown = [profile_id for profile_id in assignment if profile_id not in by_id]
            if unknown:
                raise OwnershipError("KEEL_TENANT_BACKFILL names an unknown profile")
            missing = [profile_id for profile_id in pending if profile_id not in assignment]
            if missing:
                raise OwnershipError("KEEL_TENANT_BACKFILL does not name every profile")
            for profile_id, tenant_id in assignment.items():
                if tenant_id not in provisioned_tenants:
                    raise OwnershipError("KEEL_TENANT_BACKFILL names a tenant that is not provisioned")
                current = by_id[profile_id]
                if current is not None and current != tenant_id:
                    raise OwnershipError("KEEL_TENANT_BACKFILL conflicts with a profile that already has a tenant")
            for profile_id in pending:
                await db.execute("UPDATE profiles SET tenant_id = ? WHERE id = ? AND tenant_id IS NULL",
                                 assignment[profile_id], profile_id)
        still = await db.fetch(
            "SELECT id FROM profiles WHERE tenant_id IS NULL OR tenant_id NOT IN (SELECT id FROM tenants)")
        if still:
            raise OwnershipError("profiles have no tenant and KEEL_TENANT_BACKFILL is empty")
        await _enforce(db)


async def _enforce(db) -> None:
    row = await db.fetchrow("SELECT 1 AS ok FROM pg_constraint WHERE conname = 'profiles_tenant_fk'")
    if row is None:
        await db.execute("ALTER TABLE profiles ALTER COLUMN tenant_id SET NOT NULL")
        await db.execute(
            "ALTER TABLE profiles ADD CONSTRAINT profiles_tenant_fk FOREIGN KEY (tenant_id) REFERENCES tenants (id)")
