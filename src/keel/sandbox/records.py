"""Frozen coordination records shared by the store, coordinator and sandbox backend.

These values are read models and authority handles, never cached validity: a handle names a usage or generation, and
the store rechecks its lease, token and state before accepting work. Diagnostic fields are bounded and must never
carry argv, source code, environment values or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass

from keel.sandbox.base import Sandbox, SandboxError, SandboxPolicy
from keel.store.base import Run

SANDBOX_STATES = ("absent", "creating", "ready", "draining", "removing", "blocked")
USAGE_STATES = ("preparing", "active", "cleaning", "orphaned", "released")
EXEC_STATES = ("pending", "running", "finished", "uncertain", "retired")
OPERATION_KINDS = ("create", "install", "start", "remove")
AUTHORITIES = ("current", "retired")
OUTCOMES = ("planned", "dispatched", "succeeded", "definite_failure", "uncertain")
DOCKER_OUTCOME_STATES = ("succeeded", "definite_failure", "uncertain")
MAX_DIAGNOSTIC_CHARS = 512


class SandboxBusy(SandboxError):
    """The profile still has queued/running/stopping runs or unreleased usage; the request must wait."""


class SandboxLeaseLost(SandboxError):
    """This owner's lease expired or was revoked; it must not dispatch or publish more work."""


class SandboxUncertain(SandboxError):
    """An external sandbox operation has an unknown outcome; recovery must reconcile it before reuse."""


@dataclass(frozen=True)
class SandboxKey:
    """The coordination identity of one profile's sandbox on one deployment and Docker daemon."""

    deployment: str
    profile_id: str
    daemon_id: str


@dataclass(frozen=True)
class UseHandle:
    """Immutable usage authority: an identity to present, not a claim that its lease is still valid."""

    key: SandboxKey
    usage_id: str
    token: str
    owner: str


@dataclass(frozen=True)
class LeaseStamp:
    """Database observation time and the deadline derived from it."""

    observed_at: float
    expires_at: float


@dataclass(frozen=True)
class SandboxBinding:
    """The coordinator's full execution handle for a published generation."""

    generation: int
    generation_token: str
    container_id: str
    container_name: str
    policy: SandboxPolicy
    helper_digest: str


@dataclass(frozen=True)
class UsageBinding:
    """The store's identity-only binding from design section 5.2.

    A usage row records the generation and exact container, not the applied policy or helper artifact. Those belong
    to the published generation; never fabricate them from the profile's current policy.
    """

    generation: int
    generation_token: str
    container_id: str
    container_name: str


@dataclass(frozen=True)
class BoundSandbox(Sandbox):
    """A published generation plus the usage authorized to execute in it.

    The inherited `name` holds the full container ID for compatibility; it is never executable authority.
    """

    use: UseHandle
    binding: SandboxBinding

    def __post_init__(self) -> None:
        if self.name != self.binding.container_id:
            raise ValueError("a bound sandbox name must be the full container ID, never a reusable name")
        if self.policy != self.binding.policy:
            raise ValueError("a bound sandbox policy must be the applied generation's policy")


@dataclass(frozen=True)
class RunClaim:
    """One claimed run. Chat-only claims have no usage and need no Docker coordination."""

    run: Run
    use: UseHandle | None
    lease: LeaseStamp


@dataclass(frozen=True)
class SandboxUsage:
    """A usage read model; callers must use `UseHandle` to renew, release or execute."""

    id: str
    token: str
    key: SandboxKey
    run_id: str
    owner: str
    state: str
    heartbeat_at: float
    lease_expires_at: float
    session_id: str | None = None
    binding: UsageBinding | None = None
    create_operation_id: str | None = None
    released_at: float | None = None
    release_reason: str | None = None


@dataclass(frozen=True)
class LifecycleAttempt:
    """Durable evidence of one create/install/start/remove attempt (design section 5.4)."""

    id: str
    kind: str
    key: SandboxKey
    generation: int
    generation_token: str
    authority: str
    outcome: str
    owner: str
    owner_token: str
    created_at: float
    updated_at: float
    intended_name: str | None = None
    original_create_operation_id: str | None = None
    usage_id: str | None = None
    usage_token: str | None = None
    container_id: str | None = None
    lease_expires_at: float | None = None
    retired_at: float | None = None
    retirement_reason: str | None = None
    result_code: str | None = None
    result_message: str | None = None
    discovered_container_id: str | None = None
    cleanup_operation_id: str | None = None
    confirmed_absent_at: float | None = None
    resolved_at: float | None = None
    run_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.result_message is not None and len(self.result_message) > MAX_DIAGNOSTIC_CHARS:
            object.__setattr__(self, "result_message", self.result_message[:MAX_DIAGNOSTIC_CHARS])


@dataclass(frozen=True)
class ExecTicket:
    exec_id: str
    use: UseHandle
    binding: SandboxBinding
    control_token: str


@dataclass(frozen=True)
class CleanupReceipt:
    exec_id: str
    control_token: str
    leader_status: int | None
    control_closed: bool
    descendants_complete: bool
    uncertain_reason: str | None


@dataclass(frozen=True)
class ContainerObservation:
    """What an exact-ID inspection reports; it carries no environment or command contents."""

    daemon_id: str
    container_id: str
    container_name: str
    labels: tuple[tuple[str, str], ...] = ()
    running: bool = False
    policy_digest: str | None = None


@dataclass(frozen=True)
class DockerOutcome:
    """A definite success/failure, or an uncertainty that recovery must resolve before reuse."""

    state: str
    observation: ContainerObservation | None = None
    receipt: CleanupReceipt | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.state not in DOCKER_OUTCOME_STATES:
            raise ValueError(f"unknown Docker outcome {self.state!r}")
        if self.diagnostic is not None and len(self.diagnostic) > MAX_DIAGNOSTIC_CHARS:
            object.__setattr__(self, "diagnostic", self.diagnostic[:MAX_DIAGNOSTIC_CHARS])

    @property
    def uncertain(self) -> bool:
        return self.state == "uncertain"


@dataclass(frozen=True)
class RecoveryAction:
    """A typed recovery step against recorded identities; it never instructs removal by a reusable name."""

    key: SandboxKey
    action: str
    usage_id: str | None = None
    usage_token: str | None = None
    operation_id: str | None = None
    container_id: str | None = None
    generation: int | None = None
    generation_token: str | None = None
    owner_token: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RecoveryReport:
    """One bounded maintenance pass: counters, oldest unresolved age and bounded diagnostics."""

    inspected: int = 0
    removed: int = 0
    released: int = 0
    blocked: int = 0
    unresolved: int = 0
    oldest_age_s: float | None = None
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        bounded = tuple(entry[:MAX_DIAGNOSTIC_CHARS] for entry in self.diagnostics)
        if bounded != self.diagnostics:
            object.__setattr__(self, "diagnostics", bounded)
