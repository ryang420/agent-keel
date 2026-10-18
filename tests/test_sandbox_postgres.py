"""PostgreSQL races between independent keel instances sharing one database and one Docker daemon (design §16).

Each instance is its own store (its own connection pool) and its own `SandboxCoordinator`; they share one test
daemon double. Races are released together by an `asyncio.Barrier`, and in-flight Docker calls are held by explicit
barriers, never by sleeps. Lease expiry is observed through an injected coordination clock on the recovering
instance only (host-clock skew), so the other instances keep using database time.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from dataclasses import dataclass

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.sandbox.base import SandboxError
from keel.sandbox.coordinator import SandboxCoordinator
from keel.store import open_store
from tests.sandbox_support import (
    LocalDocker, absence_verified_before_create, install_helper_assets, queue_run,
)

DSN = os.environ.get("KEEL_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(DSN is None, reason="needs KEEL_TEST_POSTGRES_DSN")
DEPLOYMENT, DAEMON = "dep", "daemon-1"
LEASE = 30.0
REPEATS = 6
TERMINAL_EVENTS = ("run.completed", "run.failed", "run.cancelled")


class Skew:
    """A frozen coordination clock: database time read once, shifted by `seconds` (injected host-clock skew)."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class Instance:
    store: object
    coordinator: SandboxCoordinator

    @contextmanager
    def skewed(self, now: float):
        self.store.coordination_clock = Skew(now)
        try:
            yield
        finally:
            self.store.coordination_clock = None


@dataclass
class Cluster:
    a: Instance
    b: Instance
    c: Instance
    backend: LocalDocker
    sandbox: dict

    async def seed(self, profile_id: str, *, sandbox: dict | None = None,
                   models: list[dict] | None = None) -> None:
        await self.a.store.upsert_profile(profile_id, models=models or [{"provider": "fake", "model": "m"}],
                                          sandbox=self.sandbox if sandbox is None else sandbox, soul="",
                                          skills=[])

    async def later(self, seconds: float) -> float:
        """Database time `seconds` from now, for the recovering instance's skewed clock."""
        return await self.c.store.db.database_now() + seconds


async def reset_schema() -> None:
    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS sandbox_execs, sandbox_operations, sandbox_uses, sandboxes, "
                           "schema_version, memories, run_events, runs, messages, sessions, profiles, tenants, "
                           "blobs")
    finally:
        await conn.close()


@pytest.fixture
async def cluster(tmp_path, monkeypatch):
    install_helper_assets(tmp_path / "bin", monkeypatch)
    await reset_schema()
    backend = LocalDocker(daemon_id=DAEMON)
    instances = []
    try:
        for owner in ("instance-a", "instance-b", "instance-c"):
            store = await open_store(DSN)
            coordinator = SandboxCoordinator(store, backend, deployment=DEPLOYMENT, owner=owner, lease_seconds=LEASE,
                                             idle_seconds=10.0, reap_seconds=1.0)
            instances.append(Instance(store, coordinator))
            assert await coordinator.open_admission()
        yield Cluster(*instances, backend, {"image": "local", "workdir": str(tmp_path / "work")})
    finally:
        for instance in instances:
            await instance.store.close()


async def race(*calls):
    barrier = asyncio.Barrier(len(calls))

    async def run(call):
        await barrier.wait()
        return await call()

    return await asyncio.gather(*(run(call) for call in calls), return_exceptions=True)


async def terminal_event_count(store, run_id: str) -> int:
    return sum(event.type in TERMINAL_EVENTS for event in await store.events_after(run_id, 0))


