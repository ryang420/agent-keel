"""Coordinate claims, sandbox preparation and conditional publication across keel instances.

A claim reserves one usage. `prepare` then drives a fresh generation through three separately recorded external
steps, create, helper install and start, never holding a database transaction across a Docker await. Each step is
authorized under the profile guard, dispatched once while its operation lease is renewed independently, and its
result recorded conditionally before the next step may begin. Publication rechecks the exact generation, the
create and start attempts and the still-valid preparing usage; a caller that lost ownership records evidence on
its own attempt and cannot start, publish or clear another generation.

Removal, reconciliation of an uncertain step and policy replacement are recovery work: an uncertain start blocks
publication here, and its exact target must be reconciled or removed before anything reuses it.
"""

from __future__ import annotations

import asyncio
import logging

from keel.sandbox.base import (
    CoordinatedSandboxBackend, ExecFileIO, ExecResult, PolicyError, SandboxError, parse_policy,
)
from keel.sandbox.records import (
    BoundSandbox, CleanupReceipt, ContainerObservation, DockerOutcome, ExecTicket, LeaseStamp, LifecycleAttempt,
    RunClaim, SandboxBinding,
    SandboxBusy, SandboxLeaseLost, SandboxUncertain, UseHandle,
)
from keel.sandbox.recovery import SandboxRecoveryMixin
from keel.sandbox.runtime import HELPER_PROTOCOL_VERSION, HelperArtifact, helper_artifact
from keel.store.sql import SqlStore

log = logging.getLogger(__name__)
# Docker transport allowed for one helper control request on top of the TERM grace and the descendant reap timeout.
CONTROL_TRANSPORT_S = 15.0


def _observe(control: asyncio.Future) -> None:
    if not control.cancelled():
        control.exception()


