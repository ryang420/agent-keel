"""The frozen coordination records shared by the store, coordinator and sandbox backend."""

from dataclasses import FrozenInstanceError, replace

import pytest

from keel.models import Usage
from keel.sandbox.base import Sandbox, SandboxError, SandboxPolicy
from keel.sandbox.records import (
    BoundSandbox, CleanupReceipt, ContainerObservation, DockerOutcome, ExecTicket, LeaseStamp, LifecycleAttempt,
    MAX_DIAGNOSTIC_CHARS, RecoveryAction, RecoveryReport, RunClaim, SandboxBinding, SandboxBusy, SandboxKey,
    SandboxLeaseLost, SandboxUncertain, SandboxUsage, UsageBinding, UseHandle,
)
from keel.store.base import Run

POLICY = SandboxPolicy(image="python:3.12-slim", workdir="/work")
KEY = SandboxKey(deployment="keel", profile_id="p1", daemon_id="daemon-1")
USE = UseHandle(key=KEY, usage_id="use_1", token="token-1", owner="worker-1")
BINDING = SandboxBinding(
    generation=3, generation_token="gen-token-3", container_id="c" * 64,
    container_name="keel-sbx-p1-gen-token-3", policy=POLICY, helper_digest="sha256:helper")
RUN = Run(
    id="run_1", profile_id="p1", session_id="s1", status="running", provider="fake", model="m",
    reasoning_effort=None, input=[], instructions=None, output="", usage=Usage(), error_code=None, error=None,
    lease_owner="worker-1", lease_expires_at=430.0, created_at=100.0, started_at=101.0, finished_at=None)


def test_coordination_identities_are_frozen():
    with pytest.raises(FrozenInstanceError):
        USE.usage_id = "use_2"
    with pytest.raises(FrozenInstanceError):
        USE.key = SandboxKey("keel", "p2", "daemon-1")
    with pytest.raises(FrozenInstanceError):
        BINDING.container_id = "d" * 64
    bound = BoundSandbox(profile_id="p1", name=BINDING.container_id, policy=POLICY, use=USE, binding=BINDING)
    with pytest.raises(FrozenInstanceError):
        bound.use = USE
    assert isinstance(bound, Sandbox)
    assert bound.name == BINDING.container_id
    assert bound.policy == BINDING.policy


def test_a_bound_sandbox_only_accepts_the_published_exact_identity():
    bound = BoundSandbox(profile_id="p1", name=BINDING.container_id, policy=POLICY, use=USE, binding=BINDING)
    assert bound.name == BINDING.container_id
    with pytest.raises(ValueError, match="full container ID"):
        BoundSandbox(profile_id="p1", name="keel-sbx-p1-reusable", policy=POLICY, use=USE, binding=BINDING)
    other = SandboxPolicy(image="python:3.11-slim", workdir="/work")
    with pytest.raises(ValueError, match="applied generation's policy"):
        BoundSandbox(profile_id="p1", name=BINDING.container_id, policy=other, use=USE, binding=BINDING)


def test_a_frozen_identity_can_be_replaced_without_mutating_the_original():
    moved = replace(BINDING, generation_token="gen-token-4")
    assert moved.generation_token == "gen-token-4"
    assert BINDING.generation_token == "gen-token-3"


