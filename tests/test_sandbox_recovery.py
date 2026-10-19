"""Restart, orphan and idle recovery through exact container identities (task 9)."""

import asyncio

import pytest

from keel.deletion import ProfileDeletionService
from keel.sandbox.coordinator import SandboxCoordinator
from keel.sandbox.records import ContainerObservation, DockerOutcome
from keel.sandbox.recovery import SandboxRecoveryMixin
from tests.sandbox_support import (FakeDocker, current_container_id, make_claim, queue_run, seed_profile)
from tests.test_sandbox_coordinator import helper_assets  # shared synthetic helper fixture


@pytest.fixture
async def recovery(store, clock, helper_assets):
    clock.now = await store.db.database_now()
    store.coordination_clock = clock
    backend = FakeDocker()
    coordinator = SandboxCoordinator(store, backend, deployment="dep", owner="w1", lease_seconds=30,
                                     idle_seconds=10, reap_seconds=1)
    assert await coordinator.open_admission()
    return coordinator, backend, store, clock


async def test_restart_retired_creates_reverse_arrival_preserves_current_user(recovery):
    coordinator, backend, store, clock = recovery
    retired = []
    for index in range(2):
        claim = await make_claim(store, f"old-{index}")
        attempt = await store.begin_lifecycle(claim.use, "create", target=None,
                                              create_operation_id=None, lease_seconds=30)
        assert await store.authorize_dispatch(attempt)
        await store.record_lifecycle_result(attempt, DockerOutcome("uncertain"))
        assert await store.retire_unbound_preparation(claim.use, abandon=True)
        retired.append(attempt)
    current = await make_claim(store, "current")
    bound = await coordinator.prepare(current.use)
    restarted = SandboxCoordinator(store, backend, deployment="dep", owner="restart", lease_seconds=30,
                                   idle_seconds=10, reap_seconds=1)
    late_ids = []
    for attempt in reversed(retired):
        outcome = await backend.create_generation(attempt, bound.policy)
        late_ids.append(outcome.observation.container_id)
    report = await restarted.reconcile_once()
    assert set(backend.removed_ids) == set(late_ids)
    assert report.removed == 2
    assert await current_container_id(store, current.use.key) == bound.name
    assert (await store.get_usage(current.use.usage_id)).state == "active"
    for attempt in retired:
        saved = await store.get_operation(attempt.id)
        assert saved.confirmed_absent_at is not None
        await store.record_lifecycle_result(attempt, DockerOutcome("uncertain"))
    assert await current_container_id(store, current.use.key) == bound.name