async def sandbox_row(store, profile_id: str) -> dict | None:
    return await store.db.fetchrow(
        "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, profile_id)


async def published_generation_count(store, profile_id: str) -> int:
    """Distinct generations any usage of the profile was ever bound to: only publication binds a usage."""
    row = await store.db.fetchrow(
        "SELECT COUNT(DISTINCT generation_token) AS n FROM sandbox_uses WHERE deployment = ? AND profile_id = ? "
        "AND generation_token IS NOT NULL", DEPLOYMENT, profile_id)
    return row["n"]


async def finish(instance: Instance, claim) -> None:
    """End a claimed run the way its executor does: terminal transition, then a guarded usage release."""
    await instance.store.finish_run(claim.run.id, "completed", output="ok")
    if claim.use is not None:
        assert await instance.coordinator.release(claim.use)


async def test_concurrent_first_claims_on_two_instances_publish_one_generation(cluster):
    a, b, backend = cluster.a, cluster.b, cluster.backend
    for attempt in range(REPEATS):
        profile = f"first-{attempt}"
        await cluster.seed(profile)
        for session in ("one", "two"):
            await queue_run(a.store, f"{profile}-{session}", profile_id=profile)
        claims = await race(a.coordinator.claim, b.coordinator.claim)
        assert not [error for error in claims if isinstance(error, BaseException)], claims
        owners = [(instance, claim) for instance, claim in zip((a, b), claims) if claim is not None]
        assert len(owners) == 1, "exactly one instance may own the first preparation"
        owner, first = owners[0]
        other = b if owner is a else a
        assert (await owner.store.get_usage(first.use.usage_id)).state == "preparing"
        assert await other.coordinator.claim() is None, "the reserved preparation is the profile's only owner"

        held = backend.hold_next("create")
        preparing = asyncio.create_task(owner.coordinator.prepare(first.use))
        await held.entered.wait()
        assert await other.coordinator.claim() is None, "a second claim cannot prepare while one is in flight"
        held.release.set()
        bound = await preparing
        second = await other.coordinator.claim()
        assert second is not None and second.use is not None
        shared = await other.coordinator.prepare(second.use)
        assert shared.binding.container_id == bound.binding.container_id
        assert await published_generation_count(a.store, profile) == 1
        await finish(owner, first)
        await finish(other, second)
    assert len(backend.created_ids) == REPEATS


async def test_idle_reaping_racing_a_claim_never_binds_the_removing_generation(cluster):
    a, b, backend = cluster.a, cluster.b, cluster.backend
    digest = a.coordinator._artifact.sha256
    outcomes = set()
    for attempt in range(REPEATS):
        profile = f"idle-{attempt}"
        await cluster.seed(profile)
        await queue_run(a.store, f"{profile}-one", profile_id=profile)
        first = await a.coordinator.claim()
        bound = await a.coordinator.prepare(first.use)
        await finish(a, first)  # the generation is now ready and idle
        await queue_run(a.store, f"{profile}-two", profile_id=profile)
        claim, actions = await race(
            lambda: a.store.claim_run_with_usage("instance-a", LEASE, deployment=DEPLOYMENT, daemon_id=DAEMON,
                                                 helper_digest=digest),
            lambda: b.store.plan_idle_removal(first.use.key, owner="instance-b", lease_seconds=LEASE,
                                              idle_seconds=0.0, helper_digest=digest))
        assert not isinstance(claim, BaseException) and not isinstance(actions, BaseException), (claim, actions)
        usage = None if claim is None else await a.store.get_usage(claim.use.usage_id)
        bound_to_old = usage is not None and usage.binding is not None and (
            usage.binding.container_id == bound.binding.container_id)
        assert not (bound_to_old and actions), "a claim bound a generation that the reaper is removing"
        if actions:
            outcomes.add("reaped")
            assert usage is None or usage.binding is None
            assert (await sandbox_row(a.store, profile))["state"] == "removing"
            for action in actions:
                assert await b.coordinator._recover_action(action) == 1
            assert bound.binding.container_id in backend.removed_ids
        else:
            outcomes.add("claimed")
            assert bound_to_old and usage.state == "active"
            assert bound.binding.container_id not in backend.removed_ids
        # A run skipped while the reaper held the profile is claimed once the removal has ended.
        claim = claim or await a.coordinator.claim()
        assert claim is not None and claim.run.profile_id == profile
        while claim is not None:
            if (await a.store.get_usage(claim.use.usage_id)).binding is None:
                await a.coordinator.prepare(claim.use)  # the reaped generation is replaced, never reused
            await finish(a, claim)
            claim = await a.coordinator.claim()
    assert outcomes  # which interleaving won is the database's choice; both are legal


def keel_app(store) -> httpx.AsyncClient:
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    bus = InMemoryEventBus()
    keel = Keel(store=store, bus=bus, providers=registry, executor=RunExecutor(store, bus, registry),
                credentials=(ServiceCredential(1, "key-a"),), max_running=1000)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(keel)), base_url="http://keel",
                             headers={"Authorization": "Bearer key-a"})