def test_records_keep_their_fields():
    lease = LeaseStamp(observed_at=100.0, expires_at=130.0)
    assert (lease.observed_at, lease.expires_at) == (100.0, 130.0)

    claim = RunClaim(run=RUN, use=USE, lease=lease)
    assert claim.run.id == "run_1" and claim.use is USE and claim.lease == lease
    assert RunClaim(run=RUN, use=None, lease=lease).use is None  # chat-only claims have no usage

    usage_binding = UsageBinding(
        generation=BINDING.generation, generation_token=BINDING.generation_token,
        container_id=BINDING.container_id, container_name=BINDING.container_name)
    usage = SandboxUsage(
        id="use_1", token="token-1", key=KEY, run_id="run_1", owner="worker-1", state="active",
        heartbeat_at=100.0, lease_expires_at=130.0, session_id="s1", binding=usage_binding,
        create_operation_id="op_0")
    assert usage.binding == usage_binding and usage.create_operation_id == "op_0"
    assert usage.released_at is None and usage.release_reason is None
    assert not hasattr(usage.binding, "policy")

    attempt = LifecycleAttempt(
        id="op_1", kind="create", key=KEY, generation=3, generation_token="gen-token-3",
        authority="retired", outcome="uncertain", owner="worker-1", owner_token="owner-token-1",
        created_at=100.0, updated_at=110.0, intended_name="keel-sbx-p1-gen-token-3",
        original_create_operation_id=None, usage_id="use_1", usage_token="token-1",
        retirement_reason="replaced", result_message="x" * (MAX_DIAGNOSTIC_CHARS * 3))
    assert attempt.authority == "retired" and attempt.outcome == "uncertain"
    assert len(attempt.result_message) == MAX_DIAGNOSTIC_CHARS

    ticket = ExecTicket(exec_id="exec_1", use=USE, binding=BINDING, control_token="control-1")
    assert (ticket.exec_id, ticket.control_token) == ("exec_1", "control-1")
    assert ticket.binding.container_id == BINDING.container_id

    receipt = CleanupReceipt(exec_id="exec_1", control_token="control-1", leader_status=0, control_closed=True,
                             descendants_complete=True, uncertain_reason=None)
    assert receipt.descendants_complete and receipt.leader_status == 0
    assert CleanupReceipt("e", "c", None, True, False, "descendants remained").uncertain_reason is not None


def test_a_container_observation_has_no_environment():
    observation = ContainerObservation(
        daemon_id="daemon-1", container_id="c" * 64, container_name="keel-sbx-p1-gen-token-3",
        labels=(("keel.managed", "1"), ("keel.generation", "3")), running=True, policy_digest="policy-3")
    assert observation.labels == (("keel.managed", "1"), ("keel.generation", "3"))
    assert observation.running and observation.policy_digest == "policy-3"
    assert not hasattr(observation, "environment")


def test_docker_outcome_derives_uncertainty_from_its_state():
    succeed = DockerOutcome("succeeded", observation=ContainerObservation("daemon-1", "c1", "n"))
    assert succeed.uncertain is False
    assert DockerOutcome("definite_failure", diagnostic="no such container").uncertain is False
    assert DockerOutcome("uncertain", diagnostic="the CLI timed out").uncertain is True
    with pytest.raises(ValueError):
        DockerOutcome("maybe")
    assert len(DockerOutcome("uncertain", diagnostic="x" * (MAX_DIAGNOSTIC_CHARS * 4)).diagnostic) <= \
        MAX_DIAGNOSTIC_CHARS


def test_recovery_records_carry_evidence_not_names():
    action = RecoveryAction(
        key=KEY, action="remove", usage_id="use_1", usage_token="token-1", operation_id="op_1",
        container_id="c" * 64, generation=3, generation_token="gen-token-3", owner_token="owner-token-1",
        reason="retired create observed")
    assert action.container_id == "c" * 64
    assert not hasattr(action, "container_name")
    report = RecoveryReport(inspected=4, removed=2, released=1, blocked=0, unresolved=1, oldest_age_s=90.0,
                            diagnostics=("retired create remains", "y" * (MAX_DIAGNOSTIC_CHARS * 2)))
    assert (report.inspected, report.removed, report.released, report.unresolved) == (4, 2, 1, 1)
    assert report.oldest_age_s == 90.0 and report.diagnostics[0] == "retired create remains"
    assert len(report.diagnostics[1]) == MAX_DIAGNOSTIC_CHARS


def test_coordination_exceptions_are_sandbox_errors():
    for error in (SandboxBusy, SandboxLeaseLost, SandboxUncertain):
        assert issubclass(error, SandboxError)
