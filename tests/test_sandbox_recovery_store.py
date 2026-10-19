"""Exec registry, guarded release, recovery planning and evidence retention (store task 3)."""

import uuid
from dataclasses import replace

import pytest

from keel.sandbox.base import parse_policy
from keel.sandbox.records import (
    BoundSandbox, CleanupReceipt, ContainerObservation, DockerOutcome, SandboxBinding, SandboxBusy,
    SandboxKey, SandboxLeaseLost,
)
from tests.sandbox_support import (
    DEFAULT_SANDBOX, container_id, make_claim, queue_run, seed_profile, seed_ready_sandbox,
)

DEPLOYMENT = "dep"
DAEMON = "daemon-1"


def _key(profile_id: str = "p1") -> SandboxKey:
    return SandboxKey(DEPLOYMENT, profile_id, DAEMON)


@pytest.fixture(autouse=True)
async def db_clock(store, clock):
    """Drive coordination deadlines from the injected clock, aligned with database time."""
    clock.now = await store.db.database_now()
    store.coordination_clock = clock
    return clock


def _binding(generation: dict) -> SandboxBinding:
    return SandboxBinding(
        generation=generation["generation"], generation_token=generation["generation_token"],
        container_id=generation["container_id"], container_name=generation["container_name"],
        policy=generation["policy"], helper_digest="sha256:test-helper")


def _bound(claim, generation: dict) -> BoundSandbox:
    binding = _binding(generation)
    return BoundSandbox(profile_id=claim.use.key.profile_id, name=binding.container_id,
                        policy=binding.policy, use=claim.use, binding=binding)