async def test_stopped_healthy_preparation_is_protected(recovery):
    coordinator, backend, store, _ = recovery
    claim = await make_claim(store, "preparing")
    attempt = await store.begin_lifecycle(claim.use, "create", target=None,
                                          create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(attempt)
    result = await backend.create_generation(attempt, await coordinator._profile_policy(claim.use))
    await store.record_lifecycle_result(attempt, result)
    await coordinator.reconcile_once()
    assert backend.removed_ids == []
    assert (await store.get_usage(claim.use.usage_id)).state == "preparing"


async def test_unknown_inventory_blocks_admission_without_removal(recovery):
    coordinator, backend, store, _ = recovery
    claim = await make_claim(store, "current")
    bound = await coordinator.prepare(claim.use)
    observation = (await backend.inventory())[0]
    backend._created["f" * 64] = ("unknown", observation.labels, observation.policy_digest)
    report = await coordinator.reconcile_once()
    await queue_run(store, "later")
    assert await coordinator.claim() is None
    assert report.blocked >= 1
    assert backend.removed_ids == []
    assert await current_container_id(store, claim.use.key) == bound.name


async def test_orphan_waits_for_valid_user_then_retires_exec_without_pid_control(recovery):
    coordinator, backend, store, clock = recovery
    first = await make_claim(store, "first")
    bound = await coordinator.prepare(first.use)
    await store.begin_exec(bound, "unfinished")
    await queue_run(store, "second")
    second = await coordinator.claim()
    clock.now += 20
    assert await coordinator.renew_usage(second.use)
    clock.now += 11
    await coordinator.reconcile_once()
    assert backend.removed_ids == []
    assert (await store.get_usage(first.use.usage_id)).state == "orphaned"
    assert (await store.get_run_by_id(first.run.id)).error_code == "sandbox_unavailable"
    assert await coordinator.release(second.use)
    await coordinator.reconcile_once()
    assert backend.removed_ids == [bound.name]
    assert (await store.get_usage(first.use.usage_id)).state == "released"
    row = await store.db.fetchrow("SELECT state FROM sandbox_execs WHERE id = 'unfinished'")
    assert row["state"] == "retired"


async def test_unbound_no_create_arrival_releases_even_when_inventory_fails(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "abandoned")
    attempt = await store.begin_lifecycle(claim.use, "create", target=None,
                                          create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(attempt)
    async def unavailable():
        raise OSError("unreachable")
    backend.inventory = unavailable
    clock.now += 31
    report = await coordinator.reconcile_once()
    assert (await store.get_usage(claim.use.usage_id)).state == "released"
    assert (await store.get_operation(attempt.id)).authority == "retired"
    assert report.unresolved >= 1


async def test_idle_reapers_remove_once_and_keep_generation_counter(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "idle")
    bound = await coordinator.prepare(claim.use)
    assert await coordinator.release(claim.use)
    clock.now += 11
    await asyncio.gather(coordinator.reap_idle_once(), coordinator.reap_idle_once())
    assert backend.removed_ids == [bound.name]
    row = await store.db.fetchrow("SELECT * FROM sandboxes WHERE profile_id = 'p1'")
    assert row["state"] == "absent" and row["generation"] == 1


async def test_missing_bound_container_revokes_healthy_users(recovery):
    coordinator, backend, store, _ = recovery
    claim = await make_claim(store, "missing")
    bound = await coordinator.prepare(claim.use)
    backend._created.pop(bound.name)
    await coordinator.reconcile_once()
    assert (await store.get_usage(claim.use.usage_id)).state == "released"
    assert (await store.get_run_by_id(claim.run.id)).error_code == "sandbox_unavailable"
    assert await current_container_id(store, claim.use.key) is None


async def test_inspect_transport_failure_does_not_prove_absence(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "inspect-error")
    bound = await coordinator.prepare(claim.use)
    clock.now += 31
    async def uncertain(exact):
        return DockerOutcome("uncertain", diagnostic="transport failed")
    backend.inspect_id = uncertain
    await coordinator.reconcile_once()
    assert await current_container_id(store, claim.use.key) == bound.name
    assert (await store.get_usage(claim.use.usage_id)).state == "orphaned"
    assert backend.removed_ids == []


async def test_candidate_batches_progress_past_indefinite_tombstones(store, clock):
    clock.now = await store.db.database_now()
    store.coordination_clock = clock
    for index in range(4):
        claim = await make_claim(store, f"s-{index}", profile_id=f"p{index}")
        attempt = await store.begin_lifecycle(claim.use, "create", target=None,
                                              create_operation_id=None, lease_seconds=30)
        await store.authorize_dispatch(attempt)
        await store.retire_unbound_preparation(claim.use, abandon=True)
    batches = [await store.recovery_candidates(limit=1) for _ in range(4)]
    assert {batch[0].profile_id for batch in batches} == {"p0", "p1", "p2", "p3"}


@pytest.mark.parametrize("change", ["helper", "protocol", "policy"])
async def test_incompatible_generation_defers_claim_and_is_replaced_after_valid_users_finish(recovery, change):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "old-user")
    bound = await coordinator.prepare(claim.use)
    if change == "helper":
        await store.db.execute("UPDATE sandboxes SET helper_digest = ? WHERE profile_id = 'p1'", "0" * 64)
    elif change == "protocol":
        await store.db.execute("UPDATE sandboxes SET helper_protocol = 2 WHERE profile_id = 'p1'")
    else:
        # Configuration drift setup: the public policy API correctly rejects changes with active usage.
        await store.db.execute('UPDATE profiles SET sandbox = ? WHERE id = ?',
                               '{"image":"new-image","workdir":"/w"}', "p1")
    waiting = await queue_run(store, "waiting")
    await seed_profile(store, "other")
    other = await queue_run(store, "other-session", profile_id="other")
    picked = await coordinator.claim()
    assert picked.run.id == other.id
    assert (await store.get_run_by_id(waiting.id)).status == "queued"
    assert await store.get_run_usage(waiting.id) is None
    await coordinator.reconcile_once()
    assert await current_container_id(store, claim.use.key) == bound.name
    assert await coordinator.release(claim.use)
    await coordinator.reconcile_once()
    assert backend.removed_ids == [bound.name]
    replacement = await coordinator.claim()
    assert replacement.run.id == waiting.id
    fresh = await coordinator.prepare(replacement.use)
    assert fresh.binding.generation == 2 and fresh.name != bound.name


async def test_uncertain_remove_takeover_inspects_and_dispatches_new_attempt(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "uncertain-remove")
    bound = await coordinator.prepare(claim.use)
    clock.now += 31
    action = (await store.plan_recovery(claim.use.key, owner="lost", lease_seconds=30))[0]
    old = await store.get_operation(action.operation_id)
    assert await store.authorize_dispatch(old)
    clock.now += 31
    await coordinator.reconcile_once()
    assert backend.removed_ids == [bound.name]
    assert backend.attempts["remove"][0].id != old.id
    saved = await store.get_operation(old.id)
    assert saved.authority == "retired" and saved.confirmed_absent_at is not None


async def test_lost_remove_ack_is_settled_by_exact_missing_id(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "lost-ack")
    bound = await coordinator.prepare(claim.use)
    clock.now += 31
    remove = backend.remove_id
    async def lost(attempt):
        await remove(attempt)
        return DockerOutcome("uncertain", diagnostic="lost reply")
    backend.remove_id = lost
    report = await coordinator.reconcile_once()
    assert report.removed == 1
    assert await current_container_id(store, claim.use.key) is None
    assert (await store.get_usage(claim.use.usage_id)).state == "released"


async def test_lost_db_commit_ack_after_remove_recovers_without_redispatch(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "db-ack")
    bound = await coordinator.prepare(claim.use)
    clock.now += 31
    commit = store.commit_recovery
    failed = False
    async def lose_ack(action, outcome):
        nonlocal failed
        if action.action == "remove" and not failed:
            failed = True
            raise OSError("commit unavailable")
        return await commit(action, outcome)
    store.commit_recovery = lose_ack
    await coordinator.reconcile_once()
    assert backend.removed_ids == [bound.name]
    assert await current_container_id(store, claim.use.key) == bound.name
    clock.now += 31
    await coordinator.reconcile_once()
    assert backend.removed_ids == [bound.name]
    assert await current_container_id(store, claim.use.key) is None


@pytest.mark.parametrize("conflict", ["daemon", "generation-token", "name", "running-retired"])
async def test_conflicting_inventory_blocks_without_removing_a_valid_current_generation(recovery, conflict):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "valid")
    bound = await coordinator.prepare(claim.use)
    observation = (await backend.inventory())[0]
    if conflict == "daemon":
        backend.daemon_id = "different-daemon"
    elif conflict == "name":
        name, labels, digest = backend._created[bound.name]
        backend._created[bound.name] = ("wrong-name", labels, digest)
    elif conflict == "generation-token":
        name, labels, digest = backend._created[bound.name]
        labels = tuple((k, "wrong" if k == "keel.generation-token" else v) for k, v in labels)
        backend._created[bound.name] = (name, labels, digest)
    else:
        await store.db.execute(
            "UPDATE sandbox_operations SET authority = 'retired', retired_at = 1 WHERE kind = 'create'")
        await store.db.execute("DELETE FROM sandbox_operations WHERE kind IN ('start', 'install')")
    report = await coordinator.reconcile_once()
    assert report.blocked >= 1
    assert backend.removed_ids == []
    assert await current_container_id(store, claim.use.key) == bound.name
    assert (await store.get_usage(claim.use.usage_id)).state == "active"
    row = await store.db.fetchrow("SELECT * FROM sandboxes WHERE profile_id = 'p1'")
    if conflict == "daemon":
        assert row["error_code"] == "sandbox_daemon_mismatch"
    else:
        # The recorded exact ID keeps execution and cancellation authority while the profile drains.
        assert row["error_code"] in ("sandbox_inventory_conflict", "sandbox_retired_create_running")
        ticket = await store.begin_exec(bound, "still-valid")
        assert ticket.binding.container_id == bound.name


