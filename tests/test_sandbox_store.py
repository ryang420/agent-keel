"""Guarded sandbox admission, usage leases and lifecycle intent (store task 2)."""

import asyncio
import os
from dataclasses import replace

import pytest

from keel.sandbox.base import parse_policy
from keel.sandbox.records import (
    ContainerObservation, DockerOutcome, SandboxBinding, SandboxBusy, SandboxKey, SandboxLeaseLost, UsageBinding,
    UseHandle,
)
from keel.store import open_store
from tests.sandbox_support import (
    DEFAULT_SANDBOX, TransactionProbe, container_id, make_claim, queue_run, seed_profile, seed_ready_sandbox,
)

DEPLOYMENT = "dep"
DAEMON = "daemon-1"


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


def _binding(sandbox: dict) -> SandboxBinding:
    return SandboxBinding(
        generation=sandbox["generation"], generation_token=sandbox["generation_token"],
        container_id=sandbox["container_id"], container_name=sandbox["container_name"],
        policy=sandbox["policy"], helper_digest="sha256:test-helper")



async def test_concurrent_first_claims_have_one_preparation_owner(store, clock):
    await seed_profile(store)
    first = await queue_run(store, "s1")
    clock.now += 1
    second = await queue_run(store, "s2")

    claims = await asyncio.gather(
        store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON),
        store.claim_run_with_usage("w2", 30, deployment=DEPLOYMENT, daemon_id=DAEMON),
    )
    claimed = [claim for claim in claims if claim is not None]

    assert len(claimed) == 1
    assert claimed[0].run.id == first.id  # ordered by created_at, id
    assert (await store.get_run_by_id(second.id)).status == "queued"
    usage = await store.get_usage(claimed[0].use.usage_id)
    assert usage.state == "preparing"
    assert usage.binding is None and usage.create_operation_id is None
    preparing = await store.db.fetchrow("SELECT COUNT(*) AS n FROM sandbox_uses WHERE state = 'preparing'")
    assert preparing["n"] == 1


