"""Sandbox lifecycle acceptance against a real Docker daemon with the packaged supervisor (design §16).

Every container is created through `SandboxCoordinator` and the exact-ID `DockerSandbox` primitives, never by name.
Each test uses its own deployment label, and the fixture removes that deployment's containers by exact ID. The
packaged helper for the daemon's architecture must already be built (`scripts/build_supervisor.py`); a missing
helper fails the test rather than skipping it.

These cases need both `KEEL_TEST_DOCKER=1` and `KEEL_TEST_POSTGRES_DSN`: each instance has its own store
(and connection pool) over one database, as separate keel processes do, and both share this daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from keel.sandbox.base import SandboxError, TIMEOUT_EXIT_CODE, parse_policy, profile_mount_root
from keel.sandbox.coordinator import SandboxCoordinator
from keel.sandbox.docker import DockerSandbox
from keel.sandbox.records import SandboxUncertain
from keel.sandbox.runtime import helper_artifact
from keel.store import open_store
from tests.sandbox_support import queue_run, wait_until

needs_docker = pytest.mark.skipif(os.environ.get("KEEL_TEST_DOCKER") != "1", reason="set KEEL_TEST_DOCKER=1")
pytestmark = needs_docker
IMAGE = os.environ.get("KEEL_TEST_DOCKER_IMAGE", "python:3.12-slim")
MODELS = [{"provider": "fake", "model": "m"}]
LEASE = 30.0
TERMINAL_EVENTS = ("run.completed", "run.failed", "run.cancelled")


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def deployment_containers(deployment: str) -> list[str]:
    output = docker("ps", "-a", "--no-trunc", "-q", "--filter", "label=keel.managed=1",
                    "--filter", f"label=keel.deployment={deployment}")
    return sorted(output.split())


def exists(container_id: str) -> bool:
    return subprocess.run(["docker", "inspect", "--type", "container", container_id],
                          capture_output=True, timeout=60).returncode == 0


class Frozen:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@contextmanager
def skewed(store, now: float):
    """Run one recovery step with an injected coordination clock (host-clock skew) on this store only."""
    store.coordination_clock = Frozen(now)
    try:
        yield
    finally:
        store.coordination_clock = None


@pytest.fixture
def deployment():
    name = f"keel-test-{uuid.uuid4().hex[:12]}"
    yield name
    leftovers = deployment_containers(name)
    if leftovers:
        docker("rm", "-f", *leftovers)
    assert deployment_containers(name) == []


@pytest.fixture
def backend(deployment, tmp_path):
    backend = DockerSandbox(deployment=deployment, kill_grace_s=0.5, reap_seconds=2.0,
                            mount_root=str(tmp_path / "mounts"))
    architecture = docker("version", "--format", "{{.Server.Arch}}")
    try:
        helper_artifact(architecture)
    except SandboxError as exc:
        pytest.fail(f"build the packaged helper first (scripts/build_supervisor.py): {exc}")
    return backend


@pytest.fixture
async def single(pg_dsn):
    """One instance's independent connection pool over its isolated PostgreSQL database."""
    store = await open_store(pg_dsn)
    try:
        yield store, pg_dsn
    finally:
        await store.close()


def coordinator_for(store, backend, owner: str, *, idle_seconds: float = 1800.0) -> SandboxCoordinator:
    return SandboxCoordinator(store, backend, deployment=backend.deployment, owner=owner, lease_seconds=LEASE,
                              idle_seconds=idle_seconds, reap_seconds=2.0)


async def opened(store, backend, owner: str = "instance-a", **kwargs) -> SandboxCoordinator:
    coordinator = coordinator_for(store, backend, owner, **kwargs)
    assert await coordinator.open_admission()
    return coordinator


async def seed(store, profile_id: str, **sandbox) -> None:
    await store.upsert_profile(profile_id, models=MODELS, soul="", skills=[],
                               sandbox={"image": IMAGE, "workdir": "/work", **sandbox})


async def claim_and_prepare(coordinator, store, session: str, *, profile_id: str = "p1"):
    await queue_run(store, session, profile_id=profile_id)
    claim = await coordinator.claim()
    assert claim is not None and claim.run.profile_id == profile_id and claim.use is not None
    return claim, await coordinator.prepare(claim.use)