async def test_retired_late_create_after_policy_change_is_removed_while_new_generation_remains(recovery):
    coordinator, backend, store, clock = recovery
    old = await make_claim(store, "old-policy")
    policy = await coordinator._profile_policy(old.use)
    create = await store.begin_lifecycle(old.use, "create", target=None,
                                         create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(create)
    assert await store.retire_unbound_preparation(old.use, abandon=True)
    await store.finish_run(old.run.id, "cancelled")
    current = await make_claim(store, "new-policy", sandbox={"image": "new", "workdir": "/w"})
    bound = await coordinator.prepare(current.use)
    late = await backend.create_generation(create, policy)
    await coordinator.reconcile_once()
    assert backend.removed_ids == [late.observation.container_id]
    assert await current_container_id(store, current.use.key) == bound.name
    assert (await store.get_usage(current.use.usage_id)).state == "active"


async def test_recovery_terminal_event_notifies_once_and_bus_failure_is_advisory(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "terminal")
    await coordinator.prepare(claim.use)
    calls = []
    class Bus:
        async def notify(self, run_id):
            calls.append(run_id)
            raise OSError("notification unavailable")
    coordinator.bus = Bus()
    clock.now += 31
    await coordinator.reconcile_once()
    await coordinator.reconcile_once()
    assert calls == [claim.run.id]
    row = await store.db.fetchrow("SELECT COUNT(*) AS n FROM run_events WHERE run_id = ? AND type = ?",
                                  claim.run.id, "run.failed")
    assert row["n"] == 1


@pytest.mark.parametrize("label", ["keel.deployment", "keel.profile", "keel.managed", "keel.policy"])
async def test_changed_identity_labels_cannot_authorize_cleanup(recovery, label):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "label-conflict")
    bound = await coordinator.prepare(claim.use)
    name, labels, digest = backend._created[bound.name]
    labels = tuple((k, "wrong" if k == label else v) for k, v in labels)
    backend._created[bound.name] = (name, labels, "wrong" if label == "keel.policy" else digest)
    clock.now += 31
    await coordinator.reconcile_once()
    assert backend.removed_ids == []
    assert await current_container_id(store, claim.use.key) == bound.name
    row = await store.db.fetchrow("SELECT * FROM sandboxes WHERE profile_id = 'p1'")
    assert row["error_code"] == "sandbox_inventory_conflict"