async def _active_claim(store, *, session_id: str = "s1", profile_id: str = "p1", owner: str = "w1"):
    await seed_profile(store, profile_id)
    generation = await seed_ready_sandbox(store, profile_id=profile_id)
    await queue_run(store, session_id, profile_id=profile_id)
    claim = await store.claim_run_with_usage(owner, 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
    assert claim is not None and claim.use is not None
    return claim, generation


async def _created(store, claim):
    """Drive one create to a definite success and build the exact binding later steps use."""
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(create) is True
    exact_id = container_id()
    saved = await store.record_lifecycle_result(
        create, DockerOutcome(state="succeeded", observation=ContainerObservation(
            daemon_id=DAEMON, container_id=exact_id, container_name=create.intended_name)))
    target = SandboxBinding(
        generation=create.generation, generation_token=create.generation_token, container_id=exact_id,
        container_name=create.intended_name, policy=parse_policy(DEFAULT_SANDBOX, mount_root="/"),
        helper_digest="sha256:test-helper")
    return saved, target


def _success(ticket) -> CleanupReceipt:
    return CleanupReceipt(ticket.exec_id, ticket.control_token, 0, True, True, None)


async def _one(store, sql: str, *args) -> dict:
    return await store.db.fetchrow(sql, *args)


# ---- exec registry ---------------------------------------------------------------------------------------------


async def test_begin_exec_commits_pending_before_any_dispatch(store):
    claim, generation = await _active_claim(store)
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1")

    row = await _one(store, "SELECT * FROM sandbox_execs WHERE id = 'exec-1'")
    assert row["state"] == "pending"
    assert row["dispatched_at"] is None and row["started_at"] is None and row["finished_at"] is None
    assert row["control_token"] == ticket.control_token and len(ticket.control_token) == 32
    assert (row["usage_id"], row["usage_token"], row["run_id"], row["session_id"]) == (
        claim.use.usage_id, claim.use.token, claim.run.id, claim.run.session_id)
    assert (row["generation"], row["generation_token"], row["container_id"]) == (
        generation["generation"], generation["generation_token"], generation["container_id"])
    assert ticket.exec_id == "exec-1" and ticket.use == claim.use


async def test_two_execs_get_never_reused_control_tokens(store):
    claim, generation = await _active_claim(store)
    bound = _bound(claim, generation)
    first = await store.begin_exec(bound, "exec-1")
    second = await store.begin_exec(bound, "exec-2")
    assert first.control_token != second.control_token
    assert first.control_token != first.exec_id


@pytest.mark.parametrize("case", ["owner", "token", "lease", "state", "container", "generation", "duplicate"])
async def test_begin_exec_rejects_invalid_authority_without_a_row(store, clock, case):
    claim, generation = await _active_claim(store)
    bound = _bound(claim, generation)
    use = claim.use
    if case == "owner":
        use = replace(use, owner="someone-else")
        bound = replace(bound, use=use)
    elif case == "token":
        use = replace(use, token="0" * 32)
        bound = replace(bound, use=use)
    elif case == "lease":
        await store.db.execute(
            "UPDATE sandbox_uses SET lease_expires_at = ? WHERE id = ?", clock.now - 1, use.usage_id)
    elif case == "state":
        await store.db.execute("UPDATE sandbox_uses SET state = 'cleaning' WHERE id = ?", use.usage_id)
    elif case == "container":
        binding = replace(bound.binding, container_id=container_id())
        bound = replace(bound, name=binding.container_id, binding=binding)
    elif case == "generation":
        binding = replace(bound.binding, generation=bound.binding.generation + 1)
        bound = replace(bound, binding=binding)
    else:
        await store.begin_exec(bound, "exec-1")
        with pytest.raises(SandboxBusy):
            await store.begin_exec(bound, "exec-1")
        assert (await _one(store, "SELECT COUNT(*) AS n FROM sandbox_execs"))["n"] == 1
        return

    with pytest.raises((SandboxLeaseLost, SandboxBusy)):
        await store.begin_exec(bound, "exec-1")
    assert (await _one(store, "SELECT COUNT(*) AS n FROM sandbox_execs"))["n"] == 0


async def test_finish_exec_records_one_finished_transition(store):
    claim, generation = await _active_claim(store)
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1")

    assert await store.finish_exec(ticket, _success(ticket)) is True
    row = await _one(store, "SELECT * FROM sandbox_execs WHERE id = 'exec-1'")
    assert row["state"] == "finished" and row["finished_at"] is not None
    assert row["cleanup_outcome"] == "complete" and row["uncertainty_reason"] is None
    finished_at = row["finished_at"]

    assert await store.finish_exec(ticket, _success(ticket)) is False
    again = await _one(store, "SELECT * FROM sandbox_execs WHERE id = 'exec-1'")
    assert again["state"] == "finished" and again["finished_at"] == finished_at
    sandbox = await _one(
        store, "SELECT state FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "ready"


@pytest.mark.parametrize("mismatch", ["exec_id", "control_token"])
async def test_finish_exec_rejects_a_mismatched_receipt(store, mismatch):
    claim, generation = await _active_claim(store)
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1")
    receipt = _success(ticket)
    receipt = replace(receipt, **{mismatch: "wrong"})

    assert await store.finish_exec(ticket, receipt) is False
    row = await _one(store, "SELECT * FROM sandbox_execs WHERE id = 'exec-1'")
    assert row["state"] == "pending" and row["finished_at"] is None


@pytest.mark.parametrize("receipt", [
    lambda ticket: CleanupReceipt(ticket.exec_id, ticket.control_token, 0, True, False, None),
    lambda ticket: CleanupReceipt(ticket.exec_id, ticket.control_token, 0, False, True, None),
    lambda ticket: CleanupReceipt(ticket.exec_id, ticket.control_token, 1, True, True, "escaped descendant"),
])
async def test_incomplete_or_uncertain_receipt_drains_the_generation(store, receipt):
    claim, generation = await _active_claim(store)
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1")

    assert await store.finish_exec(ticket, receipt(ticket)) is False
    row = await _one(store, "SELECT * FROM sandbox_execs WHERE id = 'exec-1'")
    assert row["state"] == "uncertain" and row["finished_at"] is not None
    assert row["uncertainty_reason"] is not None
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "draining"
    assert sandbox["generation"] == generation["generation"]


async def test_uncertainty_never_drains_a_newer_generation(store):
    claim, generation = await _active_claim(store)
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1")
    newer_token = uuid.uuid4().hex
    newer_container = container_id()
    await store.db.execute(
        "UPDATE sandboxes SET generation = ?, generation_token = ?, container_id = ?, container_name = ?, "
        "state = 'ready' WHERE deployment = ? AND profile_id = ?",
        generation["generation"] + 1, newer_token, newer_container, "keel-sbx-newer", DEPLOYMENT, "p1")

    assert await store.finish_exec(ticket, CleanupReceipt(
        ticket.exec_id, ticket.control_token, 1, True, True, "lost receipt")) is False
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "ready" and sandbox["generation_token"] == newer_token
    assert sandbox["container_id"] == newer_container


async def test_mark_exec_uncertain_never_downgrades_a_finished_exec(store):
    claim, generation = await _active_claim(store)
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1")
    assert await store.finish_exec(ticket, _success(ticket)) is True

    await store.mark_exec_uncertain(ticket, "late failure")
    row = await _one(store, "SELECT * FROM sandbox_execs WHERE id = 'exec-1'")
    assert row["state"] == "finished"
    sandbox = await _one(
        store, "SELECT state FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "ready"

    other = await store.begin_exec(_bound(claim, generation), "exec-2")
    await store.mark_exec_uncertain(other, "x" * 2000)
    row = await _one(store, "SELECT * FROM sandbox_execs WHERE id = 'exec-2'")
    assert row["state"] == "uncertain" and len(row["uncertainty_reason"]) <= 512


# ---- normal cleanup and release --------------------------------------------------------------------------------


async def test_release_waits_for_exec_and_terminal_run_status_is_not_enough(store):
    claim, generation = await _active_claim(store)
    use = claim.use
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1")

    assert await store.begin_usage_cleanup(use) is True
    assert (await store.get_usage(use.usage_id)).state == "cleaning"
    await store.finish_run(claim.run.id, "completed")
    assert await store.release_usage(use) is False
    assert (await store.get_usage(use.usage_id)).state == "cleaning"

    assert await store.finish_exec(ticket, _success(ticket)) is True
    assert await store.release_usage(use) is True
    usage = await store.get_usage(use.usage_id)
    assert usage.state == "released" and usage.release_reason == "normal_release"
    assert await store.release_usage(use) is False


@pytest.mark.parametrize("state", ["preparing", "orphaned", "released"])
async def test_begin_usage_cleanup_only_accepts_an_active_usage(store, state):
    claim, _ = await _active_claim(store)
    now = await store.db.database_now()
    await store.db.execute(
        "UPDATE sandbox_uses SET state = ?, released_at = ? WHERE id = ?",
        state, now if state == "released" else None, claim.use.usage_id)
    if state == "released":
        await store.db.execute(
            "UPDATE sandbox_uses SET release_reason = 'test' WHERE id = ?", claim.use.usage_id)
    assert await store.begin_usage_cleanup(claim.use) is False


async def test_release_refuses_live_preparation_authority(store):
    claim = await make_claim(store, "s1")
    await store.begin_lifecycle(claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    await store.db.execute("UPDATE sandbox_uses SET state = 'cleaning' WHERE id = ?", claim.use.usage_id)

    assert await store.release_usage(claim.use) is False
    assert (await store.get_usage(claim.use.usage_id)).state == "cleaning"


async def test_orphaned_usage_releases_only_after_its_generation_is_gone(store):
    claim, generation = await _active_claim(store)
    use = claim.use
    await store.db.execute("UPDATE sandbox_uses SET state = 'orphaned' WHERE id = ?", use.usage_id)
    assert await store.release_usage(use) is False

    await store.db.execute(
        "UPDATE sandboxes SET state = 'absent', generation = ?, generation_token = NULL, container_id = NULL, "
        "container_name = NULL WHERE deployment = ? AND profile_id = ?",
        generation["generation"] + 1, DEPLOYMENT, "p1")
    assert await store.release_usage(use) is True
    usage = await store.get_usage(use.usage_id)
    assert usage.release_reason == "generation_removed"
    assert await store.release_usage(use) is False


# ---- section 11.1: unbound preparation retirement -------------------------------------------------------------


async def test_unbound_preparation_can_be_retired_before_create_allocation(store, clock):
    claim = await make_claim(store, "s1")
    clock.now += 31

    assert await store.retire_unbound_preparation(claim.use) is True
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "released" and usage.release_reason == "preparation_retired_without_execution"
    assert await store.renew_usage(claim.use, 30) is None
    assert (await _one(store, "SELECT COUNT(*) AS n FROM sandbox_operations"))["n"] == 0


async def test_undelivered_planned_create_is_retired_with_its_evidence(store, clock):
    claim = await make_claim(store, "s1")
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    clock.now += 31

    assert await store.retire_unbound_preparation(claim.use) is True
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.release_reason == "preparation_retired_without_execution"
    saved = await store.get_operation(create.id)
    assert saved.outcome == "uncertain" and saved.authority == "retired"
    assert saved.retirement_reason == "preparation_retired"
    assert saved.resolved_at is None  # the tombstone remains independently discoverable
    assert usage.create_operation_id == create.id  # retained as evidence
    assert await store.renew_usage(claim.use, 30) is None


async def test_a_valid_owner_may_abandon_its_healthy_preparation(store):
    claim = await make_claim(store, "s1")
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)

    assert await store.retire_unbound_preparation(claim.use) is False
    assert (await store.get_usage(claim.use.usage_id)).state == "preparing"
    assert (await store.get_operation(create.id)).authority == "current"

    assert await store.retire_unbound_preparation(claim.use, abandon=True) is True
    assert (await store.get_usage(claim.use.usage_id)).state == "released"
    assert (await store.get_operation(create.id)).authority == "retired"


async def test_an_authorized_start_blocks_unbound_retirement(store, clock):
    claim = await make_claim(store, "s1")
    create, target = await _created(store, claim)
    start = await store.begin_lifecycle(
        claim.use, "start", target=target, create_operation_id=create.id, lease_seconds=30)
    clock.now += 31

    assert await store.retire_unbound_preparation(claim.use) is False
    assert (await store.get_usage(claim.use.usage_id)).state == "preparing"
    assert (await store.get_operation(start.id)).authority == "current"
    assert (await store.get_operation(create.id)).authority == "current"


async def test_a_dispatched_install_blocks_until_confirmed_absence(store, clock):
    claim = await make_claim(store, "s1")
    create, target = await _created(store, claim)
    install = await store.begin_lifecycle(
        claim.use, "install", target=target, create_operation_id=create.id, lease_seconds=30)
    assert await store.authorize_dispatch(install) is True
    clock.now += 31

    assert await store.retire_unbound_preparation(claim.use) is False
    await store.db.execute(
        "UPDATE sandbox_operations SET confirmed_absent_at = ?, resolved_at = ? WHERE id = ?",
        clock.now, clock.now, install.id)

    assert await store.retire_unbound_preparation(claim.use) is True
    assert (await store.get_usage(claim.use.usage_id)).state == "released"


async def test_predecessor_replacement_is_not_cleared_by_retirement(store, clock):
    claim = await make_claim(store, "s1")
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    newer_token = uuid.uuid4().hex
    newer_container = container_id()
    newer_name = "keel-sbx-newer"
    await store.db.execute(
        "UPDATE sandboxes SET generation = ?, generation_token = ?, container_id = ?, container_name = ?, "
        "state = 'ready' WHERE deployment = ? AND profile_id = ?",
        create.generation + 1, newer_token, newer_container, newer_name, DEPLOYMENT, "p1")
    await store.db.execute(
        "INSERT INTO sandbox_uses (id, token, deployment, profile_id, daemon_id, run_id, session_id, owner, "
        "state, generation, generation_token, container_id, container_name, heartbeat_at, lease_expires_at) "
        "VALUES ('use_other', ?, ?, 'p1', ?, 'run_other', NULL, 'w2', 'active', ?, ?, ?, ?, ?, ?)",
        uuid.uuid4().hex, DEPLOYMENT, DAEMON, create.generation + 1, newer_token, newer_container, newer_name,
        clock.now, clock.now + 300)
    clock.now += 31

    assert await store.retire_unbound_preparation(claim.use) is True

    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert (sandbox["state"], sandbox["generation"], sandbox["generation_token"]) == (
        "ready", create.generation + 1, newer_token)
    assert sandbox["container_id"] == newer_container
    other = await store.get_usage("use_other")
    assert (other.state, other.binding.container_id) == ("active", newer_container)


async def test_partial_or_conflicting_binding_blocks_unbound_retirement(store, clock):
    claim = await make_claim(store, "s1")
    clock.now += 31
    await store.db.execute(
        "UPDATE sandbox_uses SET container_id = ? WHERE id = ?", container_id(), claim.use.usage_id)
    assert await store.retire_unbound_preparation(claim.use) is False
    assert (await store.get_usage(claim.use.usage_id)).state == "preparing"

    await store.db.execute(
        "UPDATE sandbox_uses SET container_id = NULL, create_operation_id = 'op_missing' WHERE id = ?",
        claim.use.usage_id)
    assert await store.retire_unbound_preparation(claim.use) is False


async def test_a_create_linked_without_a_pointer_blocks_unbound_retirement(store, clock):
    claim = await make_claim(store, "s1")
    await store.begin_lifecycle(claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    await store.db.execute(
        "UPDATE sandbox_uses SET create_operation_id = NULL WHERE id = ?", claim.use.usage_id)
    clock.now += 31

    assert await store.retire_unbound_preparation(claim.use) is False


async def test_an_unretired_exec_blocks_unbound_retirement(store, clock):
    claim, generation = await _active_claim(store)
    await store.begin_exec(_bound(claim, generation), "exec-1")
    # Make the usage look unbound while its exec evidence survives; this is conflicting evidence.
    await store.db.execute(
        "UPDATE sandbox_uses SET generation = NULL, generation_token = NULL, container_id = NULL, "
        "container_name = NULL, state = 'preparing' WHERE id = ?", claim.use.usage_id)
    clock.now += 31

    assert await store.retire_unbound_preparation(claim.use) is False


async def test_a_retired_usage_cannot_renew_or_publish_again(store, clock):
    claim = await make_claim(store, "s1")
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    clock.now += 31
    assert await store.retire_unbound_preparation(claim.use) is True

    assert await store.renew_usage(claim.use, 30) is None
    assert await store.authorize_dispatch(create) is False
    with pytest.raises(SandboxLeaseLost):
        await store.begin_lifecycle(
            claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    target = SandboxBinding(
        create.generation, create.generation_token, container_id(), create.intended_name,
        parse_policy(DEFAULT_SANDBOX, mount_root="/"), "sha256:test-helper")
    with pytest.raises(SandboxLeaseLost):
        await store.begin_lifecycle(
            claim.use, "remove", target=target, create_operation_id=create.id, lease_seconds=30)


# ---- deletion before cleanup -----------------------------------------------------------------------------------


async def test_session_and_run_deletion_do_not_remove_execution_evidence(store):
    claim, generation = await _active_claim(store)
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1")
    await store.finish_run(claim.run.id, "completed")
    assert await store.delete_session("p1", "s1") is True

    assert (await _one(store, "SELECT COUNT(*) AS n FROM sandbox_execs WHERE id = 'exec-1'"))["n"] == 1
    assert (await _one(store, "SELECT COUNT(*) AS n FROM sandbox_uses WHERE id = ?",
                       claim.use.usage_id))["n"] == 1
    assert await store.finish_exec(ticket, _success(ticket)) is True
    assert await store.begin_usage_cleanup(claim.use) is True
    assert await store.release_usage(claim.use) is True


async def test_retirement_and_recovery_work_after_profile_deletion(store, clock):
    claim = await make_claim(store, "s1")
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    clock.now += 31
    await store.db.execute("DELETE FROM profiles WHERE id = 'p1'")

    assert await store.retire_unbound_preparation(claim.use) is True
    assert (await store.get_operation(create.id)).authority == "retired"
    assert await store.renew_usage(claim.use, 30) is None
    assert await store.plan_recovery(_key(), owner="recovery", lease_seconds=30) == []


async def test_finish_exec_works_after_profile_deletion(store):
    claim, generation = await _active_claim(store)
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1")
    await store.db.execute("DELETE FROM profiles WHERE id = 'p1'")

    assert await store.finish_exec(ticket, _success(ticket)) is True
    assert (await _one(store, "SELECT state FROM sandbox_execs WHERE id = 'exec-1'"))["state"] == "finished"


# ---- recovery planning and commit ------------------------------------------------------------------------------


async def _expired_bound_profile(store, clock, *, with_exec: bool = False):
    claim, generation = await _active_claim(store)
    ticket = await store.begin_exec(_bound(claim, generation), "exec-1") if with_exec else None
    clock.now += 31
    return claim, generation, ticket


async def test_plan_recovery_orphans_drains_and_plans_removal(store, clock):
    claim, generation, _ = await _expired_bound_profile(store, clock)

    actions = await store.plan_recovery(_key(), owner="recovery", lease_seconds=30)

    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "orphaned"
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "removing"
    assert [action.action for action in actions] == ["remove"]
    action = actions[0]
    assert action.container_id == generation["container_id"]
    assert (action.generation, action.generation_token) == (
        generation["generation"], generation["generation_token"])
    operation = await store.get_operation(action.operation_id)
    assert operation.kind == "remove" and operation.outcome == "planned"
    assert operation.owner_token == action.owner_token
    assert operation.usage_id is None and operation.usage_token is None
    assert sandbox["current_operation_id"] == action.operation_id


async def test_repeated_scans_are_empty_and_do_not_starve_later_work(store, clock):
    claim, generation, _ = await _expired_bound_profile(store, clock)
    actions = await store.plan_recovery(_key(), owner="recovery", lease_seconds=30)
    assert len(actions) == 1

    before = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert await store.plan_recovery(_key(), owner="other", lease_seconds=30) == []
    after = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert before == after
    assert (await _one(store, "SELECT COUNT(*) AS n FROM sandbox_operations"))["n"] == 1


async def test_removal_success_clears_the_generation_and_retires_evidence(store, clock):
    claim, generation, ticket = await _expired_bound_profile(store, clock, with_exec=True)
    actions = await store.plan_recovery(_key(), owner="recovery", lease_seconds=30)
    action = actions[0]
    operation = await store.get_operation(action.operation_id)
    assert await store.authorize_dispatch(operation) is True

    assert await store.commit_recovery(action, DockerOutcome(state="succeeded")) is True

    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "absent" and sandbox["generation"] == generation["generation"]
    assert sandbox["generation_token"] is None and sandbox["container_id"] is None
    assert sandbox["current_operation_id"] is None
    exec_row = await _one(store, "SELECT * FROM sandbox_execs WHERE id = 'exec-1'")
    assert exec_row["state"] == "retired" and exec_row["finished_at"] is not None
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "released" and usage.release_reason == "generation_removed"
    assert (await store.get_operation(action.operation_id)).confirmed_absent_at is not None

    # A fresh claim prepares a genuinely new generation rather than reusing the removed identity.
    await queue_run(store, "s2")
    claim2 = await store.claim_run_with_usage("w2", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
    assert claim2 is not None and claim2.use is not None
    create = await store.begin_lifecycle(
        claim2.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert create.generation == generation["generation"] + 1
    assert create.generation_token != generation["generation_token"]


async def test_recovery_plans_unbound_retirement_and_orphan_release(store, clock):
    unbound = await make_claim(store, "s1", profile_id="p1")
    bound_claim, generation = await _active_claim(store, session_id="s2", profile_id="p2")
    clock.now += 31

    unbound_actions = await store.plan_recovery(_key("p1"), owner="recovery", lease_seconds=30)
    assert [action.action for action in unbound_actions] == ["retire_unbound"]
    assert unbound_actions[0].usage_id == unbound.use.usage_id
    assert await store.commit_recovery(unbound_actions[0], DockerOutcome(state="succeeded")) is True
    assert (await store.get_usage(unbound.use.usage_id)).release_reason == (
        "preparation_retired_without_execution")

    await store.db.execute(
        "UPDATE sandboxes SET state = 'absent', generation = ?, generation_token = NULL, container_id = NULL, "
        "container_name = NULL WHERE deployment = ? AND profile_id = 'p2'",
        generation["generation"] + 1, DEPLOYMENT)
    bound_actions = await store.plan_recovery(_key("p2"), owner="recovery", lease_seconds=30)
    assert [action.action for action in bound_actions] == ["release_orphan"]
    assert await store.commit_recovery(bound_actions[0], DockerOutcome(state="succeeded")) is True
    usage = await store.get_usage(bound_claim.use.usage_id)
    assert usage.state == "released" and usage.release_reason == "generation_removed"


async def test_expired_usage_with_an_unresolved_exec_is_not_released(store, clock):
    claim, generation, _ = await _expired_bound_profile(store, clock, with_exec=True)
    await store.db.execute(
        "UPDATE sandboxes SET state = 'absent', generation = ?, generation_token = NULL, container_id = NULL, "
        "container_name = NULL WHERE deployment = ? AND profile_id = 'p1'",
        generation["generation"] + 1, DEPLOYMENT)

    actions = await store.plan_recovery(_key(), owner="recovery", lease_seconds=30)

    assert actions == []
    assert (await store.get_usage(claim.use.usage_id)).state == "orphaned"


async def test_plan_recovery_leaves_a_healthy_idle_ready_generation(store, clock):
    claim, generation = await _active_claim(store)
    assert await store.begin_usage_cleanup(claim.use) is True
    assert await store.release_usage(claim.use) is True

    assert await store.plan_recovery(_key(), owner="recovery", lease_seconds=30) == []

    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "ready"
    assert sandbox["container_id"] == generation["container_id"]
    assert sandbox["current_operation_id"] is None
    assert (await _one(store, "SELECT COUNT(*) AS n FROM sandbox_operations"))["n"] == 0


async def test_retired_unbound_preparation_recovers_its_discovered_container(store, clock):
    claim = await make_claim(store, "s1")
    assert claim.use is not None
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(create) is True
    exact_id = container_id()
    await store.record_lifecycle_result(create, DockerOutcome(state="succeeded", observation=ContainerObservation(
        daemon_id=DAEMON, container_id=exact_id, container_name=create.intended_name)))

    assert await store.retire_unbound_preparation(claim.use, abandon=True) is True
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "creating" and sandbox["container_id"] is None

    actions = await store.plan_recovery(_key(), owner="recovery", lease_seconds=30)

    assert [entry.action for entry in actions] == ["remove"]
    assert actions[0].container_id == exact_id
    operation = await store.get_operation(actions[0].operation_id)
    assert await store.authorize_dispatch(operation) is True
    assert await store.commit_recovery(actions[0], DockerOutcome(state="succeeded")) is True
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "absent" and sandbox["container_id"] is None
    assert sandbox["generation"] == create.generation


async def test_stale_orphan_does_not_remove_the_current_generation(store, clock):
    claim, generation = await _active_claim(store)
    new_container = container_id()
    new_token = uuid.uuid4().hex
    await store.db.execute(
        "UPDATE sandboxes SET generation = 2, generation_token = ?, container_id = ?, container_name = ?, "
        "transitioned_at = ? WHERE deployment = ? AND profile_id = 'p1'",
        new_token, new_container, "keel-sbx-next", clock.now, DEPLOYMENT)
    clock.now += 31

    actions = await store.plan_recovery(_key(), owner="recovery", lease_seconds=30)

    assert [entry.action for entry in actions] == ["release_orphan"]
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "ready"
    assert (sandbox["generation"], sandbox["generation_token"]) == (2, new_token)
    assert sandbox["container_id"] == new_container


async def test_stale_exec_does_not_remove_the_current_generation(store, clock):
    claim, generation = await _active_claim(store)
    assert await store.begin_exec(_bound(claim, generation), "exec-old") is not None
    new_container = container_id()
    new_token = uuid.uuid4().hex
    await store.db.execute(
        "UPDATE sandboxes SET generation = 2, generation_token = ?, container_id = ?, container_name = ?, "
        "transitioned_at = ? WHERE deployment = ? AND profile_id = 'p1'",
        new_token, new_container, "keel-sbx-next", clock.now, DEPLOYMENT)
    clock.now += 31

    assert await store.plan_recovery(_key(), owner="recovery", lease_seconds=30) == []
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "ready" and sandbox["container_id"] == new_container


async def test_removal_definite_failure_blocks_then_rearms_on_the_next_pass(store, clock):
    claim, generation, _ = await _expired_bound_profile(store, clock)
    action = (await store.plan_recovery(_key(), owner="recovery", lease_seconds=30))[0]
    operation = await store.get_operation(action.operation_id)
    assert await store.authorize_dispatch(operation) is True

    assert await store.commit_recovery(
        action, DockerOutcome(state="definite_failure", diagnostic="daemon refused")) is False
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "blocked" and sandbox["error_code"] == "sandbox_remove_failed"
    assert sandbox["error_message"] == "daemon refused"
    assert sandbox["container_id"] == generation["container_id"]
    assert sandbox["current_operation_id"] == action.operation_id
    saved = await store.get_operation(action.operation_id)
    assert saved.outcome == "definite_failure" and saved.resolved_at is not None
    assert saved.result_message == "daemon refused"

    retry = await store.plan_recovery(_key(), owner="recovery-2", lease_seconds=30)

    assert [entry.action for entry in retry] == ["remove"]
    assert retry[0].operation_id != action.operation_id
    assert retry[0].container_id == generation["container_id"]
    assert (retry[0].generation, retry[0].generation_token) == (
        generation["generation"], generation["generation_token"])
    retired = await store.get_operation(action.operation_id)
    assert retired.authority == "retired" and retired.retirement_reason == "remove_retry"
    assert retired.outcome == "definite_failure" and retired.resolved_at is not None
    assert retired.result_message == "daemon refused"
    fresh = await store.get_operation(retry[0].operation_id)
    assert fresh.authority == "current" and fresh.outcome == "planned"
    assert fresh.owner_token == retry[0].owner_token
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "removing" and sandbox["error_code"] is None
    assert sandbox["error_message"] is None
    assert sandbox["current_operation_id"] == retry[0].operation_id

    assert await store.authorize_dispatch(fresh) is True
    assert await store.commit_recovery(retry[0], DockerOutcome(state="succeeded")) is True
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "absent" and sandbox["current_operation_id"] is None
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "released" and usage.release_reason == "generation_removed"


async def test_blocked_sandbox_with_a_valid_usage_plans_no_retry(store, clock):
    _, generation = await _active_claim(store)
    await store.db.execute(
        "UPDATE sandboxes SET state = 'blocked', error_code = 'sandbox_remove_failed', "
        "error_message = 'daemon refused' WHERE deployment = ? AND profile_id = 'p1'", DEPLOYMENT)

    assert await store.plan_recovery(_key(), owner="recovery", lease_seconds=30) == []

    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "blocked" and sandbox["error_code"] == "sandbox_remove_failed"
    assert sandbox["container_id"] == generation["container_id"]
    assert (await _one(store, "SELECT COUNT(*) AS n FROM sandbox_operations"))["n"] == 0



async def test_uncertain_removal_retains_evidence_until_lease_takeover(store, clock):
    claim, generation, _ = await _expired_bound_profile(store, clock)
    action = (await store.plan_recovery(_key(), owner="recovery", lease_seconds=30))[0]
    operation = await store.get_operation(action.operation_id)
    assert await store.authorize_dispatch(operation) is True

    assert await store.commit_recovery(
        action, DockerOutcome(state="uncertain", diagnostic="lost acknowledgement")) is False
    saved = await store.get_operation(action.operation_id)
    assert saved.outcome == "uncertain" and saved.resolved_at is None
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "removing" and sandbox["container_id"] == generation["container_id"]
    assert await store.plan_recovery(_key(), owner="other", lease_seconds=30) == []

    clock.now += 31
    retry = await store.plan_recovery(_key(), owner="other", lease_seconds=30)
    assert [entry.action for entry in retry] == ["remove"]
    assert retry[0].operation_id == action.operation_id
    assert retry[0].owner_token != action.owner_token
    taken = await store.get_operation(action.operation_id)
    assert taken.owner_token == retry[0].owner_token and taken.owner == "other"
    assert taken.outcome == "uncertain"


@pytest.mark.parametrize("case", ["duplicate", "owner-token", "container", "generation", "token"])
async def test_stale_or_mismatched_recovery_actions_change_nothing(store, clock, case):
    claim, generation, _ = await _expired_bound_profile(store, clock)
    action = (await store.plan_recovery(_key(), owner="recovery", lease_seconds=30))[0]
    operation = await store.get_operation(action.operation_id)
    assert await store.authorize_dispatch(operation) is True
    if case == "duplicate":
        assert await store.commit_recovery(action, DockerOutcome(state="succeeded")) is True
        assert await store.commit_recovery(action, DockerOutcome(state="succeeded")) is False
        return
    if case == "owner-token":
        stale = replace(action, owner_token="0" * 32)
    elif case == "container":
        stale = replace(action, container_id=container_id())
    elif case == "generation":
        stale = replace(action, generation=(action.generation or 0) + 1)
    else:
        stale = replace(action, generation_token=uuid.uuid4().hex)
    before = await _one(store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?",
                        DEPLOYMENT, "p1")

    assert await store.commit_recovery(stale, DockerOutcome(state="succeeded")) is False
    after = await _one(store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?",
                       DEPLOYMENT, "p1")
    assert before == after
    saved = await store.get_operation(action.operation_id)
    assert saved.outcome == "dispatched" and saved.authority == "current"


# ---- evidence retention ----------------------------------------------------------------------------------------


async def test_unresolved_uncertain_create_is_retained_indefinitely(store, clock):
    claim = await make_claim(store, "s1")
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(create) is True
    await store.record_lifecycle_result(
        create, DockerOutcome(state="uncertain", diagnostic="lost create response"))
    assert await store.retire_unbound_preparation(claim.use, abandon=True) is True

    clock.now += 10_000_000
    assert await store.prune_sandbox_records(0.0) == 0
    assert await store.prune_sandbox_records(0.0) == 0
    assert (await store.get_operation(create.id)) is not None
    assert (await store.get_usage(claim.use.usage_id)) is not None
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["generation"] == create.generation


async def test_resolved_attempts_and_released_usages_prune_after_the_cutoff(store, clock):
    claim = await make_claim(store, "s1")
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(create) is True
    await store.record_lifecycle_result(create, DockerOutcome(state="definite_failure"))
    assert await store.retire_unbound_preparation(claim.use, abandon=True) is True

    assert await store.prune_sandbox_records(10.0) == 0
    clock.now += 20
    assert await store.prune_sandbox_records(10.0) == 2
    assert (await store.get_operation(create.id)) is None
    assert (await store.get_usage(claim.use.usage_id)) is None
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["generation"] == create.generation  # the counter is never reset


async def _seed_operation(store, operation_id: str, kind: str, *, resolved_at, name=None,
                          original_create: str | None = None, cleanup: str | None = None,
                          container: str | None = None) -> None:
    now = 1_000.0
    await store.db.execute(
        "INSERT INTO sandbox_operations (id, kind, deployment, profile_id, daemon_id, generation, "
        "generation_token, intended_name, original_create_operation_id, cleanup_operation_id, usage_id, "
        "usage_token, container_id, owner, owner_token, authority, outcome, resolved_at, created_at, updated_at) "
        "VALUES (?, ?, ?, 'p1', ?, 1, 'gen-token', ?, ?, ?, NULL, NULL, ?, 'recovery', ?, 'current', "
        "'definite_failure', ?, ?, ?)",
        operation_id, kind, DEPLOYMENT, DAEMON, name, original_create, cleanup, container,
        uuid.uuid4().hex, resolved_at, now, now)


async def _dangling_links(store) -> list[dict]:
    return await store.db.fetch(
        "SELECT o.id FROM sandbox_operations o WHERE "
        "(o.original_create_operation_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM sandbox_operations r WHERE r.id = o.original_create_operation_id)) "
        "OR (o.cleanup_operation_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM sandbox_operations r WHERE r.id = o.cleanup_operation_id))")


async def test_dependent_operations_prune_only_with_their_referrer(store, clock):
    clock.now = 1_100.0
    await _seed_operation(store, "op_create", "create", resolved_at=1_000.0, name="keel-sbx-one")
    await _seed_operation(store, "op_install", "install", resolved_at=None,
                          original_create="op_create", container=container_id())

    assert await store.prune_sandbox_records(0.0) == 0
    assert (await store.get_operation("op_create")) is not None

    await store.db.execute(
        "UPDATE sandbox_operations SET resolved_at = 1050.0 WHERE id = 'op_install'")
    clock.now = 1_200.0
    assert await store.prune_sandbox_records(0.0) == 1
    assert (await store.get_operation("op_install")) is None
    assert (await store.get_operation("op_create")) is not None
    assert await _dangling_links(store) == []
    assert await store.prune_sandbox_records(0.0) == 1
    assert (await store.get_operation("op_create")) is None
    assert await _dangling_links(store) == []


async def test_blocked_dependencies_do_not_starve_the_limit(store, clock):
    clock.now = 2_000.0
    for index in range(4):
        await _seed_operation(store, f"op_resolved_{index}", "install", resolved_at=1_000.0 + index,
                              container=container_id())
        await _seed_operation(store, f"op_referrer_{index}", "install", resolved_at=None,
                              cleanup=f"op_resolved_{index}", container=container_id())
    await _seed_operation(store, "op_eligible", "install", resolved_at=1_500.0, container=container_id())

    assert await store.prune_sandbox_records(0.0, limit=1) == 1

    assert (await store.get_operation("op_eligible")) is None
    for index in range(4):
        assert (await store.get_operation(f"op_resolved_{index}")) is not None
        assert (await store.get_operation(f"op_referrer_{index}")) is not None
    assert await _dangling_links(store) == []


async def test_pruning_honours_the_limit_and_retained_rows_do_not_starve(store, clock):
    now = 1_000.0
    clock.now = 2_000.0
    for index in range(3):
        await store.db.execute(
            "INSERT INTO sandbox_execs (id, control_token, usage_id, usage_token, deployment, profile_id, "
            "daemon_id, generation, generation_token, container_id, state, created_at, finished_at) "
            "VALUES (?, ?, 'use_x', 'tok_x', ?, 'p1', ?, 1, 'gen-token', ?, 'finished', ?, ?)",
            f"exec-{index}", uuid.uuid4().hex, DEPLOYMENT, DAEMON, container_id(), now, now)
    await _seed_operation(store, "op_unresolved", "create", resolved_at=None, name="keel-sbx-late")
    await _seed_operation(store, "op_resolved", "create", resolved_at=now, name="keel-sbx-done")
    await store.db.execute(
        "INSERT INTO sandboxes (deployment, profile_id, daemon_id, generation, state, transitioned_at) "
        "VALUES (?, 'p1', ?, 7, 'absent', ?)", DEPLOYMENT, DAEMON, now)

    assert await store.prune_sandbox_records(0.0, limit=2) == 2
    assert await store.prune_sandbox_records(0.0, limit=2) == 2
    assert await store.prune_sandbox_records(0.0, limit=2) == 0
    assert (await store.get_operation("op_unresolved")) is not None
    assert (await store.get_operation("op_resolved")) is None
    sandbox = await _one(
        store, "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["generation"] == 7