async def finish(coordinator, store, claim) -> None:
    await store.finish_run(claim.run.id, "completed", output="ok")
    assert await coordinator.release(claim.use)


async def run(coordinator, bound, command: str, *, exec_id: str | None = None, timeout_s: float = 60,
              cwd: str = "/work", env: dict | None = None):
    return await coordinator.exec(bound, ["sh", "-c", command], cwd=cwd, timeout_s=timeout_s,
                                  exec_id=exec_id or f"x-{uuid.uuid4().hex[:12]}", env=env)


async def file_exists(coordinator, bound, path: str) -> bool:
    """Readiness probe for a payload marker file, read through the coordinated file helper."""
    try:
        await coordinator.read_file(bound, path, 10)
    except FileNotFoundError:
        return False
    return True


async def exec_state(store, exec_id: str) -> str:
    return (await store.db.fetchrow("SELECT state FROM sandbox_execs WHERE id = ?", exec_id))["state"]


async def sandbox_row(store, deployment: str, profile_id: str = "p1") -> dict:
    return await store.db.fetchrow(
        "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", deployment, profile_id)


async def terminal_event_count(store, run_id: str) -> int:
    return sum(event.type in TERMINAL_EVENTS for event in await store.events_after(run_id, 0))


# --- one instance --------------------------------------------------------------------------------------------


async def test_coordinated_execs_run_by_exact_id_through_the_installed_helper(single, backend):
    store, _ = single
    coordinator = await opened(store, backend)
    await seed(store, "p1", user="1000:1000", env={"KEEL_V": "x"})
    claim, bound = await claim_and_prepare(coordinator, store, "s1")
    assert deployment_containers(backend.deployment) == [bound.binding.container_id]
    labels = json.loads(docker("inspect", "--format", "{{json .Config.Labels}}", bound.binding.container_id))
    assert labels["keel.generation-token"] == bound.binding.generation_token
    helper = docker("exec", bound.binding.container_id, "stat", "-c", "%u %g %a", "/.keel-runtime/keel-supervisor")
    assert helper == "0 0 755"

    result = await run(coordinator, bound, 'mkdir -p sub && cd sub && pwd && echo "$KEEL_V $E" && id -u && exit 3',
                       exec_id="x-cwd", cwd="/tmp", env={"E": "y"})
    assert result.exit_code == 3 and result.output.splitlines() == ["/tmp/sub", "x y", "1000"]
    assert await exec_state(store, "x-cwd") == "finished"

    await coordinator.write_file(bound, "/tmp/a/b.bin", b"\x00\x01hello")
    assert await coordinator.read_file(bound, "/tmp/a/b.bin", 100) == b"\x00\x01hello"
    with pytest.raises(FileNotFoundError):
        await coordinator.read_file(bound, "/tmp/missing", 10)
    with pytest.raises(IsADirectoryError):
        await coordinator.read_file(bound, "/tmp/a", 10)

    script = ("grep CapEff /proc/self/status; grep NoNewPrivs /proc/self/status; "
              'for d in /tmp /var/tmp; do touch $d/x && echo "$d ok"; done')
    out = (await run(coordinator, bound, script, cwd="/tmp")).output
    assert "NoNewPrivs:\t1" in out and "/tmp ok" in out and "/var/tmp ok" in out
    assert int(next(line for line in out.splitlines() if line.startswith("CapEff")).split()[1], 16) == 0
    await finish(coordinator, store, claim)


async def test_two_profiles_keep_private_files_and_env_separate(single, backend):
    store, _ = single
    coordinator = await opened(store, backend)
    left = Path(profile_mount_root(backend.mount_root, "p1"))
    right = Path(profile_mount_root(backend.mount_root, "p2"))
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    (left / "marker").write_text("from-left")
    (right / "marker").write_text("from-right")
    await seed(store, "p1", env={"KEEL_TOKEN": "value-left"},
               mounts=[{"source": str(left), "target": "/private"}])
    await seed(store, "p2", env={"KEEL_TOKEN": "value-right"},
               mounts=[{"source": str(right), "target": "/private"}])
    claim_a, bound_a = await claim_and_prepare(coordinator, store, "s1", profile_id="p1")
    claim_b, bound_b = await claim_and_prepare(coordinator, store, "s2", profile_id="p2")
    assert bound_a.binding.container_id != bound_b.binding.container_id
    await coordinator.write_file(bound_a, "/work/only-a", b"marker-a")
    result = await run(coordinator, bound_b, 'echo "$KEEL_TOKEN"; ls /work; cat /private/marker')
    assert "value-right" in result.output and "value-left" not in result.output
    assert "from-right" in result.output and "from-left" not in result.output
    assert "only-a" not in result.output and "marker-a" not in result.output
    await finish(coordinator, store, claim_a)
    await finish(coordinator, store, claim_b)