async def test_released_recovery_usage_retries_terminal_commit_after_database_failure(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "terminal-db-loss")
    create = await store.begin_lifecycle(claim.use, "create", target=None,
                                         create_operation_id=None, lease_seconds=30)
    await store.authorize_dispatch(create)
    clock.now += 31
    original = store.finish_run
    async def broken(*args, **kwargs):
        raise OSError("database unavailable")
    store.finish_run = broken
    await coordinator.reconcile_once()
    assert (await store.get_usage(claim.use.usage_id)).state == "released"
    assert (await store.get_run_by_id(claim.run.id)).status == "running"
    store.finish_run = original
    await coordinator.reconcile_once()
    assert (await store.get_run_by_id(claim.run.id)).error_code == "sandbox_unavailable"


async def test_terminal_run_retention_does_not_prune_unresolved_create_origin(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "retained-origin")
    create = await store.begin_lifecycle(claim.use, "create", target=None,
                                         create_operation_id=None, lease_seconds=30)
    await store.authorize_dispatch(create)
    clock.now += 31
    await coordinator.reconcile_once()
    clock.now += 10000
    await store.prune_sandbox_records(0)
    assert await store.get_usage(claim.use.usage_id) is not None
    assert (await store.get_operation(create.id)).resolved_at is None


async def test_recovery_run_entrypoint_preserves_healthy_bound_user(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "healthy-run")
    bound = await coordinator.prepare(claim.use)
    await coordinator.recover_run(claim.run.id)
    assert backend.removed_ids == []
    assert await current_container_id(store, claim.use.key) == bound.name


async def test_idle_claim_and_removal_serialize_before_dispatch(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "idle-claim")
    bound = await coordinator.prepare(claim.use)
    assert await coordinator.release(claim.use)
    clock.now += 11
    run = await queue_run(store, "queued-after-idle")
    actions = await store.plan_idle_removal(claim.use.key, owner="w1", lease_seconds=30,
                                           idle_seconds=10, helper_digest=coordinator._artifact.sha256)
    assert len(actions) == 1
    assert await coordinator.claim() is None
    await coordinator._recover_action(actions[0])
    fresh = await coordinator.claim()
    assert fresh.run.id == run.id
    assert (await coordinator.prepare(fresh.use)).name != bound.name


async def test_unknown_extra_container_blocks_admission_but_preserves_current_exec_authority(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "protected-exec")
    bound = await coordinator.prepare(claim.use)
    observation = (await backend.inventory())[0]
    backend._created["f" * 64] = ("unknown", observation.labels, observation.policy_digest)
    await coordinator.reconcile_once()
    await queue_run(store, "later")
    assert await coordinator.claim() is None
    assert await coordinator.renew_usage(claim.use)
    ticket = await store.begin_exec(bound, "still-valid")
    assert ticket.binding.container_id == bound.name


async def test_removing_the_conflicting_container_releases_the_admission_block(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "current")
    bound = await coordinator.prepare(claim.use)
    observation = (await backend.inventory())[0]
    rogue = "f" * 64
    backend._created[rogue] = ("unknown", observation.labels, observation.policy_digest)
    await coordinator.reconcile_once()
    later = await queue_run(store, "later")
    assert await coordinator.claim() is None
    backend._created.pop(rogue)
    await coordinator.reconcile_once()
    fresh = await coordinator.claim()
    assert fresh is not None and fresh.run.id == later.id
    assert (await store.get_usage(fresh.use.usage_id)).binding.container_id == bound.name


async def test_a_restored_label_conflict_clears_when_the_container_is_verified_again(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "relabeled")
    bound = await coordinator.prepare(claim.use)
    name, labels, digest = backend._created[bound.name]
    broken = tuple((key, "wrong" if key == "keel.policy" else value) for key, value in labels)
    backend._created[bound.name] = (name, broken, "wrong")
    await coordinator.reconcile_once()
    row = await store.db.fetchrow("SELECT * FROM sandboxes WHERE profile_id = 'p1'")
    assert row["state"] == "draining" and row["error_code"] == "sandbox_inventory_conflict"
    backend._created[bound.name] = (name, labels, digest)
    await coordinator.reconcile_once()
    row = await store.db.fetchrow("SELECT * FROM sandboxes WHERE profile_id = 'p1'")
    assert row["state"] == "ready" and row["error_code"] is None
    await queue_run(store, "later")
    assert await coordinator.claim() is not None


