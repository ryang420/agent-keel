"""Sandbox preparation, conditional publication and ownership loss (task 7)."""

import asyncio
from dataclasses import replace
import hashlib
import json

import pytest

import keel.sandbox.runtime as sandbox_runtime
from keel.sandbox.base import SandboxError, parse_policy
from keel.sandbox.coordinator import SandboxCoordinator
from keel.sandbox.records import (
    DockerOutcome, SandboxBusy, SandboxLeaseLost, SandboxUncertain, UsageBinding,
)
from keel.sandbox.runtime import HELPER_MANIFEST_NAME, HELPER_MANIFEST_VERSION, HELPER_PROTOCOL_VERSION
from tests.sandbox_support import (
    DEFAULT_SANDBOX, FakeDocker, TransactionProbe, container_id, make_claim, queue_run, seed_profile,
    seed_ready_sandbox,
)

DEPLOYMENT = "dep"
DAEMON = "daemon-1"


@pytest.fixture
def helper_assets(tmp_path, monkeypatch):
    """Synthetic helper artifacts, so these tests never read the gitignored built binaries.

    The manifest and both architecture files are written into a temporary directory and `artifact_directory` is
    redirected there; the returned mapping holds each architecture's digest.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    digests = {}
    artifacts = {}
    for architecture in ("amd64", "arm64"):
        data = f"synthetic keel supervisor for {architecture}".encode()
        path = directory / f"keel-supervisor-linux-{architecture}"
        path.write_bytes(data)
        path.chmod(0o755)
        digests[architecture] = hashlib.sha256(data).hexdigest()
        artifacts[architecture] = {"file": path.name, "sha256": digests[architecture]}
    manifest = {"version": HELPER_MANIFEST_VERSION, "protocol": HELPER_PROTOCOL_VERSION,
                "artifacts": artifacts}
    (directory / HELPER_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(sandbox_runtime, "artifact_directory", lambda: directory)
    return digests


@pytest.fixture
async def coordination(store, clock, helper_assets):
    """A coordinator on a database-time-aligned clock, with an initialized fake Docker backend."""
    clock.now = await store.db.database_now()
    store.coordination_clock = clock
    backend = FakeDocker(daemon_id=DAEMON, architecture="amd64")
    coordinator = SandboxCoordinator(
        store, backend, deployment=DEPLOYMENT, owner="w1", lease_seconds=30.0, idle_seconds=1800.0,
        reap_seconds=5.0)
    await coordinator.initialize()
    return coordinator, backend, store, clock


async def _sandbox_row(store):
    return await store.db.fetchrow(
        "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")


async def test_concurrent_first_claims_select_one_preparation_owner(coordination):
    coordinator, backend, store, clock = coordination
    await seed_profile(store)
    first = await queue_run(store, "s1")
    clock.now += 1
    second = await queue_run(store, "s2")

    claims = await asyncio.gather(coordinator.claim(), coordinator.claim())
    claimed = [claim for claim in claims if claim is not None]

    assert len(claimed) == 1
    assert claimed[0].run.id == first.id
    assert claimed[0].use is not None
    usage = await store.get_usage(claimed[0].use.usage_id)
    assert usage.state == "preparing" and usage.binding is None and usage.create_operation_id is None
    assert (await store.get_run_by_id(second.id)).status == "queued"
    assert backend.calls == []


async def test_prepare_creates_installs_starts_and_publishes_one_generation(coordination):
    coordinator, backend, store, _ = coordination
    claim = await make_claim(store, "s1")
    exact = container_id()
    backend._containers.append(exact)
    probe = TransactionProbe(store.db)
    store.db = probe
    backend._depth = lambda: probe.depth

    bound = await coordinator.prepare(claim.use)

    assert backend.calls == ["create", "install_helper", "start"]
    assert [observation[1] for observation in backend.observations] == [0, 0, 0]
    assert probe.max_depth > 0  # the guarded transactions did run
    assert bound.binding.container_id == exact == backend.created_ids[0]
    assert bound.binding.generation == 1
    assert bound.binding.policy == parse_policy(DEFAULT_SANDBOX, mount_root="/")
    usage = await store.get_usage(bound.use.usage_id)
    assert usage.state == "active"
    assert usage.binding == UsageBinding(
        bound.binding.generation, bound.binding.generation_token, bound.binding.container_id,
        bound.binding.container_name)
    sandbox = await _sandbox_row(store)
    assert sandbox["state"] == "ready"
    assert sandbox["container_id"] == exact
    assert sandbox["generation_token"] == bound.binding.generation_token
    assert sandbox["policy_digest"] == bound.binding.policy.digest()
    assert sandbox["helper_digest"] == bound.binding.helper_digest
    assert sandbox["current_operation_id"] is None and sandbox["idle_since"] is None
    assert (await store.db.fetchrow("SELECT COUNT(*) AS n FROM sandboxes WHERE state = 'ready'"))["n"] == 1
    assert (await store.db.fetchrow("SELECT COUNT(*) AS n FROM sandbox_uses WHERE state = 'active'"))["n"] == 1
    start = backend.attempts["start"][0]
    assert start.original_create_operation_id == backend.attempts["create"][0].id
    assert start.container_id == exact


async def test_a_later_claim_shares_the_published_generation_without_new_docker_calls(coordination):
    coordinator, backend, store, _ = coordination
    first = await make_claim(store, "s1")
    published = await coordinator.prepare(first.use)
    await queue_run(store, "s2")

    second = await coordinator.claim()
    assert second is not None and second.use is not None
    assert (await store.get_usage(second.use.usage_id)).state == "active"
    shared = await coordinator.prepare(second.use)

    assert shared.binding == published.binding
    assert backend.calls == ["create", "install_helper", "start"]


async def test_policy_mismatch_does_not_replace_an_existing_target(coordination):
    coordinator, backend, store, _ = coordination
    await seed_ready_sandbox(store)
    claim = await make_claim(store, "s1", sandbox={"image": "other", "workdir": "/w"})
    before = await _sandbox_row(store)

    with pytest.raises(SandboxBusy):
        await coordinator.prepare(claim.use)

    assert backend.calls == []
    after = await _sandbox_row(store)
    assert after == before
    assert (await store.get_usage(claim.use.usage_id)).state == "preparing"


async def test_an_expired_usage_never_dispatches_anything(coordination):
    coordinator, backend, store, clock = coordination
    claim = await make_claim(store, "s1")
    clock.now += 61

    with pytest.raises(SandboxLeaseLost):
        await coordinator.prepare(claim.use)

    assert backend.calls == []
    assert (await store.get_usage(claim.use.usage_id)).state == "preparing"


@pytest.mark.parametrize("phase", ["create", "install_helper", "start"])
async def test_usage_expiry_during_a_phase_stops_publication(coordination, phase):
    coordinator, backend, store, clock = coordination
    claim = await make_claim(store, "s1")
    barrier = backend.hold_next(phase)
    task = asyncio.create_task(coordinator.prepare(claim.use))
    await asyncio.wait_for(barrier.entered.wait(), 5)
    clock.now += 61  # past the usage lease
    barrier.release.set()

    with pytest.raises(SandboxLeaseLost):
        await asyncio.wait_for(task, 5)

    assert (await _sandbox_row(store))["state"] != "ready"
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "preparing" and usage.binding is None
    assert (await store.db.fetchrow("SELECT COUNT(*) AS n FROM sandbox_uses WHERE state = 'active'"))["n"] == 0


async def test_a_delayed_create_after_retirement_never_starts_or_clears_the_new_generation(coordination):
    coordinator, backend, store, _ = coordination
    first = await make_claim(store, "s1", owner="w1")
    first_id, second_id = container_id(), container_id()
    backend._containers.extend([first_id, second_id])
    barrier = backend.hold_next("create")
    delayed = asyncio.create_task(coordinator.prepare(first.use))
    await asyncio.wait_for(barrier.entered.wait(), 5)
    create_id = (await store.get_usage(first.use.usage_id)).create_operation_id
    assert create_id is not None

    # Section 11.1 retires the still-unbound preparation while its create is in flight.
    assert await store.retire_unbound_preparation(first.use, abandon=True) is True
    assert (await store.get_usage(first.use.usage_id)).state == "released"

    second = await make_claim(store, "s2", owner="w2")
    published = await coordinator.prepare(second.use)
    assert published.binding.container_id == second_id
    barrier.release.set()

    with pytest.raises(SandboxUncertain):
        await asyncio.wait_for(delayed, 5)

    # The late result is retained as evidence on its retired attempt and starts nothing.
    assert backend.calls == ["create", "create", "install_helper", "start"]
    assert backend.calls.count("start") == 1
    stale = await store.get_operation(create_id)
    assert stale.authority == "retired"
    assert stale.outcome == "dispatched"
    assert stale.discovered_container_id == first_id
    starts = await store.db.fetch(
        "SELECT id FROM sandbox_operations WHERE kind = 'start' AND original_create_operation_id = ?", create_id)
    assert starts == []
    sandbox = await _sandbox_row(store)
    assert sandbox["state"] == "ready"
    assert sandbox["container_id"] == second_id
    assert sandbox["generation_token"] == published.binding.generation_token


async def test_an_authorized_late_start_blocks_retirement_and_still_publishes(coordination):
    coordinator, backend, store, _ = coordination
    claim = await make_claim(store, "s1")
    barrier = backend.hold_next("start")
    task = asyncio.create_task(coordinator.prepare(claim.use))
    await asyncio.wait_for(barrier.entered.wait(), 5)

    assert await store.retire_unbound_preparation(claim.use, abandon=True) is False
    barrier.release.set()
    bound = await asyncio.wait_for(task, 5)

    assert bound.binding.container_id == backend.created_ids[0]
    assert (await store.get_usage(claim.use.usage_id)).state == "active"
    assert (await _sandbox_row(store))["state"] == "ready"
    assert await store.retire_unbound_preparation(claim.use, abandon=True) is False


async def test_an_uncertain_start_is_not_published_and_blocks_reuse(coordination):
    coordinator, backend, store, _ = coordination
    claim = await make_claim(store, "s1")
    backend.set_result("start", DockerOutcome("uncertain", diagnostic="the start timed out"))
    await queue_run(store, "s2")

    with pytest.raises(SandboxUncertain):
        await coordinator.prepare(claim.use)

    sandbox = await _sandbox_row(store)
    assert sandbox["state"] == "creating" and sandbox["container_id"] is None
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "preparing" and usage.binding is None
    assert await coordinator.claim() is None
    assert await store.retire_unbound_preparation(claim.use, abandon=True) is False


async def test_a_ready_generation_binds_in_the_claim_and_needs_no_docker(coordination):
    coordinator, backend, store, _ = coordination
    await seed_profile(store)
    generation = await seed_ready_sandbox(store)
    await queue_run(store, "s1")

    claim = await coordinator.claim()
    assert claim is not None and claim.use is not None
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "active" and usage.create_operation_id is None
    bound = await coordinator.prepare(claim.use)

    assert backend.calls == []
    assert bound.binding.container_id == generation["container_id"]
    assert bound.binding.generation == generation["generation"]
    assert bound.binding.policy == generation["policy"]
    stamp = await coordinator.renew_usage(claim.use)
    assert stamp is not None and stamp.expires_at > stamp.observed_at


async def test_initialize_fails_closed_when_the_helper_cannot_serve_the_architecture(store, clock, helper_assets):
    clock.now = await store.db.database_now()
    coordinator = SandboxCoordinator(
        store, FakeDocker(architecture="mips"), deployment=DEPLOYMENT, owner="w1", lease_seconds=30.0,
        idle_seconds=1800.0, reap_seconds=5.0)
    with pytest.raises(SandboxError, match="unsupported Docker architecture"):
        await coordinator.initialize()


async def test_a_generation_published_with_the_same_helper_is_reused_without_docker(coordination, helper_assets):
    coordinator, backend, store, _ = coordination
    await seed_profile(store)
    generation = await seed_ready_sandbox(store, helper_digest=helper_assets["amd64"])
    await queue_run(store, "s1")

    claim = await coordinator.claim()
    assert claim is not None and claim.use is not None
    assert (await store.get_usage(claim.use.usage_id)).state == "active"
    bound = await coordinator.prepare(claim.use)

    assert backend.calls == []
    assert bound.binding.container_id == generation["container_id"]
    assert bound.binding.helper_digest == helper_assets["amd64"]


async def test_a_generation_published_with_another_helper_is_not_reused(coordination, helper_assets):
    coordinator, backend, store, _ = coordination
    await seed_profile(store)
    await seed_ready_sandbox(store, helper_digest="0" * 64)
    await queue_run(store, "s1")

    claim = await coordinator.claim()
    assert claim is not None and claim.use is not None
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "preparing" and usage.binding is None
    with pytest.raises(SandboxBusy):
        await coordinator.prepare(claim.use)

    assert backend.calls == []
    assert (await store.get_usage(claim.use.usage_id)).state == "preparing"


async def test_a_generation_published_for_another_helper_protocol_is_not_reused(coordination):
    coordinator, backend, store, _ = coordination
    await seed_profile(store)
    await seed_ready_sandbox(store, helper_protocol=2)
    await queue_run(store, "s1")

    claim = await coordinator.claim()
    assert claim is not None and claim.use is not None
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "preparing" and usage.binding is None
    with pytest.raises(SandboxBusy):
        await coordinator.prepare(claim.use)

    assert backend.calls == []


async def test_a_create_observation_without_the_exact_labels_is_not_published(coordination):
    coordinator, backend, store, _ = coordination
    claim = await make_claim(store, "s1")
    backend.observe = lambda kind, observation: (
        replace(observation, labels=tuple(
            pair for pair in observation.labels if pair[0] != "keel.managed"))
        if kind == "create" else observation)

    with pytest.raises(SandboxUncertain):
        await coordinator.prepare(claim.use)

    assert backend.calls == ["create"]
    assert (await _sandbox_row(store))["state"] == "creating"


async def test_a_start_observation_with_a_foreign_generation_token_is_not_published(coordination):
    coordinator, backend, store, _ = coordination
    claim = await make_claim(store, "s1")
    backend.observe = lambda kind, observation: (
        replace(observation, labels=tuple(
            (key, "0" * 32) if key == "keel.generation-token" else (key, value)
            for key, value in observation.labels))
        if kind == "start" else observation)

    with pytest.raises(SandboxUncertain):
        await coordinator.prepare(claim.use)

    assert backend.calls == ["create", "install_helper", "start"]
    assert (await _sandbox_row(store))["state"] == "creating"


async def test_an_observation_without_the_published_policy_digest_is_not_published(coordination):
    coordinator, backend, store, _ = coordination
    claim = await make_claim(store, "s1")
    backend.observe = lambda kind, observation: (
        replace(observation, policy_digest=None) if kind == "create" else observation)

    with pytest.raises(SandboxError):
        await coordinator.prepare(claim.use)

    assert backend.calls == ["create"]
    assert (await _sandbox_row(store))["state"] == "creating"


async def test_a_late_result_records_its_diagnostic_on_the_retired_attempt(coordination):
    coordinator, backend, store, _ = coordination
    claim = await make_claim(store, "s1", owner="w1")
    backend.set_result("create", DockerOutcome("uncertain", diagnostic="the create timed out"))
    barrier = backend.hold_next("create")
    delayed = asyncio.create_task(coordinator.prepare(claim.use))
    await asyncio.wait_for(barrier.entered.wait(), 5)
    create_id = (await store.get_usage(claim.use.usage_id)).create_operation_id
    assert create_id is not None
    assert await store.retire_unbound_preparation(claim.use, abandon=True) is True

    barrier.release.set()
    with pytest.raises(SandboxUncertain):
        await asyncio.wait_for(delayed, 5)

    stale = await store.get_operation(create_id)
    assert stale.authority == "retired" and stale.outcome == "dispatched"
    assert stale.result_message == "the create timed out"
    assert stale.discovered_container_id is None


async def test_cancellation_between_a_recorded_start_and_publication_publishes_nothing(
        coordination, monkeypatch):
    coordinator, backend, store, _ = coordination
    claim = await make_claim(store, "s1")

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(store, "publish_generation", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await coordinator.prepare(claim.use)

    assert backend.calls == ["create", "install_helper", "start"]
    start = backend.attempts["start"][0]
    assert (await store.get_operation(start.id)).outcome == "succeeded"
    sandbox = await _sandbox_row(store)
    assert sandbox["state"] == "creating" and sandbox["container_id"] is None
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "preparing" and usage.binding is None