async def test_a_timeout_and_a_cancellation_end_the_whole_tree_through_the_supervisor(single, backend):
    store, _ = single
    coordinator = await opened(store, backend)
    await seed(store, "p1")
    claim, bound = await claim_and_prepare(coordinator, store, "s1")
    result = await run(coordinator, bound, "(sleep 2; touch /work/late) & sleep 30", exec_id="x-timeout",
                       timeout_s=1)
    assert result.timed_out and result.exit_code == TIMEOUT_EXIT_CODE
    assert await exec_state(store, "x-timeout") == "finished"

    started = asyncio.create_task(run(coordinator, bound, "touch /work/started; sleep 3; touch /work/cancelled",
                                      exec_id="x-cancel"))
    await wait_until(lambda: file_exists(coordinator, bound, "/work/started"), started)
    started.cancel()
    with pytest.raises(asyncio.CancelledError):
        await started
    assert await exec_state(store, "x-cancel") == "finished"  # the supervisor proved the tree gone
    check = await run(coordinator, bound, "sleep 3.5; ls /work")
    assert "late" not in check.output and "cancelled" not in check.output
    await finish(coordinator, store, claim)


async def test_descendants_are_adopted_and_an_escape_drains_the_generation(single, backend):
    store, _ = single
    coordinator = await opened(store, backend)
    await seed(store, "p1")
    claim, bound = await claim_and_prepare(coordinator, store, "s1")
    # Double fork with stdout closed: the intermediate shells exit at once, the sleeper is adopted.
    result = await run(coordinator, bound, '(sh -c "sleep 30" &) ; exec >&- 2>&-; exit 0', exec_id="x-adopt")
    assert result.exit_code == 0
    assert await exec_state(store, "x-adopt") == "finished"
    count = await run(coordinator, bound, 'for p in /proc/[0-9]*; do tr "\\0" " " < $p/cmdline; echo; done '
                                          '2>/dev/null | grep -c "^sleep 30" || true')
    assert count.output.strip() == "0"

    # A setsid descendant leaves the group and outlives the group KILL: cleanup is uncertain, never success.
    # The payload exits only once the escape has happened, so the group signal cannot win that race.
    escape = ("setsid sh -c 'touch /tmp/escaped; exec sleep 30' & "
              "while [ ! -e /tmp/escaped ]; do sleep 0.02; done; exit 0")
    with pytest.raises(SandboxUncertain):
        await run(coordinator, bound, escape, exec_id="x-escape")
    assert await exec_state(store, "x-escape") == "uncertain"
    assert (await sandbox_row(store, backend.deployment))["state"] == "draining"
    assert not await coordinator.release(claim.use)  # uncertain cleanup never releases the usage
    assert (await store.get_usage(claim.use.usage_id)).state != "released"
    await store.finish_run(claim.run.id, "completed", output="ok")

    with skewed(store, await store.db.database_now() + LEASE + 1):
        await coordinator.reconcile_once()  # the usage lapsed: the exact container is removed
    assert not exists(bound.binding.container_id)
    assert (await store.get_usage(claim.use.usage_id)).state == "released"
    assert await exec_state(store, "x-escape") == "retired"