async def test_clearing_a_conflict_keeps_a_draining_generation_unadmitted(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "draining")
    await coordinator.prepare(claim.use)
    observation = (await backend.inventory())[0]
    rogue = "d" * 64
    backend._created[rogue] = ("unknown", observation.labels, observation.policy_digest)
    await coordinator.reconcile_once()
    clock.now += 31
    await store.plan_recovery(claim.use.key, owner="w2", lease_seconds=30)
    assert (await store.get_usage(claim.use.usage_id)).state == "orphaned"
    assert await store.clear_resolved_inventory_block(claim.use.key, daemon_id="daemon-1") is False
    row = await store.db.fetchrow("SELECT * FROM sandboxes WHERE profile_id = 'p1'")
    assert row["state"] == "draining" and row["error_code"] is None
    await queue_run(store, "later")
    assert await coordinator.claim() is None


async def test_a_conflict_target_hidden_by_its_deployment_label_is_not_assumed_absent(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "hidden")
    await coordinator.prepare(claim.use)
    observation = (await backend.inventory())[0]
    rogue = "c" * 64
    backend._created[rogue] = ("unknown", observation.labels, observation.policy_digest)
    await coordinator.reconcile_once()
    name, labels, digest = backend._created[rogue]
    backend._created[rogue] = (name, tuple(
        (key, "other" if key == "keel.deployment" else value) for key, value in labels), digest)
    full_inventory = backend.inventory
    async def deployment_inventory():
        return [item for item in await full_inventory()
                if dict(item.labels).get("keel.deployment") == "dep"]
    backend.inventory = deployment_inventory
    await coordinator.reconcile_once()
    row = await store.db.fetchrow("SELECT * FROM sandboxes WHERE profile_id = 'p1'")
    assert row["state"] == "draining" and row["error_code"] == "sandbox_inventory_conflict"
    assert await coordinator.claim() is None


async def test_unbound_preparation_releases_while_the_daemon_is_unreachable(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "outage")
    create = await store.begin_lifecycle(claim.use, "create", target=None,
                                         create_operation_id=None, lease_seconds=30)
    await store.authorize_dispatch(create)
    async def unavailable():
        raise OSError("daemon unavailable")
    backend.daemon_identity = unavailable
    clock.now += 31
    report = await coordinator.reconcile_once()
    assert (await store.get_usage(claim.use.usage_id)).state == "released"
    assert report.unresolved == 1
    assert (await store.get_operation(create.id)).resolved_at is None


async def test_restarted_independent_store_removes_historical_attempt_without_touching_current(recovery):
    import os
    from keel.store import open_store
    coordinator, backend, store, clock = recovery
    old = await make_claim(store, "before-restart")
    policy = await coordinator._profile_policy(old.use)
    create = await store.begin_lifecycle(old.use, "create", target=None,
                                         create_operation_id=None, lease_seconds=30)
    await store.authorize_dispatch(create)
    await store.retire_unbound_preparation(old.use, abandon=True)
    current = await make_claim(store, "after-restart")
    bound = await coordinator.prepare(current.use)
    if store.db.dialect == "sqlite":
        path = (await store.db.fetchrow("PRAGMA database_list"))["file"]
        url = "sqlite:///" + path
    else:
        url = os.environ["KEEL_TEST_POSTGRES_DSN"]
    restarted_store = await open_store(url, clock=clock)
    restarted_store.coordination_clock = clock
    try:
        restarted = SandboxCoordinator(restarted_store, backend, deployment="dep", owner="restart",
                                       lease_seconds=30, idle_seconds=10, reap_seconds=1)
        late = await backend.create_generation(create, policy)
        await restarted.reconcile_once()
        assert backend.removed_ids == [late.observation.container_id]
        assert await current_container_id(restarted_store, current.use.key) == bound.name
        assert (await store.get_usage(current.use.usage_id)).state == "active"
    finally:
        await restarted_store.close()


async def test_postgres_recovery_skips_locked_profile_and_retries_later(recovery):
    import os
    from keel.store import open_store
    coordinator, backend, store, clock = recovery
    if store.db.dialect != "postgres":
        pytest.skip("a row-lock barrier needs PostgreSQL")
    first = await make_claim(store, "locked", profile_id="locked")
    await coordinator.prepare(first.use)
    second = await make_claim(store, "available", profile_id="available")
    other = await coordinator.prepare(second.use)
    clock.now += 31
    independent = await open_store(os.environ["KEEL_TEST_POSTGRES_DSN"], clock=clock)
    try:
        async with independent.db.transaction():
            await independent.db.fetchrow("SELECT id FROM profiles WHERE id = 'locked' FOR UPDATE")
            await asyncio.wait_for(coordinator.reconcile_once(), timeout=5)
            assert backend.removed_ids == [other.name]
            assert (await store.get_usage(first.use.usage_id)).state == "active"
        await coordinator.reconcile_once()
        assert (await store.get_usage(first.use.usage_id)).state == "released"
    finally:
        await independent.close()