class SandboxCoordinator(SandboxRecoveryMixin, ExecFileIO):
    """One instance's authority to admit, prepare and publish a profile's sandbox generation."""

    def __init__(self, store: SqlStore, backend: CoordinatedSandboxBackend, *, deployment: str, owner: str,
                 lease_seconds: float, idle_seconds: float, reap_seconds: float, bus=None) -> None:
        if not deployment:
            raise ValueError("a deployment identifier is required")
        if not owner:
            raise ValueError("a coordinator owner is required")
        if lease_seconds <= 0:
            raise ValueError("the coordination lease must be positive")
        self.bus = bus
        self.store = store
        self.backend = backend
        self.deployment = deployment
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.idle_seconds = idle_seconds
        self.reap_seconds = reap_seconds
        self._daemon_id: str | None = None
        self._artifact: HelperArtifact | None = None
        self._initialize_lock = asyncio.Lock()
        # Sandbox admission opens only once this instance has classified a complete inventory of the daemon it
        # initialized against; a later pass that reads another daemon identity closes it for this process.
        self._inventory_daemon: str | None = None
        self._inventory_seen: tuple[str | None, set[str]] = (None, set())
        self._inventory_pending: int | None = None  # observations of the current cycle not yet classified
        self._daemon_changed = False
        # One cancellation control per (usage, exec), fixed at its first request and bounded by this budget.
        # Later requests join it (or read its outcome); entries go when the usage is released.
        self.control_budget_s = getattr(backend, "kill_grace_s", 3.0) + reap_seconds + CONTROL_TRANSPORT_S
        self._controls: dict[tuple[str, str], asyncio.Task] = {}

    async def initialize(self) -> None:
        """Cache the checked daemon identity and helper artifact; fail closed when the deployment cannot be served.

        This performs no database work, so a slow or unreachable Docker daemon never holds a transaction. The
        helper manifest must name an artifact for the daemon's architecture under the current protocol; otherwise
        every admission path refuses to run.
        """
        if self._daemon_id is not None and self._artifact is not None:
            return
        async with self._initialize_lock:
            if self._daemon_id is not None and self._artifact is not None:
                return
            try:
                daemon_id, architecture = await self.backend.daemon_identity()
            except SandboxError:
                raise
            except Exception as exc:
                raise SandboxError(f"could not read the Docker daemon identity: {exc}") from exc
            if (not isinstance(daemon_id, str) or not daemon_id or len(daemon_id) > 128
                    or any(character.isspace() for character in daemon_id)):
                raise SandboxError("the Docker daemon returned an invalid identity")
            artifact = helper_artifact(architecture)
            if artifact.protocol != HELPER_PROTOCOL_VERSION:
                raise SandboxError(
                    f"the sandbox helper protocol {artifact.protocol} is not supported")
            self._daemon_id = daemon_id
            self._artifact = artifact

    async def claim(self) -> RunClaim | None:
        """Claim one queued run and reserve its usage, sharing a compatible ready generation when one exists.

        The published helper digest is part of compatibility: a generation carrying another helper binary is left
        for recovery to replace rather than reused with the wrong cleanup evidence.

        A claim never waits on Docker. Until `open_admission` has initialized the daemon identity and helper and
        classified a complete inventory, sandbox admission is withheld: sandbox-enabled runs stay queued (they
        never run tool-less) and chat-only runs are still claimed.
        """
        if not self.admitting():
            return await self.store.claim_run_with_usage(
                self.owner, self.lease_seconds, deployment=self.deployment, daemon_id=None)
        return await self.store.claim_run_with_usage(
            self.owner, self.lease_seconds, deployment=self.deployment, daemon_id=self._daemon_id,
            helper_digest=self._artifact.sha256)

    def admitting(self) -> bool:
        """True once sandbox admission is open for the initialized daemon (no Docker call)."""
        return (self._daemon_id is not None and self._artifact is not None and not self._daemon_changed
                and self._inventory_daemon == self._daemon_id)

    async def retire_profile(self, profile_id: str, *, timeout_seconds: float) -> None:
        """Retire every sandbox generation of one profile, proving none remains.

        Uses the existing recovery planning and exact-ID dispatch. An uncertain
        removal, another daemon or unreleased usage keeps the deletion
        incomplete: nothing is ever removed by name.
        """
        keys = await self.store.profile_sandbox_keys(profile_id, deployment=self.deployment)
        if not keys:
            return
        await self._ready()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout_seconds))
        while keys:
            progressed = False
            for key in list(keys):
                if key.daemon_id != self._daemon_id:
                    raise SandboxBusy("the profile's sandbox belongs to another daemon")
                counts = await self._recover_key(key)
                # `_recover_key`'s final idle pass only replaces incompatible
                # generations. A retirement must also remove a ready generation
                # whose policy still matches, so plan that removal explicitly
                # with a zero idle threshold.
                try:
                    _, artifact = await self._ready()
                    for action in await self.store.plan_idle_removal(
                            key, owner=self.owner, lease_seconds=self.lease_seconds, idle_seconds=0.0,
                            helper_digest=artifact.sha256):
                        await self._recover_action(action)
                        counts = (counts[0], counts[1] + 1, counts[2], counts[3])
                except SandboxBusy:
                    raise
                except Exception:
                    pass  # the next pass retries; a bounded wait decides the outcome
                row, usages = await self.store.recovery_snapshot(key)
                if (row is None or row["state"] == "absent") and not any(
                        use.state != "released" for use in usages):
                    keys.remove(key)
                elif sum(counts) > 0:
                    progressed = True
            if not keys:
                return
            if loop.time() >= deadline:
                raise SandboxBusy("the profile's sandbox is not quiescent")
            if not progressed:
                await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))

    async def open_admission(self) -> bool:
        """Initialize, then run bounded recovery passes until the whole inventory has been classified.

        Each pass covers at most 100 containers; passes continue while they make progress, so an inventory of any
        size is covered before sandbox admission opens. Returns whether admission is open. Docker failures leave it
        closed (the caller retries later); unknown or conflicting containers found on the way block only their own
        profile. Callers bound the wait; work done so far is kept for the next attempt.
        """
        try:
            await self.initialize()
        except SandboxError:
            log.warning("sandbox admission is withheld: the Docker daemon or the sandbox helper is unavailable",
                        exc_info=True)
            return False
        remaining = None
        while not self.admitting():
            report = await self.reconcile_once(limit=100)
            pending = self._inventory_pending
            if (self._daemon_changed or pending is None or "sandbox_inventory_unavailable" in report.diagnostics
                    or (remaining is not None and pending >= remaining)):
                return self.admitting()
            remaining = pending
        return True

    async def renew_usage(self, use: UseHandle) -> LeaseStamp | None:
        """Renew one matching, unexpired usage; a lost or released usage is never revived."""
        return await self.store.renew_usage(use, self.lease_seconds)

    async def prepare(self, use: UseHandle) -> BoundSandbox:
        """Return a bound handle for `use`, preparing and publishing a new generation when needed."""
        _, artifact = await self._ready()
        self._require_handle(use)
        usage = await self.store.get_usage(use.usage_id)
        if (usage is None or usage.token != use.token or usage.owner != use.owner or usage.key != use.key):
            raise SandboxLeaseLost(f"usage {use.usage_id!r} is not owned by this coordinator")
        if usage.state == "active" and usage.binding is not None:
            return await self._bind_existing(use, usage, artifact)
        if usage.state != "preparing" or usage.binding is not None:
            raise SandboxLeaseLost(f"usage {use.usage_id!r} is not a preparation owner")
        if usage.create_operation_id is not None:
            # A recorded create means an earlier prepare left external work behind. Its exact target must be
            # reconciled by recovery before a replacement may be prepared; never repeat creation here.
            raise SandboxUncertain(f"usage {use.usage_id!r} already has recorded preparation")
        policy = await self._profile_policy(use)
        return await self._prepare_generation(use, policy, artifact)

    async def exec(self, sandbox: BoundSandbox, argv: list[str], *, cwd: str, timeout_s: float,
                   exec_id: str, env: dict[str, str] | None = None, stdin: bytes | None = None) -> ExecResult:
        """Commit one pending ticket, dispatch it once, and finish only with matching cleanup evidence."""
        await self._ready()
        self._require_bound(sandbox)
        ticket = await self.store.begin_exec(sandbox, exec_id)

        async def authorize_control() -> None:
            current = await self.store.resolve_exec(sandbox, exec_id)
            if current != ticket:
                raise SandboxLeaseLost("the execution control identity changed")

        try:
            result, receipt = await self.backend.exec_bound(
                ticket, argv, cwd=cwd, timeout_s=timeout_s, env=env, stdin=stdin,
                authorize_control=authorize_control)
            await self._finish_exec(sandbox, ticket, receipt)
            return result
        except asyncio.CancelledError:
            try:
                await self.kill_tree(sandbox, exec_id)
            except Exception:
                await self._try_mark_exec_uncertain(ticket, "cancel_cleanup_unverified")
            raise
        except Exception as exc:
            await self._try_mark_exec_uncertain(ticket, "dispatch_or_cleanup_unverified")
            raise SandboxUncertain("the sandbox execution outcome is uncertain") from exc

    def _require_bound(self, sandbox: BoundSandbox) -> None:
        if not isinstance(sandbox, BoundSandbox):
            raise SandboxLeaseLost("coordinated execution requires a bound sandbox")
        self._require_handle(sandbox.use)
        if sandbox.profile_id != sandbox.use.key.profile_id:
            raise SandboxLeaseLost("the sandbox belongs to another usage profile")

    async def _try_mark_exec_uncertain(self, ticket: ExecTicket, reason: str) -> None:
        """Retain the primary failure if persistence is unavailable; the committed registry still protects it."""
        try:
            await self.store.mark_exec_uncertain(ticket, reason)
        except Exception:
            log.warning("could not persist uncertainty for sandbox exec %s; durable execution evidence retained",
                        ticket.exec_id)

    async def _finish_exec(self, sandbox: BoundSandbox, ticket: ExecTicket,
                           receipt: CleanupReceipt) -> None:
        if (not isinstance(receipt, CleanupReceipt) or receipt.exec_id != ticket.exec_id
                or receipt.control_token != ticket.control_token or not receipt.control_closed
                or not receipt.descendants_complete or receipt.uncertain_reason is not None):
            raise SandboxUncertain("the sandbox cleanup receipt is missing or unverified")
        if not await self.store.finish_exec(ticket, receipt):
            # Another completion/control client may already have finished the same original ticket. Resolution
            # rejects uncertain/retired rows and mismatched authority, so only that idempotent finish is accepted.
            if await self.store.resolve_exec(sandbox, ticket.exec_id) != ticket:
                raise SandboxUncertain("the sandbox execution receipt could not be recorded")

    async def kill_tree(self, sandbox: BoundSandbox, exec_id: str) -> None:
        """Cancel one exec through its supervisor, sending at most one control request for it.

        The first request starts a control bounded by `control_budget_s`; concurrent and later requests for the
        same exec join it and see its outcome, so a repeated stop never restarts the deadline or sends a second
        request. Cancelling a waiter does not cancel the control; on budget expiry it records the exec uncertain.
        """
        await self._ready()
        self._require_bound(sandbox)
        key = (sandbox.use.usage_id, exec_id)
        control = self._controls.get(key)
        if control is None:
            deadline = asyncio.get_running_loop().time() + self.control_budget_s
            control = asyncio.ensure_future(self._control(sandbox, exec_id, deadline))
            control.add_done_callback(_observe)  # waiters may have left; the outcome is recorded either way
            self._controls[key] = control
        await asyncio.shield(control)

    async def _control(self, sandbox: BoundSandbox, exec_id: str, deadline: float) -> None:
        """Resolve current authority from durable identities; successful signal delivery alone proves nothing."""
        try:
            ticket = await self.store.resolve_exec(sandbox, exec_id)
        except SandboxError:
            raise  # a genuine lease/binding rejection retains its authority failure type
        except Exception as exc:
            raise SandboxUncertain("the sandbox execution authority could not be verified") from exc
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            receipt = await asyncio.wait_for(self.backend.control_exec(ticket, cancel=True), remaining)
            await self._finish_exec(sandbox, ticket, receipt)
        except asyncio.CancelledError:
            await self._try_mark_exec_uncertain(ticket, "control_cancelled")
            raise
        except Exception as exc:
            await self._try_mark_exec_uncertain(ticket, "control_or_cleanup_unverified")
            raise SandboxUncertain("the sandbox cancellation outcome is uncertain") from exc

    async def release(self, use: UseHandle, *, abandon_preparation: bool = False) -> bool:
        """Release only guarded, proven cleanup; terminal run status never supplies execution evidence."""
        await self._ready()
        self._require_handle(use)
        for key in [key for key, control in self._controls.items() if key[0] == use.usage_id and control.done()]:
            del self._controls[key]
        if abandon_preparation:
            if await self.store.retire_unbound_preparation(use, abandon=True):
                return True
        if not await self.store.begin_usage_cleanup(use):
            usage = await self.store.get_usage(use.usage_id)
            if usage is None or usage.state != "cleaning":
                return False  # orphan release belongs to recovery, even when its generation is already gone
        return await self.store.release_usage(use)

    async def _ready(self) -> tuple[str, HelperArtifact]:
        await self.initialize()
        if self._daemon_id is None or self._artifact is None:
            raise SandboxError("the sandbox coordinator is not initialized")
        return self._daemon_id, self._artifact

    def _require_handle(self, use: UseHandle) -> None:
        if use.key.deployment != self.deployment:
            raise SandboxLeaseLost("the usage belongs to another deployment")
        if self._daemon_id is not None and use.key.daemon_id != self._daemon_id:
            raise SandboxLeaseLost("the usage belongs to another Docker daemon")

    async def _profile_policy(self, use: UseHandle):
        profile = await self.store.get_profile(use.key.profile_id)
        if profile is None:
            raise SandboxBusy(f"profile {use.key.profile_id!r} is unavailable")
        if not profile.sandbox:
            raise SandboxBusy(f"profile {use.key.profile_id!r} does not enable a sandbox")
        try:
            return parse_policy(profile.sandbox, mount_root="/")
        except (PolicyError, ValueError, TypeError) as exc:
            raise SandboxError(f"the sandbox policy is invalid: {exc}") from exc

    async def _bind_existing(self, use: UseHandle, usage, artifact: HelperArtifact) -> BoundSandbox:
        """A ready generation claimed by the store is already the applied generation; add no Docker work.

        The binding's policy and helper digest are read from the published generation (the profile's digest is
        rechecked), never assumed from the local artifact. The usage's recorded identity must still match it, and
        a generation recording another helper binary is not reused. A row published before helper recording
        (NULL digest) stays usable.
        """
        policy = await self._profile_policy(use)
        binding = await self.store.get_published_generation(use.key, policy)
        if binding is None or (binding.generation, binding.generation_token, binding.container_id) != (
                usage.binding.generation, usage.binding.generation_token, usage.binding.container_id):
            raise SandboxBusy("the usage is not bound to the currently published generation")
        if binding.helper_digest and binding.helper_digest != artifact.sha256:
            raise SandboxBusy("the published generation carries another sandbox helper")
        return BoundSandbox(profile_id=use.key.profile_id, name=binding.container_id, policy=binding.policy,
                            use=use, binding=binding)

    async def _prepare_generation(self, use: UseHandle, policy, artifact: HelperArtifact) -> BoundSandbox:
        create = await self.store.begin_lifecycle(
            use, "create", target=None, create_operation_id=None, lease_seconds=self.lease_seconds)
        created, created_outcome = await self._dispatch(
            create, lambda: self.backend.create_generation(create, policy))
        self._require_succeeded(created, created_outcome)
        container_id = created.container_id
        if container_id is None:
            raise SandboxUncertain("the create succeeded without recording an exact container ID")
        expected_name = created.intended_name or self._observed(created_outcome).container_name
        self._verify_observation(
            self._observed(created_outcome), created, policy, container_id=container_id,
            expected_name=expected_name, running=False)
        target = SandboxBinding(
            generation=created.generation, generation_token=created.generation_token, container_id=container_id,
            container_name=expected_name, policy=policy, helper_digest=artifact.sha256)

        install = await self.store.begin_lifecycle(
            use, "install", target=target, create_operation_id=created.id,
            lease_seconds=self.lease_seconds)
        installed, installed_outcome = await self._dispatch(
            install, lambda: self.backend.install_helper(install, artifact))
        self._require_succeeded(installed, installed_outcome)
        if installed.container_id != container_id:
            raise SandboxUncertain("the helper install recorded another container ID")

        start = await self.store.begin_lifecycle(
            use, "start", target=target, create_operation_id=created.id, lease_seconds=self.lease_seconds)
        started, started_outcome = await self._dispatch(start, lambda: self.backend.start_id(start))
        self._require_succeeded(started, started_outcome)
        self._verify_observation(
            self._observed(started_outcome), started, policy, container_id=container_id,
            expected_name=target.container_name, running=True)
        return await self.store.publish_generation(
            use, target, started.id, start_owner_token=started.owner_token)

    async def _dispatch(self, attempt: LifecycleAttempt,
                        action) -> tuple[LifecycleAttempt, DockerOutcome]:
        """Authorize, perform and record one external step while an independent heartbeat renews its lease.

        The heartbeat runs outside every transaction, so a long Docker call never holds the profile guard. Its
        loss stops this preparation after the returned evidence is durably attached to the original attempt; the
        external request itself is never repeated.
        """
        if not await self.store.authorize_dispatch(attempt):
            raise SandboxLeaseLost(f"operation {attempt.id!r} is no longer authorized to dispatch")
        lost = False

        async def heartbeat() -> None:
            nonlocal lost
            interval = max(self.lease_seconds / 3.0, 0.01)
            while True:
                await asyncio.sleep(interval)
                try:
                    stamp = await self.store.renew_operation(attempt, self.lease_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    lost = True
                    return
                if stamp is None:
                    lost = True
                    return

        beat = asyncio.create_task(heartbeat())
        try:
            outcome = await action()
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
        saved = await self.store.record_lifecycle_result(attempt, outcome)
        if (saved.owner_token == attempt.owner_token and saved.authority == "current"
                and saved.outcome == "dispatched"
                and (saved.lease_expires_at is None
                     or saved.lease_expires_at < await self.store.coordination_now())):
            raise SandboxLeaseLost(
                f"operation {attempt.id!r} lost its lease while the {attempt.kind} was in flight")
        if saved.owner_token != attempt.owner_token or saved.authority != "current" or saved.outcome != outcome.state:
            raise SandboxUncertain(
                f"the {attempt.kind} result arrived after its attempt was retired")
        if lost:
            raise SandboxLeaseLost(
                f"operation {attempt.id!r} lost its lease while the {attempt.kind} was in flight")
        return saved, outcome

    def _require_succeeded(self, attempt: LifecycleAttempt, outcome: DockerOutcome) -> None:
        if outcome.state == "definite_failure":
            raise SandboxError(
                f"the sandbox {attempt.kind} failed: {outcome.diagnostic or 'no diagnostic'}")
        if outcome.state == "uncertain":
            raise SandboxUncertain(
                f"the sandbox {attempt.kind} outcome is uncertain: {outcome.diagnostic or 'no diagnostic'}")
        if attempt.outcome != "succeeded":
            raise SandboxUncertain(f"the sandbox {attempt.kind} was not recorded as succeeded")

    def _observed(self, outcome: DockerOutcome) -> ContainerObservation:
        if outcome.observation is None:
            raise SandboxUncertain("the Docker operation did not report its container")
        return outcome.observation

    def _verify_observation(self, observation: ContainerObservation, attempt: LifecycleAttempt, policy,
                            *, container_id: str, expected_name: str, running: bool) -> None:
        """Verify the intended generation's identity before publishing any step of it.

        Every generation label must be present with its exact value and the policy digest must match: an
        observation that omits them (for example because an inspect could not read them) is never treated as
        matching. Docker merges an image's own labels into the container, so labels keel did not write are ignored;
        another keel label is still a conflict.
        """
        if observation.daemon_id != self._daemon_id:
            raise SandboxBusy("the Docker daemon identity changed during preparation")
        if observation.container_id != container_id:
            raise SandboxUncertain(f"the {attempt.kind} reported another container ID")
        if expected_name is not None and observation.container_name != expected_name:
            raise SandboxUncertain(f"the {attempt.kind} reported another container name")
        if observation.running != running:
            raise SandboxUncertain(f"the {attempt.kind} reported the wrong running state")
        digest = policy.digest()
        if observation.policy_digest != digest:
            raise SandboxError("the observed sandbox policy differs from the profile policy")
        expected = {
            "keel.managed": "1", "keel.deployment": self.deployment,
            "keel.profile": attempt.key.profile_id, "keel.policy": digest,
            "keel.generation": str(attempt.generation),
            "keel.generation-token": attempt.generation_token,
            "keel.create-operation": attempt.original_create_operation_id or attempt.id,
        }
        labels = dict(observation.labels)
        for name, value in expected.items():
            if labels.get(name) != value:
                raise SandboxUncertain(f"the {attempt.kind} observed another generation's labels")
        if any(name.startswith("keel.") and name not in expected for name in labels):
            raise SandboxUncertain(f"the {attempt.kind} observed another generation's labels")