async def test_mounted_data_survives_recovery_and_the_writable_layer_does_not(single, backend, tmp_path):
    store, _ = single
    coordinator = await opened(store, backend)
    data = Path(profile_mount_root(backend.mount_root, "p1")) / "data"
    data.mkdir(parents=True)
    await seed(store, "p1", mounts=[{"source": str(data), "target": "/data"}])
    first, bound = await claim_and_prepare(coordinator, store, "s1")
    await run(coordinator, bound, "echo kept > /data/keep; echo lost > /work/scratch")
    await finish(coordinator, store, first)

    docker("rm", "-f", bound.binding.container_id)  # an operator removes the container
    await coordinator.reconcile_once()
    assert (await sandbox_row(store, backend.deployment))["state"] == "absent"
    second, replacement = await claim_and_prepare(coordinator, store, "s2")
    assert replacement.binding.container_id != bound.binding.container_id
    assert replacement.binding.generation == bound.binding.generation + 1
    assert await coordinator.read_file(replacement, "/data/keep", 100) == b"kept\n"
    with pytest.raises(FileNotFoundError):
        await coordinator.read_file(replacement, "/work/scratch", 100)
    assert (data / "keep").read_text() == "kept\n"
    await finish(coordinator, store, second)

    # A policy change replaces the idle generation through recovery, keeping the mount.
    await seed(store, "p1", mounts=[{"source": str(data), "target": "/data"}], env={"POLICY": "2"})
    await coordinator.reconcile_once()
    assert not exists(replacement.binding.container_id)
    third, changed = await claim_and_prepare(coordinator, store, "s3")
    assert changed.binding.policy.digest() != replacement.binding.policy.digest()
    assert (await run(coordinator, changed, 'cat /data/keep; echo "$POLICY"')).output.split() == ["kept", "2"]
    await finish(coordinator, store, third)
    assert deployment_containers(backend.deployment) == [changed.binding.container_id]


async def test_a_container_removed_during_a_run_fails_the_use_instead_of_recreating(single, backend):
    store, _ = single
    coordinator = await opened(store, backend)
    await seed(store, "p1")
    claim, bound = await claim_and_prepare(coordinator, store, "s1")
    docker("rm", "-f", bound.binding.container_id)
    with pytest.raises(SandboxError):
        await run(coordinator, bound, "echo never", exec_id="x-gone")
    assert deployment_containers(backend.deployment) == [], "the old ID was never recreated or retried"
    assert await exec_state(store, "x-gone") == "uncertain"


async def test_delayed_exec_start_and_remove_reach_only_their_old_target(single, backend):
    store, _ = single
    coordinator = await opened(store, backend)
    await seed(store, "p1")
    old_claim, old = await claim_and_prepare(coordinator, store, "s1")
    delayed = await store.begin_exec(old, "x-delayed")  # admitted and committed, then held before dispatch
    old_start = next(attempt for attempt in [await store.get_operation(row["id"]) for row in await store.db.fetch(
        "SELECT id FROM sandbox_operations WHERE kind = 'start'")] if attempt.container_id == old.binding.container_id)

    with skewed(store, await store.db.database_now() + LEASE + 1):
        await coordinator.reconcile_once()  # the instance holding the exec died
    assert not exists(old.binding.container_id)
    remove = await store.db.fetchrow("SELECT id FROM sandbox_operations WHERE kind = 'remove'")
    old_remove = await store.get_operation(remove["id"])
    assert await exec_state(store, "x-delayed") == "retired"
    assert await terminal_event_count(store, old_claim.run.id) == 1

    new_claim, current = await claim_and_prepare(coordinator, store, "s2")
    with pytest.raises(SandboxError):
        await backend.exec_bound(delayed, ["sh", "-c", "touch /work/late"], cwd="/work", timeout_s=10)
    assert (await backend.start_id(old_start)).state == "definite_failure"
    assert (await backend.remove_id(old_remove)).state == "succeeded"  # the old ID stays absent
    assert deployment_containers(backend.deployment) == [current.binding.container_id]
    assert docker("inspect", "-f", "{{.State.Running}}", current.binding.container_id) == "true"
    with pytest.raises(FileNotFoundError):
        await coordinator.read_file(current, "/work/late", 10)
    await finish(coordinator, store, new_claim)