async def test_retired_authorized_start_still_requires_removal_before_unbound_release(recovery):
    from keel.sandbox.records import SandboxBinding
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "authorized-start")
    policy = await coordinator._profile_policy(claim.use)
    create = await store.begin_lifecycle(claim.use, "create", target=None,
                                         create_operation_id=None, lease_seconds=30)
    await store.authorize_dispatch(create)
    result = await backend.create_generation(create, policy)
    await store.record_lifecycle_result(create, result)
    target = SandboxBinding(create.generation, create.generation_token, result.observation.container_id,
                            result.observation.container_name, policy, coordinator._artifact.sha256)
    await store.begin_lifecycle(claim.use, "start", target=target, create_operation_id=create.id, lease_seconds=30)
    async def unavailable():
        raise OSError("daemon unavailable")
    backend.daemon_identity = unavailable
    clock.now += 31
    await coordinator.reconcile_once()
    assert (await store.get_usage(claim.use.usage_id)).state == "orphaned"
    assert backend.removed_ids == []


async def test_daemon_change_during_missing_inspection_cannot_release_current_users(recovery):
    coordinator, backend, store, clock = recovery
    claim = await make_claim(store, "daemon-race")
    bound = await coordinator.prepare(claim.use)
    async def replaced(exact):
        backend.daemon_id = "replacement-daemon"
        return DockerOutcome("definite_failure", diagnostic="the container does not exist")
    backend.inspect_id = replaced
    await coordinator.reconcile_once()
    assert await current_container_id(store, claim.use.key) == bound.name
    assert (await store.get_usage(claim.use.usage_id)).state == "active"


async def test_alternating_recovery_and_idle_batches_do_not_starve_later_profiles(recovery):
    coordinator, backend, store, clock = recovery
    # Two exactly full batches reproduce shared-cursor starvation deterministically: recovery would always
    # see the first hundred keys while idle, which cannot revoke expired usage, always sees the second.
    for index in range(200):
        profile = f"p{index:03}"
        await seed_profile(store, profile)
        await store.db.execute(
            "INSERT INTO sandboxes (deployment, profile_id, daemon_id, generation, state, transitioned_at) "
            "VALUES ('dep', ?, 'daemon-1', 0, 'absent', ?)", profile, clock.now)
    claim = await make_claim(store, "later-batch", profile_id="p199")
    bound = await coordinator.prepare(claim.use)
    clock.now += 31
    for _ in range(2):
        await coordinator.reconcile_once(limit=100)
        await coordinator.reap_idle_once(limit=100)
    assert (await store.get_usage(claim.use.usage_id)).state == "released"
    assert backend.removed_ids == [bound.name]


async def test_candidate_cursor_rejects_unknown_purpose(store):
    with pytest.raises(ValueError):
        await store.recovery_candidates(purpose="invalid")


async def test_an_unknown_container_on_an_unpublished_profile_blocks_until_proven_absent(recovery):
    # A legacy or unknown container on a profile that never had a coordinated generation blocks only that profile.
    # The block clears once the recorded exact ID is proven absent on the same daemon, never because of a lapse.
    coordinator, backend, store, _ = recovery
    await seed_profile(store, "p1")
    unknown = "e" * 64
    labels = (("keel.deployment", "dep"), ("keel.managed", "1"), ("keel.policy", "0" * 16), ("keel.profile", "p1"))
    backend._created[unknown] = ("keel-sbx-legacy", labels, "0" * 16)
    backend.running_ids.add(unknown)
    await coordinator.reconcile_once()
    await queue_run(store, "waits")
    assert await coordinator.claim() is None
    await coordinator.reconcile_once()  # still present: the block stays and nothing is removed
    assert await coordinator.claim() is None and backend.removed_ids == []

    backend._created.pop(unknown)  # the operator removed it after stopping the old writers
    await coordinator.reconcile_once()
    assert ("inspect", unknown, "absent") in backend.journal
    row = await store.db.fetchrow("SELECT state, error_code FROM sandboxes WHERE profile_id = 'p1'")
    assert (row["state"], row["error_code"]) == ("absent", None)
    claim = await coordinator.claim()
    assert claim is not None and (await store.get_usage(claim.use.usage_id)).state == "preparing"


async def test_an_unpublished_block_stays_when_the_absence_is_not_authoritative(recovery):
    coordinator, backend, store, _ = recovery
    await seed_profile(store, "p1")
    unknown = "d" * 64
    labels = (("keel.deployment", "dep"), ("keel.managed", "1"), ("keel.policy", "0" * 16), ("keel.profile", "p1"))
    backend._created[unknown] = ("keel-sbx-legacy", labels, "0" * 16)
    await coordinator.reconcile_once()
    backend._created.pop(unknown)

    async def uncertain(container_id):
        return DockerOutcome("uncertain", diagnostic="the inspect did not produce a definite result")

    backend.inspect_id = uncertain
    report = await coordinator.reconcile_once()
    assert report.blocked >= 1
    row = await store.db.fetchrow("SELECT state, error_message FROM sandboxes WHERE profile_id = 'p1'")
    assert (row["state"], row["error_message"]) == ("blocked", unknown)
    await queue_run(store, "waits")
    assert await coordinator.claim() is None