async def test_a_policy_update_racing_an_enqueue_on_another_instance_has_one_serial_outcome(cluster):
    old_models = [{"provider": "fake", "model": "m"}, {"provider": "fake", "model": "m2"}]
    replacement = {"models": [{"provider": "fake", "model": "m2"}], "soul": "", "skills": [],
                   "sandbox": {**cluster.sandbox, "env": {"POLICY": "new"}}}
    seen = set()
    async with keel_app(cluster.a.store) as left, keel_app(cluster.b.store) as right:
        for attempt in range(REPEATS):
            profile = f"policy-{attempt}"
            created = await left.put(f"/v1/profiles/{profile}", json={
                "models": old_models, "soul": "", "skills": [], "sandbox": cluster.sandbox})
            assert created.status_code == 200, created.text
            update, enqueue = await race(
                lambda: left.put(f"/v1/profiles/{profile}", json=replacement),
                lambda: right.post(f"/v1/profiles/{profile}/runs", json={
                    "input": "hi", "session_id": f"{profile}-s", "provider": "fake", "model": "m"}))
            pair = (update.status_code, enqueue.status_code)
            seen.add(pair)
            # The update removes the run's model and changes the policy. Serially, either the update commits
            # first and the run is validated against it (400), or the queued run makes the update busy (409).
            assert pair in {(200, 400), (409, 202)}, (update.text, enqueue.text)
            stored = await cluster.c.store.get_profile(profile)
            if pair == (409, 202):
                assert update.json()["error"]["code"] == "sandbox_busy"
                assert stored.sandbox == cluster.sandbox
                await cluster.c.store.finish_run(enqueue.json()["run_id"], "cancelled")
            else:
                assert enqueue.json()["error"]["code"] == "model_not_allowed"
                assert stored.sandbox == replacement["sandbox"]
                assert await cluster.c.store.list_runs(profile, ("queued",)) == []
    assert seen


async def test_a_policy_update_racing_a_claim_never_admits_the_stale_policy(cluster):
    a, c, backend = cluster.a, cluster.c, cluster.backend
    profile = "stale-policy"
    changed = {**cluster.sandbox, "env": {"POLICY": "new"}}
    async with keel_app(cluster.b.store) as right:
        body = {"models": [{"provider": "fake", "model": "m"}], "soul": "", "skills": [], "sandbox": changed}
        created = await right.put(f"/v1/profiles/{profile}", json={**body, "sandbox": cluster.sandbox})
        assert created.status_code == 200
        await queue_run(a.store, "warm", profile_id=profile)
        warm = await a.coordinator.claim()
        old = await a.coordinator.prepare(warm.use)
        await finish(a, warm)
        await queue_run(a.store, "racing", profile_id=profile)
        claim, update = await race(a.coordinator.claim, lambda: right.put(f"/v1/profiles/{profile}", json=body))
        assert update.status_code == 409 and update.json()["error"]["code"] == "sandbox_busy"  # a run exists
        assert claim is not None and claim.use is not None
        bound = await a.coordinator.prepare(claim.use)
        assert bound.binding.container_id == old.binding.container_id
        assert bound.binding.policy.digest() == old.binding.policy.digest()
        await finish(a, claim)

        assert (await right.put(f"/v1/profiles/{profile}", json=body)).status_code == 200
        await queue_run(a.store, "after", profile_id=profile)
        assert await a.coordinator.claim() is None, "the old-policy generation must not be shared"
        await c.coordinator.reconcile_once()  # recovery replaces the incompatible idle generation
        assert backend.removed_ids == [old.binding.container_id]
        fresh = await a.coordinator.claim()
        assert fresh is not None and (await a.store.get_usage(fresh.use.usage_id)).binding is None
        new = await a.coordinator.prepare(fresh.use)
        assert new.binding.policy.digest() != old.binding.policy.digest()
        assert absence_verified_before_create(backend.journal, old.binding.container_id, new.binding.container_id)
        await finish(a, fresh)