async def test_a_ready_compatible_generation_is_shared_without_new_intent(store):
    await seed_profile(store)
    generation = await seed_ready_sandbox(store)
    first_run = await queue_run(store, "s1")
    second_run = await queue_run(store, "s2")

    first_claim = await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
    second_claim = await store.claim_run_with_usage("w2", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
    claims = [first_claim, second_claim]
    assert {claim.run.id for claim in claims} == {first_run.id, second_run.id}
    assert all(claim.use is not None for claim in claims)

    expected = UsageBinding(
        generation["generation"], generation["generation_token"], generation["container_id"],
        generation["container_name"])
    for claim in claims:
        usage = await store.get_usage(claim.use.usage_id)
        assert usage.state == "active"
        assert usage.binding == expected
    assert (await store.db.fetchrow("SELECT COUNT(*) AS n FROM sandboxes"))["n"] == 1
    assert (await store.db.fetchrow("SELECT COUNT(*) AS n FROM sandbox_operations"))["n"] == 0


async def test_generations_and_tokens_are_monotonic_and_never_reused(store):
    generations, tokens, names = [], [], []
    for index in range(3):
        claim = await make_claim(store, f"s{index}", owner=f"w{index}")
        attempt = await store.begin_lifecycle(
            claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
        sandbox = await store.db.fetchrow(
            "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
        generation = sandbox["generation"]
        generations.append(generation)
        tokens.append(sandbox["generation_token"])
        names.append(attempt.intended_name)
        assert sandbox["state"] == "creating"
        assert sandbox["current_operation_id"] == attempt.id
        assert (await store.get_usage(claim.use.usage_id)).create_operation_id == attempt.id
        # Arrange the removal/retirement that later tasks will record through guarded transitions.
        now = await store.db.database_now()
        await store.db.execute(
            "UPDATE sandbox_operations SET authority = 'retired', retired_at = ? WHERE id = ?", now, attempt.id)
        await store.db.execute(
            "UPDATE sandbox_uses SET state = 'released', released_at = ?, release_reason = 'test' WHERE id = ?",
            now, claim.use.usage_id)
        await store.db.execute(
            "UPDATE sandboxes SET state = 'absent', current_operation_id = NULL "
            "WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")

    assert generations == [1, 2, 3]
    assert len(set(tokens)) == 3 and all(len(token) == 32 for token in tokens)
    assert len(set(names)) == 3
    assert all(name.startswith("keel-sbx-") and name.endswith(token[:16]) for name, token in zip(names, tokens))


async def test_chat_only_profiles_claim_without_any_usage(store):
    claim = await make_claim(store, "s1", sandbox={})
    assert claim.use is None
    assert claim.run.status == "running" and claim.run.lease_owner == "w1"
    assert (await store.db.fetchrow("SELECT COUNT(*) AS n FROM sandbox_uses"))["n"] == 0


async def test_an_unreleased_preparing_usage_keeps_owning_preparation(store, clock):
    await make_claim(store, "s1")
    await store.db.execute(
        "UPDATE sandbox_uses SET lease_expires_at = ? WHERE state = 'preparing'",
        await store.db.database_now() - 1)
    clock.now += 1
    second = await queue_run(store, "s2")

    assert await store.claim_run_with_usage("w2", 30, deployment=DEPLOYMENT, daemon_id=DAEMON) is None
    assert (await store.get_run_by_id(second.id)).status == "queued"


async def test_an_active_preparation_operation_blocks_a_new_preparation_owner(store, clock):
    claim = await make_claim(store, "s1")
    await store.begin_lifecycle(claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    now = await store.db.database_now()
    await store.db.execute(
        "UPDATE sandbox_uses SET state = 'released', released_at = ?, release_reason = 'test' WHERE id = ?",
        now, claim.use.usage_id)
    clock.now += 1
    second = await queue_run(store, "s2")

    assert await store.claim_run_with_usage("w2", 30, deployment=DEPLOYMENT, daemon_id=DAEMON) is None
    assert (await store.get_run_by_id(second.id)).status == "queued"


@pytest.mark.parametrize("state", ["active", "cleaning"])
async def test_an_expired_unreleased_usage_blocks_admission_between_scans(store, clock, state):
    """Recovery owns orphaning; admission must not slip through after a lease expired but before a scan."""
    await seed_profile(store)
    await seed_ready_sandbox(store)
    await queue_run(store, "s1")
    claim = await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
    assert claim is not None and claim.use is not None
    await store.db.execute(
        "UPDATE sandbox_uses SET state = ?, lease_expires_at = ? WHERE id = ?",
        state, await store.db.database_now() - 1, claim.use.usage_id)
    clock.now += 1
    second = await queue_run(store, "s2")

    assert await store.claim_run_with_usage("w2", 30, deployment=DEPLOYMENT, daemon_id=DAEMON) is None
    assert (await store.get_run_by_id(second.id)).status == "queued"
    assert (await store.get_usage(claim.use.usage_id)).state == state  # admission never mutates


async def test_usage_renewal_rejects_expired_revoked_and_released_rows(store):
    claim = await make_claim(store, "s1")
    use = claim.use

    stamp = await store.renew_usage(use, 30)
    assert stamp is not None
    assert stamp.expires_at - stamp.observed_at == pytest.approx(30)
    assert abs(stamp.observed_at - await store.db.database_now()) < 60

    now = await store.db.database_now()
    await store.db.execute(
        "UPDATE sandbox_uses SET lease_expires_at = ? WHERE id = ?", now - 1, use.usage_id)
    assert await store.renew_usage(use, 30) is None

    await store.db.execute(
        "UPDATE sandbox_uses SET state = 'orphaned', lease_expires_at = ? WHERE id = ?", now + 60, use.usage_id)
    assert await store.renew_usage(use, 30) is None
    assert await store.renew_usage(UseHandle(use.key, use.usage_id, "wrong-token", use.owner), 30) is None

    await store.db.execute(
        "UPDATE sandbox_uses SET state = 'released', released_at = ?, release_reason = 'test', "
        "lease_expires_at = ? WHERE id = ?", now, now + 60, use.usage_id)
    assert await store.renew_usage(use, 30) is None


async def test_double_dispatch_is_rejected_and_records_one_outcome(store):
    claim = await make_claim(store, "s1")
    attempt = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)

    assert await store.authorize_dispatch(attempt) is True
    assert await store.authorize_dispatch(attempt) is False
    assert (await store.get_operation(attempt.id)).outcome == "dispatched"

    exact_id = container_id()
    saved = await store.record_lifecycle_result(
        attempt, DockerOutcome(state="succeeded", observation=ContainerObservation(
            daemon_id=DAEMON, container_id=exact_id, container_name="keel-sbx-late")))
    assert saved.outcome == "succeeded"
    assert saved.discovered_container_id == exact_id
    assert saved.container_id == exact_id
    # A created container stays unresolved until its absence is reconciled.
    assert saved.resolved_at is None


async def test_a_definite_create_failure_is_resolved(store):
    claim = await make_claim(store, "s1")
    attempt = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(attempt) is True

    saved = await store.record_lifecycle_result(
        attempt, DockerOutcome(state="definite_failure", diagnostic="no such image"))
    assert saved.outcome == "definite_failure"
    assert saved.resolved_at is not None


async def test_an_uncertain_create_stays_unresolved(store):
    claim = await make_claim(store, "s1")
    attempt = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(attempt) is True

    saved = await store.record_lifecycle_result(
        attempt, DockerOutcome(state="uncertain", diagnostic="lost acknowledgement"))
    assert saved.outcome == "uncertain"
    assert saved.resolved_at is None


async def test_stale_result_attaches_evidence_but_never_publishes(store):
    claim = await make_claim(store, "s1")
    attempt = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(attempt) is True
    now = await store.db.database_now()
    await store.db.execute(
        "UPDATE sandbox_operations SET authority = 'retired', retired_at = ? WHERE id = ?", now, attempt.id)

    exact_id = container_id()
    saved = await store.record_lifecycle_result(
        attempt, DockerOutcome(state="succeeded", observation=ContainerObservation(
            daemon_id=DAEMON, container_id=exact_id, container_name="keel-sbx-late")))
    assert saved.authority == "retired"
    assert saved.outcome == "dispatched"
    assert saved.discovered_container_id == exact_id
    assert saved.container_id == exact_id
    assert saved.retired_at == pytest.approx(now)
    sandbox = await store.db.fetchrow(
        "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "creating"
    assert sandbox["current_operation_id"] == attempt.id
    assert sandbox["generation"] == attempt.generation
    assert sandbox["container_id"] is None


async def test_operation_lease_and_retirement_control_dispatch(store):
    claim = await make_claim(store, "s1")
    attempt = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)

    stamp = await store.renew_operation(attempt, 30)
    assert stamp is not None and stamp.expires_at - stamp.observed_at == pytest.approx(30)
    await store.db.execute(
        "UPDATE sandbox_operations SET lease_expires_at = ? WHERE id = ?",
        await store.db.database_now() - 1, attempt.id)
    # An expired lease cannot be revived by renewal, and its dispatch stays refused.
    assert await store.renew_operation(attempt, 30) is None
    assert await store.authorize_dispatch(attempt) is False

    await store.db.execute(
        "UPDATE sandbox_operations SET authority = 'retired', retired_at = ? WHERE id = ?",
        await store.db.database_now(), attempt.id)
    assert await store.renew_operation(attempt, 30) is None


async def test_preparation_steps_share_the_create_identity_and_removal_records_target(store):
    claim = await make_claim(store, "s1")
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(create) is True
    exact_id = container_id()
    saved = await store.record_lifecycle_result(
        create, DockerOutcome(state="succeeded", observation=ContainerObservation(
            daemon_id=DAEMON, container_id=exact_id, container_name=create.intended_name)))
    assert saved.outcome == "succeeded" and saved.container_id == exact_id
    target = SandboxBinding(
        generation=create.generation, generation_token=create.generation_token, container_id=exact_id,
        container_name=create.intended_name, policy=parse_policy(DEFAULT_SANDBOX, mount_root="/"),
        helper_digest="sha256:test-helper")

    install = await store.begin_lifecycle(
        claim.use, "install", target=target, create_operation_id=create.id, lease_seconds=30)
    assert install.intended_name == create.intended_name
    assert install.original_create_operation_id == create.id
    assert install.container_id == exact_id
    assert await store.authorize_dispatch(install) is True
    install_saved = await store.record_lifecycle_result(install, DockerOutcome(state="succeeded"))
    assert install_saved.outcome == "succeeded"
    assert install_saved.resolved_at is not None

    start = await store.begin_lifecycle(
        claim.use, "start", target=target, create_operation_id=create.id, lease_seconds=30)
    sandbox = await store.db.fetchrow(
        "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "creating"
    assert sandbox["current_operation_id"] == start.id
    assert await store.authorize_dispatch(start) is True
    start_saved = await store.record_lifecycle_result(start, DockerOutcome(state="succeeded"))
    assert start_saved.resolved_at is not None

    remove = await store.begin_lifecycle(
        claim.use, "remove", target=target, create_operation_id=create.id, lease_seconds=30)
    sandbox = await store.db.fetchrow(
        "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "removing"
    assert sandbox["current_operation_id"] == remove.id
    assert remove.container_id == exact_id
    assert await store.authorize_dispatch(remove) is True
    remove_saved = await store.record_lifecycle_result(remove, DockerOutcome(state="succeeded"))
    assert remove_saved.resolved_at is not None


@pytest.mark.parametrize("case", ["wrong-generation", "wrong-token", "wrong-container", "foreign-container"])
async def test_remove_requires_recorded_target_evidence(store, case):
    claim = await make_claim(store, "s1")
    create, target = await _created(store, claim)
    if case == "wrong-generation":
        target = replace(target, generation=target.generation + 1)
    elif case == "wrong-token":
        target = replace(target, generation_token="0" * 32)
    elif case == "wrong-container":
        target = replace(target, container_id=container_id())
    else:
        foreign = await seed_ready_sandbox(store, profile_id="p2")
        target = replace(target, container_id=foreign["container_id"],
                         container_name=foreign["container_name"])

    with pytest.raises(SandboxBusy):
        await store.begin_lifecycle(
            claim.use, "remove", target=target, create_operation_id=create.id, lease_seconds=30)
    removes = await store.db.fetchrow(
        "SELECT COUNT(*) AS n FROM sandbox_operations WHERE kind = 'remove'")
    assert removes["n"] == 0

    with pytest.raises(ValueError):
        await store.begin_lifecycle(
            claim.use, "remove", target=replace(target, container_id=""), create_operation_id=create.id,
            lease_seconds=30)


@pytest.mark.parametrize("outcome", ["uncertain", "succeeded"])
async def test_retired_create_evidence_authorizes_removal_of_its_observed_container(store, outcome):
    claim = await make_claim(store, "s1")
    create = await store.begin_lifecycle(
        claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
    assert await store.authorize_dispatch(create) is True
    exact_id = container_id()
    saved = await store.record_lifecycle_result(
        create, DockerOutcome(state=outcome, observation=ContainerObservation(
            daemon_id=DAEMON, container_id=exact_id, container_name=create.intended_name)))
    assert saved.container_id == exact_id
    # Recovery retires the old attempt, then removes its observed exact ID before replacement.
    now = await store.db.database_now()
    await store.db.execute(
        "UPDATE sandbox_operations SET authority = 'retired', retired_at = ?, "
        "retirement_reason = 'replacement' WHERE id = ?", now, create.id)
    target = SandboxBinding(
        generation=create.generation, generation_token=create.generation_token, container_id=exact_id,
        container_name=create.intended_name, policy=parse_policy(DEFAULT_SANDBOX, mount_root="/"),
        helper_digest="sha256:test-helper")

    remove = await store.begin_lifecycle(
        claim.use, "remove", target=target, create_operation_id=create.id, lease_seconds=30)

    assert remove.kind == "remove"
    assert remove.authority == "current" and remove.outcome == "planned"
    assert remove.container_id == exact_id
    assert (await store.get_operation(create.id)).authority == "retired"
    sandbox = await store.db.fetchrow(
        "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "removing"
    assert sandbox["current_operation_id"] == remove.id

    for wrong in (replace(target, container_id=container_id()),
                  replace(target, generation=target.generation + 1)):
        with pytest.raises(SandboxBusy):
            await store.begin_lifecycle(
                claim.use, "remove", target=wrong, create_operation_id=create.id, lease_seconds=30)


async def test_removal_retires_in_flight_preparation_authority(store):
    claim = await make_claim(store, "s1")
    create, target = await _created(store, claim)
    start = await store.begin_lifecycle(
        claim.use, "start", target=target, create_operation_id=create.id, lease_seconds=30)

    remove = await store.begin_lifecycle(
        claim.use, "remove", target=target, create_operation_id=create.id, lease_seconds=30)

    assert remove.authority == "current"
    retired_start = await store.get_operation(start.id)
    assert retired_start.authority == "retired"
    assert retired_start.outcome == "planned"
    assert retired_start.retired_at is not None
    assert retired_start.retirement_reason == "generation_removal"
    assert (await store.get_operation(create.id)).authority == "retired"
    assert await store.authorize_dispatch(start) is False
    assert await store.authorize_dispatch(remove) is True

    sandbox = await store.db.fetchrow(
        "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert sandbox["state"] == "removing"
    assert sandbox["current_operation_id"] == remove.id


async def test_start_intent_after_recorded_removal_is_refused(store):
    claim = await make_claim(store, "s1")
    create, target = await _created(store, claim)
    remove = await store.begin_lifecycle(
        claim.use, "remove", target=target, create_operation_id=create.id, lease_seconds=30)

    with pytest.raises(SandboxBusy):
        await store.begin_lifecycle(
            claim.use, "start", target=target, create_operation_id=create.id, lease_seconds=30)
    assert await store.authorize_dispatch(remove) is True
    starts = await store.db.fetchrow(
        "SELECT COUNT(*) AS n FROM sandbox_operations WHERE kind = 'start'")
    assert starts["n"] == 0


@pytest.mark.parametrize("case", ["generation", "token", "container", "create-id", "foreign-container"])
async def test_preparation_requires_the_original_create_and_exact_target(store, case):
    claim = await make_claim(store, "s1")
    create, target = await _created(store, claim)
    create_id = create.id
    if case == "generation":
        target = replace(target, generation=target.generation + 1)
    elif case == "token":
        target = replace(target, generation_token="0" * 32)
    elif case == "container":
        target = replace(target, container_id=container_id())
    elif case == "create-id":
        create_id = "op_unknown"
    else:
        foreign = await seed_ready_sandbox(store, profile_id="p2")
        target = replace(target, container_id=foreign["container_id"],
                         container_name=foreign["container_name"])

    with pytest.raises(SandboxBusy):
        await store.begin_lifecycle(
            claim.use, "start", target=target, create_operation_id=create_id, lease_seconds=30)
    starts = await store.db.fetchrow(
        "SELECT COUNT(*) AS n FROM sandbox_operations WHERE kind = 'start'")
    assert starts["n"] == 0


async def test_cleaning_usage_may_only_record_removal(store):
    await seed_profile(store)
    generation = await seed_ready_sandbox(store)
    await queue_run(store, "s1")
    claim = await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
    assert claim is not None and claim.use is not None
    target = _binding(generation)
    await store.db.execute(
        "UPDATE sandbox_uses SET state = 'cleaning' WHERE id = ?", claim.use.usage_id)

    for kind, step_target in (("create", None), ("install", target), ("start", target)):
        with pytest.raises(SandboxLeaseLost):
            await store.begin_lifecycle(
                claim.use, kind, target=step_target, create_operation_id=None, lease_seconds=30)
    assert (await store.db.fetchrow(
        "SELECT COUNT(*) AS n FROM sandbox_operations WHERE kind <> 'remove'"))["n"] == 0

    remove = await store.begin_lifecycle(
        claim.use, "remove", target=target, create_operation_id=None, lease_seconds=30)
    assert remove.kind == "remove" and remove.authority == "current"


async def test_lifecycle_refuses_wrong_owner_and_expired_usage(store):
    await seed_profile(store)
    generation = await seed_ready_sandbox(store)
    await queue_run(store, "s1")
    claim = await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
    assert claim is not None and claim.use is not None
    target = _binding(generation)
    wrong_owner = UseHandle(claim.use.key, claim.use.usage_id, "0" * 32, claim.use.owner)

    with pytest.raises(SandboxLeaseLost):
        await store.begin_lifecycle(
            wrong_owner, "remove", target=target, create_operation_id=None, lease_seconds=30)

    await store.db.execute(
        "UPDATE sandbox_uses SET lease_expires_at = ? WHERE id = ?",
        await store.db.database_now() - 1, claim.use.usage_id)
    with pytest.raises(SandboxLeaseLost):
        await store.begin_lifecycle(
            claim.use, "remove", target=target, create_operation_id=None, lease_seconds=30)
    assert (await store.db.fetchrow(
        "SELECT COUNT(*) AS n FROM sandbox_operations"))["n"] == 0


async def test_renew_claim_does_not_resurrect_a_lapsed_lease(store):
    claim = await make_claim(store, "s1")
    assert await store.renew_claim(claim, 30) is not None
    await store.db.execute(
        "UPDATE runs SET lease_expires_at = ? WHERE id = ?",
        await store.db.database_now() - 1, claim.run.id)
    assert await store.renew_claim(claim, 30) is None


async def test_a_blocked_profile_at_the_queue_head_does_not_block_a_healthy_one(store, clock):
    await seed_profile(store, "p1")
    blocked_run = await queue_run(store, "s1", profile_id="p1")
    await store.db.execute(
        "INSERT INTO sandboxes (deployment, profile_id, daemon_id, generation, state, transitioned_at) "
        "VALUES (?, 'p1', ?, 0, 'blocked', ?)", DEPLOYMENT, DAEMON, await store.db.database_now())
    clock.now += 1
    await seed_profile(store, "p2")
    healthy_run = await queue_run(store, "s2", profile_id="p2")

    claim = await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)

    assert claim is not None and claim.run.id == healthy_run.id
    assert claim.run.profile_id == "p2"
    assert (await store.get_run_by_id(blocked_run.id)).status == "queued"


async def test_chat_only_profile_claims_despite_a_leftover_sandbox_record_on_another_daemon(store):
    await seed_profile(store, "p1", sandbox={})
    await seed_ready_sandbox(store, profile_id="p1", daemon_id="daemon-2")
    drifted_run = await queue_run(store, "s1", profile_id="p1")

    claim = await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)

    assert claim is not None and claim.run.id == drifted_run.id
    assert claim.use is None
    assert (await store.get_run_by_id(drifted_run.id)).status == "running"
    assert (await store.db.fetchrow("SELECT COUNT(*) AS n FROM sandbox_uses"))["n"] == 0


async def test_claim_skips_a_sandbox_recorded_on_another_daemon(store, clock):
    await seed_profile(store, "p1")
    await seed_ready_sandbox(store, profile_id="p1", daemon_id="daemon-2")
    drifted_run = await queue_run(store, "s1", profile_id="p1")
    clock.now += 1
    await seed_profile(store, "p2")
    healthy_run = await queue_run(store, "s2", profile_id="p2")

    claim = await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)

    assert claim is not None and claim.run.id == healthy_run.id
    assert claim.use is not None and claim.use.key.profile_id == "p2"
    assert (await store.get_run_by_id(drifted_run.id)).status == "queued"
    drifted_uses = await store.db.fetchrow(
        "SELECT COUNT(*) AS n FROM sandbox_uses WHERE profile_id = 'p1'")
    assert drifted_uses["n"] == 0


async def test_postgres_skip_locked_claim_moves_to_a_later_profile(store, clock):
    if store.db.dialect != "postgres":
        pytest.skip("a row-lock barrier needs PostgreSQL")
    await seed_profile(store, "p1")
    blocked_run = await queue_run(store, "s1", profile_id="p1")
    clock.now += 1
    await seed_profile(store, "p2")
    healthy_run = await queue_run(store, "s2", profile_id="p2")

    barrier = await open_store(os.environ["KEEL_TEST_POSTGRES_DSN"], clock=clock)
    try:
        async with barrier.db.transaction():
            locked = await barrier.db.fetchrow(
                "SELECT id FROM profiles WHERE id = ? FOR UPDATE", "p1")
            assert locked is not None
            claim = await asyncio.wait_for(
                store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON),
                timeout=5)
            assert claim is not None and claim.run.id == healthy_run.id
            assert (await store.get_run_by_id(blocked_run.id)).status == "queued"
    finally:
        await barrier.close()

    later = await store.claim_run_with_usage("w2", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
    assert later is not None and later.run.id == blocked_run.id


async def test_host_wall_clock_skew_cannot_change_lease_decisions(store, clock):
    claim = await make_claim(store, "s1")
    assert claim.lease.observed_at > 1_000_000_000  # database time, not the legacy clock at 1000

    clock.now += 5_000_000  # an arbitrary host wall-clock jump
    observed = await store.db.database_now()
    run_stamp = await store.renew_claim(claim, 30)
    usage_stamp = await store.renew_usage(claim.use, 30)
    assert run_stamp is not None and usage_stamp is not None
    assert abs(run_stamp.observed_at - observed) < 60
    assert abs(usage_stamp.observed_at - observed) < 60
    assert run_stamp.expires_at - run_stamp.observed_at == pytest.approx(30)

    await store.db.execute(
        "UPDATE sandbox_uses SET lease_expires_at = ? WHERE id = ?",
        await store.db.database_now() - 1, claim.use.usage_id)
    assert await store.renew_usage(claim.use, 30) is None


async def test_store_methods_never_await_a_backend_inside_a_transaction(store):
    probe = TransactionProbe(store.db)
    store.db = probe
    try:
        await seed_profile(store)
        run = await queue_run(store, "s1")
        await probe.backend_operation("before-claim")
        claim = await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
        await probe.backend_operation("after-claim")
        assert claim is not None and claim.run.id == run.id
        attempt = await store.begin_lifecycle(
            claim.use, "create", target=None, create_operation_id=None, lease_seconds=30)
        await probe.backend_operation("after-begin")
        assert await store.authorize_dispatch(attempt) is True
        await probe.backend_operation("after-authorize")
        assert await store.renew_usage(claim.use, 30) is not None
        await probe.backend_operation("after-renew")
    finally:
        store.db = probe.inner

    # Task 2's store has no Docker/model calls; every transaction closes before control returns to the caller.
    assert probe.depth == 0
    assert all(depth == 0 for _, depth in probe.backend_observations)


async def _prepared(store, claim):
    """Drive create, install and start to definite successes, ready for conditional publication."""
    create, target = await _created(store, claim)
    install = await store.begin_lifecycle(
        claim.use, "install", target=target, create_operation_id=create.id, lease_seconds=30)
    assert await store.authorize_dispatch(install) is True
    await store.record_lifecycle_result(
        install, DockerOutcome(state="succeeded", observation=ContainerObservation(
            daemon_id=DAEMON, container_id=target.container_id, container_name=target.container_name,
            running=False)))
    start = await store.begin_lifecycle(
        claim.use, "start", target=target, create_operation_id=create.id, lease_seconds=30)
    assert await store.authorize_dispatch(start) is True
    started = await store.record_lifecycle_result(
        start, DockerOutcome(state="succeeded", observation=ContainerObservation(
            daemon_id=DAEMON, container_id=target.container_id, container_name=target.container_name,
            running=True)))
    assert started.outcome == "succeeded"
    return create, target, started


async def _sandbox(store):
    return await store.db.fetchrow(
        "SELECT * FROM sandboxes WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")


async def test_publication_binds_the_usage_and_records_the_ready_generation(store):
    claim = await make_claim(store, "s1")
    create, target, start = await _prepared(store, claim)

    bound = await store.publish_generation(claim.use, target, start.id)

    assert bound.binding == target
    assert bound.use == claim.use
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "active"
    assert usage.binding == UsageBinding(
        target.generation, target.generation_token, target.container_id, target.container_name)
    row = await _sandbox(store)
    assert row["state"] == "ready"
    assert row["generation"] == target.generation
    assert row["generation_token"] == target.generation_token
    assert row["container_id"] == target.container_id
    assert row["container_name"] == target.container_name
    assert row["policy_digest"] == target.policy.digest()
    assert row["helper_digest"] == target.helper_digest
    assert row["helper_protocol"] == 1
    assert row["current_operation_id"] is None and row["idle_since"] is None
    assert (await store.db.fetchrow(
        "SELECT COUNT(*) AS n FROM sandboxes WHERE state = 'ready'"))["n"] == 1
    assert (await store.get_operation(create.id)).outcome == "succeeded"


async def test_publication_requires_a_still_valid_unbound_preparation_usage(store):
    claim = await make_claim(store, "s1")
    create, target, start = await _prepared(store, claim)

    stale = replace(claim.use, token="0" * 32)
    with pytest.raises(SandboxLeaseLost):
        await store.publish_generation(stale, target, start.id)

    expired = replace(claim.use)
    await store.db.execute(
        "UPDATE sandbox_uses SET lease_expires_at = ? WHERE id = ?",
        await store.db.database_now() - 1, claim.use.usage_id)
    with pytest.raises(SandboxLeaseLost):
        await store.publish_generation(expired, target, start.id)

    await store.db.execute(
        "UPDATE sandbox_uses SET lease_expires_at = ? WHERE id = ?",
        await store.db.database_now() + 60, claim.use.usage_id)
    await store.publish_generation(claim.use, target, start.id)
    with pytest.raises(SandboxLeaseLost):  # a second publication is not a preparation owner
        await store.publish_generation(claim.use, target, start.id)


async def test_publication_requires_a_succeeded_current_exact_start(store):
    claim = await make_claim(store, "s1")
    create, target = await _created(store, claim)
    install = await store.begin_lifecycle(
        claim.use, "install", target=target, create_operation_id=create.id, lease_seconds=30)
    assert await store.authorize_dispatch(install) is True
    await store.record_lifecycle_result(install, DockerOutcome(state="succeeded"))

    planned = await store.begin_lifecycle(
        claim.use, "start", target=target, create_operation_id=create.id, lease_seconds=30)
    with pytest.raises(SandboxBusy):  # never authorized
        await store.publish_generation(claim.use, target, planned.id)

    assert await store.authorize_dispatch(planned) is True
    await store.record_lifecycle_result(planned, DockerOutcome(state="uncertain", diagnostic="timed out"))
    with pytest.raises(SandboxBusy):
        await store.publish_generation(claim.use, target, planned.id)

    await store.db.execute(
        "UPDATE sandbox_operations SET outcome = 'succeeded' WHERE id = ?", planned.id)
    with pytest.raises(SandboxBusy):  # an unknown start id authorizes nothing
        await store.publish_generation(claim.use, target, "op_missing")
    await store.db.execute(
        "UPDATE sandbox_operations SET authority = 'retired', retired_at = ? WHERE id = ?",
        await store.db.database_now(), planned.id)
    with pytest.raises(SandboxBusy):
        await store.publish_generation(claim.use, target, planned.id)
    assert (await _sandbox(store))["state"] == "creating"
    assert (await store.get_usage(claim.use.usage_id)).state == "preparing"


async def test_a_stale_publication_cannot_clear_a_newer_generation(store):
    claim = await make_claim(store, "s1")
    create, target, start = await _prepared(store, claim)
    newer_id = container_id()
    await store.db.execute(
        "UPDATE sandboxes SET generation = generation + 1, generation_token = ?, container_id = ?, "
        "container_name = ?, state = 'ready' WHERE deployment = ? AND profile_id = ?",
        "1" * 32, newer_id, "keel-sbx-newer", DEPLOYMENT, "p1")
    before = await _sandbox(store)

    with pytest.raises(SandboxBusy):
        await store.publish_generation(claim.use, target, start.id)

    assert await _sandbox(store) == before
    usage = await store.get_usage(claim.use.usage_id)
    assert usage.state == "preparing" and usage.binding is None
    mismatched = replace(target, container_id=container_id())
    with pytest.raises(SandboxBusy):
        await store.publish_generation(claim.use, mismatched, start.id)


async def test_published_generation_reconstruction_checks_daemon_policy_and_helper(store):
    await seed_profile(store)
    generation = await seed_ready_sandbox(store, helper_digest="sha256:test-helper")
    policy = parse_policy(DEFAULT_SANDBOX, mount_root="/")
    key = SandboxKey(DEPLOYMENT, "p1", DAEMON)

    binding = await store.get_published_generation(key, policy)
    assert binding == _binding(generation)

    other_daemon = SandboxKey(DEPLOYMENT, "p1", "daemon-2")
    assert await store.get_published_generation(other_daemon, policy) is None
    other_policy = parse_policy({"image": "other", "workdir": "/w"}, mount_root="/")
    assert await store.get_published_generation(key, other_policy) is None

    await store.db.execute(
        "UPDATE sandboxes SET helper_protocol = 2 WHERE deployment = ? AND profile_id = ?",
        DEPLOYMENT, "p1")
    assert await store.get_published_generation(key, policy) is None

    await store.db.execute(
        "UPDATE sandboxes SET helper_protocol = NULL, policy_digest = 'not-a-digest' "
        "WHERE deployment = ? AND profile_id = ?", DEPLOYMENT, "p1")
    assert await store.get_published_generation(key, policy) is None


async def test_a_ready_generation_with_a_null_helper_record_is_still_reconstructible(store):
    await seed_profile(store)
    generation = await seed_ready_sandbox(store)
    policy = parse_policy(DEFAULT_SANDBOX, mount_root="/")

    binding = await store.get_published_generation(SandboxKey(DEPLOYMENT, "p1", DAEMON), policy)

    assert binding is not None and binding.helper_digest == ""
    assert binding.container_id == generation["container_id"]