def test_a_present_offender_that_started_running_is_not_settled():
    """The fresh exact-ID inspect is the newest evidence: a container started since the listing keeps the block."""
    container = "a" * 64
    stopped = ContainerObservation("daemon-1", container, "keel-sbx", (), running=False, policy_digest="0" * 16)
    running = ContainerObservation("daemon-1", container, "keel-sbx", (), running=True, policy_digest="0" * 16)
    assert SandboxRecoveryMixin._offender_settled(
        DockerOutcome("succeeded", observation=stopped), container, [stopped])
    assert not SandboxRecoveryMixin._offender_settled(
        DockerOutcome("succeeded", observation=running), container, [running])


def _unknown(backend, container: str, profile_id: str = "p1") -> None:
    labels = (("keel.deployment", "dep"), ("keel.managed", "1"), ("keel.policy", "0" * 16),
              ("keel.profile", profile_id))
    backend._created[container] = ("keel-sbx-legacy-" + container[:6], labels, "0" * 16)
    backend.running_ids.add(container)


@pytest.mark.parametrize("path", ["recover_key", "batched_inventory"])
async def test_an_unpublished_block_stays_while_another_unknown_container_of_the_profile_remains(recovery, path):
    # The block records one offending ID; removing only that container must not reopen admission while another
    # unclassified container of the same profile still exists, whatever path or batch performs the check.
    coordinator, backend, store, _ = recovery
    await seed_profile(store, "p1")
    first, second = "a" * 64, "b" * 64
    _unknown(backend, first)
    _unknown(backend, second)
    if path == "batched_inventory":
        for index in range(120):  # more containers than one bounded inventory pass classifies
            _unknown(backend, f"{index:064x}", profile_id=f"other-{index}")
    for _ in range(3):
        await coordinator.reconcile_once()
    recorded = (await store.db.fetchrow("SELECT error_message FROM sandboxes WHERE profile_id = 'p1'"))[
        "error_message"]
    remaining = first if recorded == second else second
    backend._created.pop(recorded)  # the operator removed only the recorded offender
    key = next(key for key in await store.recovery_candidates(limit=100) if key.profile_id == "p1")
    if path == "recover_key":  # the lease sweep's recover_run path: no inventory scan at all
        await coordinator._recover_key(key)
    else:
        # A bounded pass whose 100-container batch does not reach the remaining container (it sorts last).
        coordinator._inventory_cursor = ""
        await coordinator.reconcile_once()
    await queue_run(store, "waits")
    assert await coordinator.claim() is None, "admission reopened beside an unclassified container"
    assert (await store.db.fetchrow("SELECT state FROM sandboxes WHERE profile_id = 'p1'"))["state"] == "blocked"

    backend._created.pop(remaining)  # once every unknown container of the profile is gone, the block clears
    for _ in range(3):
        await coordinator.reconcile_once()
    claim = await coordinator.claim()
    assert claim is not None and claim.run.profile_id == "p1"


async def test_a_published_profile_block_stays_while_another_unknown_container_remains(recovery):
    coordinator, backend, store, _ = recovery
    backend._containers = ["0" * 64]  # the published container sorts first, inside every first batch
    claim = await make_claim(store, "current")
    bound = await coordinator.prepare(claim.use)
    assert await coordinator.release(claim.use)
    first, second = "a" * 64, "b" * 64
    _unknown(backend, first)
    _unknown(backend, second)
    for index in range(1, 121):
        _unknown(backend, f"{index:064x}", profile_id=f"other-{index}")
    for _ in range(3):
        await coordinator.reconcile_once()
    row = await store.db.fetchrow("SELECT error_message FROM sandboxes WHERE profile_id = 'p1'")
    remaining = first if row["error_message"] == second else second
    backend._created.pop(row["error_message"])
    coordinator._inventory_cursor = ""  # this pass's batch does not reach the remaining container
    await coordinator.reconcile_once()
    await queue_run(store, "waits")
    assert await coordinator.claim() is None, "admission reopened beside an unclassified container"
    backend._created.pop(remaining)
    for _ in range(3):
        await coordinator.reconcile_once()
    reopened = await coordinator.claim()
    assert reopened is not None and reopened.run.profile_id == "p1"
    assert (await store.get_usage(reopened.use.usage_id)).binding.container_id == bound.name


