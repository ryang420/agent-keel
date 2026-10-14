import pytest

from keel.store import open_store
from keel.store.base import OwnershipError
from keel.store.ownership import require_tenant_ownership


async def _profile(store, profile_id, tenant_id=None):
    await store.upsert_profile(profile_id, models=[], sandbox={}, soul=profile_id, skills=[])
    if tenant_id is not None:
        await store.db.execute("UPDATE profiles SET tenant_id = ? WHERE id = ?", tenant_id, profile_id)


async def test_a_fresh_database_rejects_a_null_or_unknown_owner(store):
    await require_tenant_ownership(store.db, ("tenant-a",), {})
    with pytest.raises(Exception):
        await store.db.execute(
            "INSERT INTO profiles (id, models, sandbox, soul, skills, updated_at, memory, tenant_id) "
            "VALUES ('bare', '[]', '{}', '', '[]', 0, 0, NULL)")
    with pytest.raises(Exception):
        await store.db.execute(
            "INSERT INTO profiles (id, models, sandbox, soul, skills, updated_at, memory, tenant_id) "
            "VALUES ('bare', '[]', '{}', '', '[]', 0, 0, 'missing-tenant')")
    assert await store.get_profile("bare") is None


async def test_backfill_assigns_every_profile_or_changes_nothing(store):
    await _profile(store, "p1")
    await _profile(store, "p2")
    with pytest.raises(OwnershipError, match="KEEL_TENANT_BACKFILL"):
        await require_tenant_ownership(store.db, ("tenant-a",), {})
    assert (await store.get_profile("p1")).tenant_id is None
    assert await store.db.fetchrow("SELECT id FROM tenants") is None

    with pytest.raises(OwnershipError, match="every profile"):
        await require_tenant_ownership(store.db, ("tenant-a",), {"p1": "tenant-a"})
    assert (await store.get_profile("p1")).tenant_id is None

    with pytest.raises(OwnershipError, match="unknown profile"):
        await require_tenant_ownership(store.db, ("tenant-a",), {"p1": "tenant-a", "p2": "tenant-a", "nope": "tenant-a"})
    with pytest.raises(OwnershipError, match="not provisioned"):
        await require_tenant_ownership(store.db, ("tenant-a",), {"p1": "tenant-a", "p2": "other"})
    assert (await store.get_profile("p2")).tenant_id is None

    await require_tenant_ownership(store.db, ("tenant-a", "tenant-b"), {"p1": "tenant-a", "p2": "tenant-b"})
    assert (await store.get_profile("p1")).tenant_id == "tenant-a"
    assert (await store.get_profile("p2")).tenant_id == "tenant-b"
    await require_tenant_ownership(store.db, ("tenant-a", "tenant-b"), {})  # already complete

    with pytest.raises(OwnershipError, match="conflicts"):
        await require_tenant_ownership(store.db, ("tenant-a", "tenant-b"), {"p1": "tenant-b", "p2": "tenant-b"})
    assert (await store.get_profile("p1")).tenant_id == "tenant-a"