async def test_a_stale_owner_cannot_renew_or_publish_after_another_instance_recovers(cluster):
    a, b, c, backend = cluster.a, cluster.b, cluster.c, cluster.backend
    profile = "stale-owner"
    await cluster.seed(profile)
    await queue_run(a.store, "lost", profile_id=profile)
    lost = await a.coordinator.claim()
    held = backend.hold_next("create")
    preparing = asyncio.create_task(a.coordinator.prepare(lost.use))
    await held.entered.wait()  # the create reached Docker; instance A then stops renewing

    with c.skewed(await cluster.later(LEASE + 1)):
        await c.coordinator.reconcile_once()
    usage = await c.store.get_usage(lost.use.usage_id)
    assert (usage.state, usage.release_reason) == ("released", "preparation_retired_without_execution")
    assert await a.coordinator.renew_usage(lost.use) is None  # never resurrected
    assert await terminal_event_count(c.store, lost.run.id) == 1

    await queue_run(b.store, "replacement", profile_id=profile)
    replacement = await b.coordinator.claim()
    current = await b.coordinator.prepare(replacement.use)

    held.release.set()  # the old create answers late
    with pytest.raises(SandboxError):
        await preparing
    late = backend.created_ids[0]
    assert late != current.binding.container_id
    assert backend.running_ids == {current.binding.container_id}, "the stale create was never started"
    assert await published_generation_count(c.store, profile) == 1
    row = await sandbox_row(c.store, profile)
    assert (row["state"], row["container_id"]) == ("ready", current.binding.container_id)

    await c.coordinator.reconcile_once()  # the late container is quarantined and removed by its exact ID
    assert backend.removed_ids == [late]
    assert (await c.store.get_usage(replacement.use.usage_id)).state == "active"
    assert (await sandbox_row(c.store, profile))["container_id"] == current.binding.container_id
    await finish(b, replacement)


async def test_a_lost_commit_acknowledgement_never_dispatches_twice(cluster, monkeypatch):
    a, b, backend = cluster.a, cluster.b, cluster.backend
    profile = "lost-ack"
    await cluster.seed(profile)
    await queue_run(a.store, "first", profile_id=profile)
    first = await a.coordinator.claim()
    authorize = a.store.authorize_dispatch

    async def committed_then_lost(attempt):
        assert await authorize(attempt)
        raise ConnectionError("the commit acknowledgement was lost")

    monkeypatch.setattr(a.store, "authorize_dispatch", committed_then_lost)
    with pytest.raises(ConnectionError):
        await a.coordinator.prepare(first.use)
    monkeypatch.setattr(a.store, "authorize_dispatch", authorize)
    with pytest.raises(SandboxError, match="already has recorded preparation"):
        await a.coordinator.prepare(first.use)  # the original identities are re-read; nothing is repeated
    assert backend.calls == []
    create = await a.store.get_operation((await a.store.get_usage(first.use.usage_id)).create_operation_id)
    assert (create.kind, create.outcome) == ("create", "dispatched")
    assert await a.coordinator.release(first.use, abandon_preparation=True)
    assert (await a.store.get_operation(create.id)).resolved_at is None  # the tombstone stays unresolved

    await queue_run(b.store, "second", profile_id=profile)
    second = await b.coordinator.claim()
    bound = await b.coordinator.prepare(second.use)
    assert backend.calls.count("create") == 1 and backend.created_ids == [bound.binding.container_id]

    release = b.store.release_usage

    async def released_then_lost(use):
        assert await release(use)
        raise ConnectionError("the commit acknowledgement was lost")

    await b.store.finish_run(second.run.id, "completed", output="ok")
    monkeypatch.setattr(b.store, "release_usage", released_then_lost)
    with pytest.raises(ConnectionError):
        await b.coordinator.release(second.use)
    monkeypatch.setattr(b.store, "release_usage", release)
    released = await b.store.get_usage(second.use.usage_id)
    assert released.state == "released"
    assert not await b.coordinator.release(second.use)  # a retry finds the committed release, changes nothing
    assert (await b.store.get_usage(second.use.usage_id)).released_at == released.released_at
    report = await cluster.c.coordinator.reconcile_once()
    assert report.removed == 0 and backend.removed_ids == []
    assert (await sandbox_row(b.store, profile))["state"] == "ready"