async def _late_retired_create(coordinator, store, backend):
    """A retired create whose container arrived late (recorded in the ledger), as a stopped container."""
    claim = await make_claim(store, "old")
    attempt = await store.begin_lifecycle(claim.use, "create", target=None, create_operation_id=None,
                                          lease_seconds=30)
    assert await store.authorize_dispatch(attempt)
    await store.record_lifecycle_result(attempt, DockerOutcome("uncertain"))
    assert await store.retire_unbound_preparation(claim.use, abandon=True)
    await store.finish_run(claim.run.id, "failed", error_code="sandbox_unavailable", error="gone")
    late = await backend.create_generation(attempt, await coordinator._profile_policy(claim.use))
    return late.observation.container_id


@pytest.mark.parametrize("conflict", ["running_retired_create", "recorded_labels_disagree"])
@pytest.mark.parametrize("path", ["recover_key", "limited_scan"])
async def test_a_block_stays_while_a_recorded_container_of_the_profile_still_conflicts(recovery, conflict, path):
    # Knowing a container's ID is not enough: a running retired create, or a recorded ID whose labels or policy
    # disagree, is classified as a conflict, and the block must stay while such a container is listed.
    coordinator, backend, store, _ = recovery
    backend._containers = ["1" * 64]  # the late create sorts first, so a one-container batch can skip it
    late = await _late_retired_create(coordinator, store, backend)
    name, labels, digest = backend._created[late]
    if conflict == "running_retired_create":
        backend.running_ids.add(late)  # started outside keel: it may run payload code
    else:
        backend._created[late] = (name, tuple((key, "f" * 16 if key == "keel.policy" else value)
                                              for key, value in labels), digest)
    unknown = "f" * 64
    _unknown(backend, unknown)
    await coordinator.reconcile_once()
    row = await store.db.fetchrow("SELECT state, error_message FROM sandboxes WHERE profile_id = 'p1'")
    assert row["state"] == "blocked"
    # The operator removes the unknown container; the recorded (ledger-known) one still conflicts.
    backend._created.pop(unknown)
    key = next(key for key in await store.recovery_candidates(limit=100) if key.profile_id == "p1")
    if path == "recover_key":
        await coordinator._recover_key(key)
    else:
        coordinator._inventory_cursor = late  # this one-container batch skips the conflicting container
        await coordinator.reconcile_once(limit=1)
    await queue_run(store, "waits")
    assert late in backend._created
    assert await coordinator.claim() is None, "admission reopened beside a conflicting recorded container"

    if conflict == "running_retired_create":
        backend.running_ids.discard(late)  # stopped again: an ordinary quarantined late create
    else:
        backend._created.pop(late)
    for _ in range(3):
        await coordinator.reconcile_once()
    assert late not in backend._created
    claim = await coordinator.claim()
    assert claim is not None and claim.run.session_id == "waits"


async def test_profile_deletion_purges_resolved_sandbox_evidence(recovery):
    coordinator, backend, store, _ = recovery
    target = await make_claim(store, "gone")
    target_container = (await coordinator.prepare(target.use)).binding.container_id
    await store.finish_run(target.run.id, "completed", output="ok")
    assert await coordinator.release(target.use)

    kept = await make_claim(store, "kept", profile_id="p2")
    kept_container = (await coordinator.prepare(kept.use)).binding.container_id
    await store.finish_run(kept.run.id, "completed", output="ok")
    assert await coordinator.release(kept.use)
    assert target_container != kept_container

    service = ProfileDeletionService(store, coordinator=coordinator)
    await service.delete("p1", 1, "11111111-1111-4111-8111-111111111111", timeout_seconds=10)
    await service.complete("p1", 1, "11111111-1111-4111-8111-111111111111")

    for table in ("sandboxes", "sandbox_uses", "sandbox_execs", "sandbox_operations"):
        assert await store.db.fetchrow(
            f"SELECT 1 AS present FROM {table} WHERE profile_id = 'p1'") is None, table
    # Another profile's lifecycle evidence survives the reset.
    assert await store.db.fetchrow("SELECT 1 AS present FROM sandboxes WHERE profile_id = 'p2'") is not None
    assert await store.db.fetchrow(
        "SELECT 1 AS present FROM sandbox_operations WHERE profile_id = 'p2'") is not None
    assert await current_container_id(store, kept.use.key) == kept_container

async def test_a_published_clear_is_bound_to_the_recorded_offender(recovery):
    coordinator, backend, store, _ = recovery
    claim = await make_claim(store, "current")
    await coordinator.prepare(claim.use)
    _unknown(backend, "a" * 64)
    await coordinator.reconcile_once()
    row = await store.db.fetchrow("SELECT error_message FROM sandboxes WHERE profile_id = 'p1'")
    assert row["error_message"] == "a" * 64
    # Evidence about another container never clears the block recorded on this one.
    assert not await store.clear_resolved_inventory_block(claim.use.key, daemon_id="daemon-1", offending="b" * 64)
    assert (await store.db.fetchrow("SELECT error_message FROM sandboxes WHERE profile_id = 'p1'"))[
        "error_message"] == "a" * 64
