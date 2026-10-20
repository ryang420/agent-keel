"""Bounded recovery orchestration; Docker awaits never occur inside coordination transactions."""

from __future__ import annotations

import asyncio
import logging

from keel.sandbox.base import SandboxError
from keel.sandbox.records import DockerOutcome, RecoveryAction, RecoveryReport, SandboxKey
from keel.store.base import ProfileDeletionConflict
from keel.store.deletions import ProfileDeletionAuthority

log = logging.getLogger(__name__)


def _deletion_kwargs(authority: ProfileDeletionAuthority | None) -> dict[str, ProfileDeletionAuthority]:
    return {} if authority is None else {"deletion_authority": authority}


def confirmed_absent(outcome: DockerOutcome) -> bool:
    """Only the backend's explicit exact-ID missing contract is absence evidence."""
    return (outcome.state == "definite_failure" and outcome.observation is None
            and outcome.diagnostic == "the container does not exist")


class SandboxRecoveryMixin:
    """Used by the coordinator; service scheduling remains the worker's responsibility."""

    async def reconcile_once(self, *, limit: int = 100) -> RecoveryReport:
        return await self._recovery_pass(limit=limit, idle=False)

    async def reap_idle_once(self, *, limit: int = 100) -> RecoveryReport:
        return await self._recovery_pass(limit=limit, idle=True)

    async def recover_run(self, run_id: str) -> None:
        use = await self.store.get_run_usage(run_id)
        if use is None:
            return
        # A terminal run does not prove its payloads gone. This only reconciles expired or already orphaned
        # authority; an owner's normal cancellation/release remains responsible for healthy executions.
        await self._recover_key(use.key)

    async def _recovery_pass(self, *, limit: int, idle: bool) -> RecoveryReport:
        limit = max(0, min(limit, 100))
        if not limit:
            return RecoveryReport()
        # No daemon initialization is needed to retire proven unbound preparations during an outage.
        candidates = await self.store.recovery_candidates(limit=limit, purpose="idle" if idle else "recovery")
        inspected = removed = released = blocked = 0
        diagnostics = []
        if not idle:
            self._inventory_pending = None
            try:
                daemon_id, _ = await self.backend.daemon_identity()
                if self._daemon_id is not None and daemon_id != self._daemon_id:
                    # Still classify: records naming the initialized daemon are blocked as a daemon mismatch.
                    self._note_daemon_changed(daemon_id)
                inventory = await self.backend.inventory()
                verified: list[SandboxKey] = []
                cursor = getattr(self, "_inventory_cursor", "")
                ordered = sorted(inventory, key=lambda item: item.container_id)
                selected = ([item for item in ordered if item.container_id > cursor]
                            + [item for item in ordered if item.container_id <= cursor])[:limit]
                if selected:
                    self._inventory_cursor = selected[-1].container_id
                seen_daemon, seen = self._inventory_seen
                if seen_daemon != daemon_id:
                    seen = set()
                    self._inventory_seen = (daemon_id, seen)
                for observation in selected:
                    if dict(observation.labels).get("keel.deployment") != self.deployment:
                        seen.add(observation.container_id)
                        continue
                    inspected += 1
                    action = await self.store.classify_inventory(
                        observation, owner=self.owner, lease_seconds=self.lease_seconds)
                    if action is None or action.action != "skipped":
                        seen.add(observation.container_id)  # classified now; a locked profile is retried later
                    if action is not None and action.action == "protected":
                        verified.append(action.key)
                    elif action is not None and action.action == "blocked":
                        blocked += 1
                        diagnostics.append(action.reason or "sandbox_inventory_conflict")
                    elif action is not None and action.action == "remove":
                        removed += await self._recover_action(action)
                # A conflict recorded on another container clears only after that exact ID is proven absent on
                # the same daemon: an exact-ID inspection cannot be hidden by a missing deployment label the
                # way a deployment-filtered list can.
                if verified:
                    for key, offending in await self.store.blocked_inventory_targets(
                            verified, daemon_id=daemon_id):
                        if await self._listing_keeps_block(key, inventory):
                            blocked += 1  # another container of this profile is still unclassified or conflicting
                            continue
                        try:
                            outcome = await self.backend.inspect_id(offending)
                            if not self._offender_settled(outcome, offending, inventory):
                                continue
                            confirmed_daemon, _ = await self.backend.daemon_identity()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            blocked += 1
                            continue
                        if confirmed_daemon == daemon_id:
                            await self.store.clear_resolved_inventory_block(
                                key, daemon_id=daemon_id, offending=offending)
                # A cycle completes only when every container of a fresh listing has been classified and the
                # daemon identity read after the scan is still the one read before it.
                after, _ = await self.backend.daemon_identity()
                if after != daemon_id:
                    self._note_daemon_changed(after)
                    raise SandboxError("the Docker daemon identity changed during the inventory")
                self._inventory_pending = sum(1 for item in inventory if item.container_id not in seen)
                if not self._inventory_pending:
                    self._inventory_daemon = daemon_id
            except asyncio.CancelledError:
                raise
            except Exception:
                diagnostics.append("sandbox_inventory_unavailable")
                blocked += 1
        for key in candidates:
            if key.deployment != self.deployment:
                continue
            try:
                if idle:
                    _, artifact = await self._ready()
                    if key.daemon_id != self._daemon_id:
                        blocked += 1
                        diagnostics.append("sandbox_daemon_mismatch")
                        continue
                    actions = await self.store.plan_idle_removal(
                        key, owner=self.owner, lease_seconds=self.lease_seconds,
                        idle_seconds=self.idle_seconds, helper_digest=artifact.sha256)
                    for action in actions:
                        removed += await self._recover_action(action)
                else:
                    counts = await self._recover_key(key)
                    inspected += counts[0]
                    removed += counts[1]
                    released += counts[2]
                    blocked += counts[3]
                    if counts[3]:
                        diagnostics.append("sandbox_recovery_blocked")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fixed diagnostic codes intentionally exclude raw driver/backend exception text.
                diagnostics.append("sandbox_recovery_unavailable")
                blocked += 1
        unresolved, oldest = await self.store.recovery_diagnostics(self.deployment)
        return RecoveryReport(inspected=inspected, removed=removed, released=released, blocked=blocked,
                              unresolved=unresolved, oldest_age_s=oldest, diagnostics=tuple(diagnostics[:100]))

    def _note_daemon_changed(self, observed: str) -> None:
        """Close sandbox admission for this process: a different daemon never proves the old containers absent,
        and replacing the daemon requires an explicit stopped-service reconciliation (design section 14)."""
        if not self._daemon_changed:
            log.error("the Docker daemon identity changed from %r to %r; sandbox admission is closed until the "
                      "recorded daemon is restored and the service restarted", self._daemon_id, observed)
        self._daemon_changed = True

    async def _recover_key(self, key: SandboxKey, *,
                           deletion_authority: ProfileDeletionAuthority | None = None
                           ) -> tuple[int, int, int, int]:
        if deletion_authority is not None:
            async with self.store.profile_deletion_guard(key.profile_id, deletion_authority):
                pass
        row, before = await self.store.recovery_snapshot(key)
        inspected = removed = blocked = 0
        # Match the configured daemon afresh before interpreting a missing ID. Cached initialization does not
        # authorize absence on a replaced daemon. Even an outage still permits section 11.1 retirement below.
        available = False
        try:
            daemon, _ = await self.backend.daemon_identity()
            available = daemon == key.daemon_id
            if not available:
                blocked += 1
        except Exception:
            blocked += 1
        if available and row is not None and row["container_id"] is not None:
            inspected += 1
            outcome = await self.backend.inspect_id(row["container_id"])
            if confirmed_absent(outcome):
                observed_daemon, _ = await self.backend.daemon_identity()
                if observed_daemon == key.daemon_id:
                    await self.store.confirm_generation_missing(
                        key, row["container_id"], **_deletion_kwargs(deletion_authority))
                else:
                    available = False
                    blocked += 1
            elif outcome.observation is not None:
                action = await self.store.classify_inventory(
                    outcome.observation, owner=self.owner, lease_seconds=self.lease_seconds,
                    **_deletion_kwargs(deletion_authority))
                if action is not None and action.action == "blocked":
                    blocked += 1
                elif action is not None and action.action == "remove":
                    removed += await self._recover_action(action, deletion_authority=deletion_authority)
            else:
                blocked += 1
        if available and row is not None and row["container_id"] is None:
            blocked += await self._resolve_unpublished_block(key, deletion_authority=deletion_authority)
        actions = await self.store.plan_recovery(
            key, owner=self.owner, lease_seconds=self.lease_seconds, **_deletion_kwargs(deletion_authority))
        _, orphaned = await self.store.recovery_snapshot(key)
        for use in orphaned:
            if use.state in ("orphaned", "released"):
                try:
                    await self._finish_unavailable(use.run_id, deletion_authority=deletion_authority)
                except Exception:
                    blocked += 1  # released recovery usages retain the unfinished run for a later retry
        for action in actions:
            if action.action != "remove" or available:
                removed += await self._recover_action(action, deletion_authority=deletion_authority)
        # The guarded planner marks orphans before returning. Terminal transitions happen afterwards, in their
        # own transactions, preserving profile-before-run lock ordering and the existing single terminal event.
        _, after = await self.store.recovery_snapshot(key)
        released = 0
        for use in before:
            current = await self.store.get_usage(use.id)
            if current is None or current.state not in ("orphaned", "released"):
                continue
            try:
                await self._finish_unavailable(use.run_id, deletion_authority=deletion_authority)
            except Exception:
                blocked += 1  # the finished usage keeps its unfinished run for a later retry
            released += current.state == "released" and use.state != "released"
        if row is not None and row.get("state") == "draining" and any(
                use.state in ("active", "cleaning") for use in after):
            blocked += 1
        # Replacement of incompatible policy/helper artifacts also happens during ordinary recovery passes.
        if available:
            try:
                _, artifact = await self._ready()
                for action in await self.store.plan_idle_removal(
                        key, owner=self.owner, lease_seconds=self.lease_seconds,
                        idle_seconds=float("inf"), helper_digest=artifact.sha256,
                        **_deletion_kwargs(deletion_authority)):
                    removed += await self._recover_action(action, deletion_authority=deletion_authority)
            except ProfileDeletionConflict:
                raise
            except Exception:
                blocked += 1
        return inspected, removed, released, blocked

    async def _resolve_unpublished_block(self, key: SandboxKey, *,
                                         deletion_authority: ProfileDeletionAuthority | None = None) -> int:
        """Clear a conflict on a profile with no published generation once its exact ID is proven absent.

        Returns 1 while the block stays. The recorded ID settles only when the daemon explicitly reports it absent,
        confirmed on the same daemon, or when a fresh exact-ID inspection shows it present but stopped and a
        complete listing no longer classifies it as conflicting; a failed inspection keeps the block.
        """
        offending = await self.store.unpublished_inventory_block(key)
        if offending is None:
            return 0
        try:
            # The block names one offender; others of this profile may sit outside any bounded scan batch (or
            # this may be the sweep's path, which scans nothing). A complete fresh listing must show none.
            listing = await self.backend.inventory()
            if await self._listing_keeps_block(key, listing, deletion_authority=deletion_authority):
                return 1
            outcome = await self.backend.inspect_id(offending)
            if not self._offender_settled(outcome, offending, listing):
                return 1
            daemon, _ = await self.backend.daemon_identity()
        except asyncio.CancelledError:
            raise
        except ProfileDeletionConflict:
            raise
        except Exception:
            return 1
        if daemon != key.daemon_id or any(item.daemon_id != daemon for item in listing):
            return 1
        return 0 if await self.store.clear_resolved_inventory_block(
            key, daemon_id=daemon, offending=offending,
            **_deletion_kwargs(deletion_authority)) else 1

    @staticmethod
    def _offender_settled(outcome: DockerOutcome, offending: str, listing) -> bool:
        """The recorded offender is proven absent by exact ID, or it is still present but stopped and in the
        complete listing, where `_listing_keeps_block` has just classified it as no longer conflicting. The
        exact-ID observation is newer than the listing: a container started since then still keeps the block. A
        present offender missing from the deployment's listing (its deployment label was changed) is never
        settled."""
        if confirmed_absent(outcome):
            return True
        return (outcome.observation is not None and not outcome.observation.running
                and any(item.container_id == offending for item in listing))

    async def _listing_keeps_block(self, key: SandboxKey, observations, *,
                                   deletion_authority: ProfileDeletionAuthority | None = None) -> bool:
        """True while a complete, unbatched listing holds a container of this profile that must keep its block.

        Every listed container that names the profile, or whose exact ID the ledger records for it, is classified
        with the same guarded classifier admission relies on. An unknown container, a recorded one whose labels or
        policy disagree, a running retired create, or a profile another instance holds (`skipped`) keeps the block,
        whichever single offender the block happens to record. The published generation (`protected`) does not, nor
        does a stopped retired create that matches its ledger row: it is quarantined and removed by exact ID once
        the block is gone (the classifier reports it as the profile's existing block, without a container of its
        own), as section 9 lets historical stopped-only creates proceed separately.
        """
        recorded = await self.store.recorded_container_ids(key)
        for item in observations:
            labels = dict(item.labels)
            if item.container_id not in recorded and labels.get("keel.profile") != key.profile_id:
                continue
            action = await self.store.classify_inventory(
                item, owner=self.owner, lease_seconds=self.lease_seconds,
                **_deletion_kwargs(deletion_authority))
            if action is None or action.action == "skipped":
                return True
            if action.action == "blocked" and action.container_id is not None:
                return True
            if action.action == "remove":
                await self._recover_action(action, deletion_authority=deletion_authority)
        return False

    async def _finish_unavailable(self, run_id: str, *,
                                  deletion_authority: ProfileDeletionAuthority | None = None) -> None:
        transition = await self.store.finish_run(
            run_id, "failed", error_code="sandbox_unavailable", error="The sandbox generation is unavailable.",
            **_deletion_kwargs(deletion_authority))
        if transition is not None and transition.event is not None and self.bus is not None:
            try:
                await self.bus.notify(run_id)
            except Exception:
                pass  # persisted terminal evidence remains authoritative if notification fails

    async def _recover_action(self, action: RecoveryAction, *,
                              deletion_authority: ProfileDeletionAuthority | None = None) -> int:
        if action.action != "remove":
            await self.store.commit_recovery(
                action, DockerOutcome("succeeded"), **_deletion_kwargs(deletion_authority))
            return 0
        # Never replay an uncertain remove. Its exact target is inspected first, and only a new ledger attempt
        # may dispatch a further remove if the old request still has a live result.
        daemon, _ = await self.backend.daemon_identity()
        if daemon != action.key.daemon_id:
            return 0
        outcome = await self.backend.inspect_id(action.container_id)
        if outcome.observation is not None:
            observation = outcome.observation
            if observation.container_id != action.container_id or observation.daemon_id != action.key.daemon_id:
                await self.store.classify_inventory(
                    observation, owner=self.owner, lease_seconds=self.lease_seconds,
                    **_deletion_kwargs(deletion_authority))
                return 0
            classified = await self.store.classify_inventory(
                observation, owner=self.owner, lease_seconds=self.lease_seconds,
                **_deletion_kwargs(deletion_authority))
            if classified is not None and classified.action == "blocked":
                return 0
        elif not confirmed_absent(outcome):
            return 0
        observed_daemon, _ = await self.backend.daemon_identity()
        if observed_daemon != action.key.daemon_id:
            return 0
        original = await self.store.get_operation(action.operation_id)
        if original is None or original.owner_token != action.owner_token:
            return 0
        if not confirmed_absent(outcome) and original.outcome in ("dispatched", "uncertain"):
            action = await self.store.rearm_recovery_remove(
                action, **_deletion_kwargs(deletion_authority))
            if action is None:
                return 0
        if not await self.store.authorize_recovery_remove(action, **_deletion_kwargs(deletion_authority)):
            return 0
        attempt = await self.store.get_operation(action.operation_id)
        if confirmed_absent(outcome):
            return int(await self.store.commit_recovery(
                action, DockerOutcome("succeeded"), **_deletion_kwargs(deletion_authority)))
        async def heartbeat():
            while True:
                await asyncio.sleep(max(0.01, self.lease_seconds / 3))
                if await self.store.renew_operation(attempt, self.lease_seconds) is None:
                    return
        beat = asyncio.create_task(heartbeat())
        try:
            result = await self.backend.remove_id(attempt)
        except asyncio.CancelledError:
            raise
        except Exception:
            result = DockerOutcome("uncertain", diagnostic="sandbox_remove_unverified")
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
        # A lost Docker acknowledgement can still be settled by a fresh authoritative missing-ID observation.
        checked = await self.backend.inspect_id(action.container_id)
        if confirmed_absent(checked):
            observed_daemon, _ = await self.backend.daemon_identity()
            if observed_daemon != action.key.daemon_id:
                return 0
            result = DockerOutcome("succeeded")
        elif result.state == "succeeded":
            result = DockerOutcome("uncertain", diagnostic="sandbox_absence_unconfirmed")
        return int(await self.store.commit_recovery(action, result, **_deletion_kwargs(deletion_authority)))