async def test_an_orphan_on_one_instance_leaves_a_valid_user_on_another_running(cluster, tmp_path):
    a, b, c, backend = cluster.a, cluster.b, cluster.c, cluster.backend
    profile = "shared"
    await cluster.seed(profile)
    await cluster.seed("healthy", sandbox={})
    await queue_run(a.store, "dies", profile_id=profile)
    dying = await a.coordinator.claim()
    generation = await a.coordinator.prepare(dying.use)
    await a.store.begin_exec(generation, "x-unfinished")  # instance A dies with an exec admitted
    await queue_run(b.store, "survives", profile_id=profile)
    survivor = await b.coordinator.claim()
    shared = await b.coordinator.prepare(survivor.use)
    assert shared.binding.container_id == generation.binding.container_id

    gate = tmp_path / "release-survivor"
    command = ["sh", "-c", f'while [ ! -e "{gate}" ]; do sleep 0.02; done; echo survived']
    running = asyncio.create_task(
        b.coordinator.exec(shared, command, cwd=str(tmp_path), timeout_s=60, exec_id="x-survivor"))
    while not backend.local._groups.get("x-survivor"):
        await asyncio.sleep(0.01)  # wait for the survivor's process group to exist (not an ordering oracle)

    later = await cluster.later(LEASE + 1)
    assert await b.store.renew_usage(survivor.use, 10 * LEASE)  # B keeps renewing; A has stopped
    with c.skewed(later):
        await c.coordinator.reconcile_once()
    assert (await c.store.get_usage(dying.use.usage_id)).state == "orphaned"
    assert (await sandbox_row(c.store, profile))["state"] == "draining"
    assert backend.removed_ids == [], "a valid user's container is never removed"
    assert (await c.store.get_run_by_id(dying.run.id)).error_code == "sandbox_unavailable"
    assert await terminal_event_count(c.store, dying.run.id) == 1

    await queue_run(c.store, "waits", profile_id=profile)
    await queue_run(c.store, "healthy-run", profile_id="healthy")
    progressed = await c.coordinator.claim()
    assert progressed is not None and progressed.run.profile_id == "healthy"  # the draining profile is skipped
    assert await c.coordinator.claim() is None
    await c.store.finish_run(progressed.run.id, "completed", output="ok")

    assert not running.done()
    survivor_process_is_alive = backend.local._groups.get("x-survivor") is not None
    assert survivor_process_is_alive
    gate.write_text("")
    assert (await running).output.strip() == "survived"
    await finish(b, survivor)

    await c.coordinator.reconcile_once()
    assert backend.removed_ids == [generation.binding.container_id]
    assert (await c.store.get_usage(dying.use.usage_id)).state == "released"
    row = await c.store.db.fetchrow("SELECT state FROM sandbox_execs WHERE id = 'x-unfinished'")
    assert row["state"] == "retired"
    waiting = await c.coordinator.claim()
    assert waiting is not None and waiting.run.profile_id == profile
    replacement = await c.coordinator.prepare(waiting.use)
    old_id_absence_was_verified_before_new_publish = absence_verified_before_create(
        backend.journal, generation.binding.container_id, replacement.binding.container_id)
    assert old_id_absence_was_verified_before_new_publish
    await finish(c, waiting)


async def test_a_cancelled_preparation_retires_its_authority_and_its_late_create_is_removed(cluster):
    a, b, c, backend = cluster.a, cluster.b, cluster.c, cluster.backend
    profile = "cancelled"
    await cluster.seed(profile)
    await queue_run(a.store, "stopped", profile_id=profile)
    stopped = await a.coordinator.claim()
    held = backend.hold_next("create")
    preparing = asyncio.create_task(a.coordinator.prepare(stopped.use))
    await held.entered.wait()
    preparing.cancel()  # the run is stopped while its create is in flight
    with pytest.raises(asyncio.CancelledError):
        await preparing
    assert await a.coordinator.release(stopped.use, abandon_preparation=True)
    retired = await a.store.get_operation((await a.store.get_usage(stopped.use.usage_id)).create_operation_id)
    assert retired.authority == "retired"
    late = backend.created_ids[0]  # Docker finished the create although its caller was cancelled

    await queue_run(b.store, "next", profile_id=profile)
    nxt = await b.coordinator.claim()
    current = await b.coordinator.prepare(nxt.use)
    assert current.binding.container_id != late
    assert [start.container_id for start in backend.attempts["start"]] == [current.binding.container_id]
    assert backend.running_ids == {current.binding.container_id}

    await c.coordinator.reconcile_once()
    assert backend.removed_ids == [late]
    assert (await c.store.get_operation(retired.id)).confirmed_absent_at is not None
    assert (await sandbox_row(c.store, profile))["container_id"] == current.binding.container_id
    with pytest.raises(SandboxError):
        await a.store.begin_lifecycle(stopped.use, "start", target=current.binding, create_operation_id=retired.id,
                                      lease_seconds=LEASE)
    await finish(b, nxt)