async def test_late_creates_after_a_restart_are_quarantined_in_reverse_order(single, backend):
    store, url = single
    coordinator = await opened(store, backend)
    await seed(store, "p1")
    retired = []
    for index in range(2):
        await queue_run(store, f"old-{index}")
        claim = await coordinator.claim()
        attempt = await store.begin_lifecycle(claim.use, "create", target=None, create_operation_id=None,
                                              lease_seconds=LEASE)
        assert await store.authorize_dispatch(attempt)  # dispatched, then the instance stalls before Docker
        with skewed(store, await store.db.database_now() + LEASE + 1):
            await coordinator.reconcile_once()
        assert (await store.get_usage(claim.use.usage_id)).release_reason == (
            "preparation_retired_without_execution")
        retired.append((claim, attempt))
    policy = await coordinator._profile_policy(retired[0][0].use)
    for _ in range(2):  # repeated empty scans and retention never settle an undelivered create
        await coordinator.reconcile_once()
    assert await store.prune_sandbox_records(0.0) >= 0
    for _, attempt in retired:
        assert (await store.get_operation(attempt.id)).resolved_at is None

    current_claim, current = await claim_and_prepare(coordinator, store, "current")
    restarted_store = await open_store(url)
    try:
        restarted = await opened(restarted_store, backend, "instance-restarted")
        late_ids = []
        for _, attempt in reversed(retired):  # deliver the old creates in reverse order
            outcome = await backend.create_generation(attempt, policy)
            assert outcome.state == "succeeded"
            late_ids.append(outcome.observation.container_id)
        for late in late_ids:
            assert docker("inspect", "-f", "{{.State.StartedAt}}", late).startswith("0001-01-01")
        await restarted.reconcile_once()
        for late in late_ids:
            assert not exists(late)
        assert deployment_containers(backend.deployment) == [current.binding.container_id]
        assert (await restarted_store.get_usage(current_claim.use.usage_id)).state == "active"
        row = await sandbox_row(restarted_store, backend.deployment)
        assert (row["state"], row["container_id"]) == ("ready", current.binding.container_id)
        for _, attempt in retired:
            assert (await restarted_store.get_operation(attempt.id)).confirmed_absent_at is not None
        assert (await run(coordinator, current, "echo alive")).output.strip() == "alive"
    finally:
        await restarted_store.close()
    await finish(coordinator, store, current_claim)


async def test_unknown_and_legacy_containers_block_only_their_profile_until_resolved(single, backend, tmp_path):
    store, _ = single
    data = Path(profile_mount_root(backend.mount_root, "p1")) / "legacy-data"
    data.mkdir(parents=True)
    (data / "keep").write_text("legacy\n")
    await seed(store, "p1", mounts=[{"source": str(data), "target": "/data"}])
    await seed(store, "p2")
    policy_p1 = (await store.get_profile("p1")).sandbox
    # A container left by the previous (uncoordinated) version: profile-derived name, no generation labels.
    legacy_policy = parse_policy(policy_p1, "/")
    legacy = docker("run", "-d", "--init", "--name", backend.container_name("p1"),
                    "--label", "keel.managed=1", "--label", f"keel.deployment={backend.deployment}",
                    "--label", "keel.profile=p1", "--label", f"keel.policy={legacy_policy.digest()}",
                    legacy_policy.image, "sleep", "infinity")
    coordinator = await opened(store, backend)
    await queue_run(store, "blocked", profile_id="p1")
    claim, healthy = await claim_and_prepare(coordinator, store, "healthy", profile_id="p2")
    assert await coordinator.claim() is None, "the profile with an unclassified container stays queued"
    assert exists(legacy), "an unclassified container is never removed automatically"
    assert (await sandbox_row(store, backend.deployment))["error_code"] == "sandbox_inventory_conflict"
    await finish(coordinator, store, claim)

    docker("rm", "-f", legacy)  # the operator's explicit reconciliation after stopping old writers
    await coordinator.reconcile_once()
    resumed = await coordinator.claim()
    assert resumed is not None and resumed.run.profile_id == "p1"
    bound = await coordinator.prepare(resumed.use)
    assert await coordinator.read_file(bound, "/data/keep", 100) == b"legacy\n"
    await finish(coordinator, store, resumed)


# --- two instances sharing one database and one daemon ------------------------------------------------------


async def test_two_instances_share_a_generation_and_recover_an_orphan_without_touching_the_survivor(
        backend, tmp_path, pg_dsn):
    stores = [await open_store(pg_dsn) for _ in range(3)]
    try:
        a, b, c = [await opened(store, backend, owner) for store, owner in zip(
            stores, ("instance-a", "instance-b", "instance-c"))]
        await seed(stores[0], "p1")
        for session in ("dies", "survives"):
            await queue_run(stores[0], session)
        first, second = await asyncio.gather(a.claim(), b.claim())
        claims = [(a, first), (b, second)]
        owner = next((co, cl) for co, cl in claims if cl is not None)
        waiter = next(co for co, cl in claims if co is not owner[0])
        assert sum(cl is not None for _, cl in claims) == 1, "exactly one first preparation owner"
        dying_coordinator, dying = owner
        generation = await dying_coordinator.prepare(dying.use)
        survivor = await waiter.claim()
        shared = await waiter.prepare(survivor.use)
        assert shared.binding.container_id == generation.binding.container_id
        published_generation_count = len(deployment_containers(backend.deployment))
        assert published_generation_count == 1

        gate = "/work/release-survivor"
        surviving = asyncio.create_task(run(
            waiter, shared, f"touch /work/survivor-started; while [ ! -e {gate} ]; do sleep 0.05; done; "
                            "echo survived", exec_id="x-survivor"))
        await dying_coordinator.store.begin_exec(generation, "x-orphaned")  # the dying instance's last exec
        await wait_until(lambda: file_exists(waiter, shared, "/work/survivor-started"), surviving)

        assert await waiter.store.renew_usage(survivor.use, 10 * LEASE)
        with skewed(c.store, await c.store.db.database_now() + LEASE + 1):
            await c.reconcile_once()
        assert (await c.store.get_usage(dying.use.usage_id)).state == "orphaned"
        assert (await sandbox_row(c.store, backend.deployment))["state"] == "draining"
        assert exists(generation.binding.container_id), "a valid user's container is never removed"
        assert await terminal_event_count(c.store, dying.run.id) == 1
        await queue_run(c.store, "queued-during-recovery")
        assert await c.claim() is None

        survivor_process_is_alive = not surviving.done() and exists(shared.binding.container_id)
        assert survivor_process_is_alive
        await waiter.write_file(shared, gate, b"")
        assert (await surviving).output.strip() == "survived"
        await finish(waiter, waiter.store, survivor)

        await c.reconcile_once()
        assert not exists(generation.binding.container_id)
        old_id_absence_was_verified_before_new_publish = (
            await c.store.db.fetchrow(
                "SELECT confirmed_absent_at FROM sandbox_operations WHERE kind = 'remove' AND container_id = ?",
                generation.binding.container_id))["confirmed_absent_at"] is not None
        assert old_id_absence_was_verified_before_new_publish
        nxt, replacement = await claim_and_prepare(c, c.store, "after-recovery")
        assert replacement.binding.container_id != generation.binding.container_id
        assert deployment_containers(backend.deployment) == [replacement.binding.container_id]
        with pytest.raises(FileNotFoundError):  # the writable layer went with the old generation
            await c.read_file(replacement, "/work/survivor-started", 10)
        await finish(c, c.store, nxt)
    finally:
        for store in stores:
            await store.close()


async def test_a_late_create_from_a_stale_instance_cannot_replace_the_published_generation(backend, pg_dsn):
    stores = [await open_store(pg_dsn) for _ in range(2)]
    try:
        a, b = [await opened(store, backend, owner) for store, owner in zip(stores, ("instance-a", "instance-b"))]
        await seed(stores[0], "p1")
        await queue_run(stores[0], "stale")
        stale = await a.claim()
        create = await a.store.begin_lifecycle(stale.use, "create", target=None, create_operation_id=None,
                                               lease_seconds=LEASE)
        assert await a.store.authorize_dispatch(create)  # instance A pauses before its create reaches Docker
        with skewed(b.store, await b.store.db.database_now() + LEASE + 1):
            await b.reconcile_once()
        nxt, current = await claim_and_prepare(b, b.store, "next")
        policy = await b._profile_policy(nxt.use)
        late = await backend.create_generation(create, policy)  # A resumes: its create arrives late
        assert late.state == "succeeded"
        recorded = await a.store.record_lifecycle_result(create, late)
        assert recorded.authority == "retired"
        with pytest.raises(SandboxError):
            await a.store.begin_lifecycle(stale.use, "install", target=current.binding,
                                          create_operation_id=create.id, lease_seconds=LEASE)
        await b.reconcile_once()
        assert not exists(late.observation.container_id)
        assert deployment_containers(backend.deployment) == [current.binding.container_id]
        assert (await sandbox_row(b.store, backend.deployment))["container_id"] == current.binding.container_id
        await finish(b, b.store, nxt)
    finally:
        for store in stores:
            await store.close()
