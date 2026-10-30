"""Retryable, tenant-owned complete profile deletion.

The service walks the durable phases from `keel.store.deletions`:

1. the fence is claimed in the store (phase ``blocked``);
2. queued and active runs are cancelled through the run state machine, this
   profile's background work is cancelled and awaited, and its sandboxes are
   retired through the lifecycle coordinator (phase ``draining``);
3. every owned transcript, memory, blob, lifecycle record and profile row is
   removed, and the phase becomes ``purged`` in that same transaction.

The fence is released only by `complete`, called by the consumer after it has
removed its own data. Every step is idempotent and resumes from the persisted
phase, so a lost response or a restart never starts a second cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import time

from keel.sandbox.records import SandboxBusy
from keel.store.base import ProfileDeletionConflict
from keel.store.deletions import ProfileDeletionAuthority

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 0.05


class ProfileDeletionBusyError(RuntimeError):
    """Work is still draining; the same operation may be retried."""


class ProfileDeletionUnavailableError(RuntimeError):
    """A cleanup dependency cannot answer; both fences stay in place."""


class ProfileDeletionService:
    """Drain, retire and purge one profile, then acknowledge its completion."""

    def __init__(self, store, *, coordinator=None, bus=None, background=None,
                 poll_seconds: float = POLL_SECONDS) -> None:
        self.store = store
        self.coordinator = coordinator
        self.bus = bus
        self.background = background
        self.poll_seconds = poll_seconds

    async def delete(self, profile_id: str, tenant_id: int, operation_id: str, *,
                     timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        """Fence, drain, retire and purge; leave the fence for `complete`."""
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        profile = await self.store.get_profile(profile_id)
        record = await self.store.begin_profile_deletion(profile_id, tenant_id, operation_id)
        if record.phase == "complete":
            return
        authority = ProfileDeletionAuthority(
            operation_id=record.operation_id,
            expected_incarnation=profile.incarnation if profile is not None else None)
        # Block new background work before draining: a run that just committed its terminal state must not spawn
        # a title behind the drain's back.
        if self.background is not None and not (profile is None and record.phase == "purged"):
            async with self.store.profile_deletion_guard(
                    profile_id, authority, phases=("blocked", "draining")):
                self.background.block_profile(profile_id, operation_id, authority.expected_incarnation)
        if record.phase == "blocked":
            record = await self._advance(record, "draining")
        if record.phase == "draining":
            await self._drain(profile_id, deadline, authority)
            await self._drain_background(profile_id, deadline, authority)
            await self._retire(profile_id, deadline, authority)
            # This commits phase `purged` only with the deletes themselves.
            await self.store.purge_profile_data(record)

    async def _advance(self, record, next_phase: str):
        """Advance, tolerating a concurrent retry of the same operation."""
        try:
            return await self.store.advance_profile_deletion(record, next_phase)
        except ProfileDeletionConflict:
            current = await self.store.get_profile_deletion(record.profile_id)
            if current is None or current.operation_id != record.operation_id:
                raise
            return current

    async def complete(self, profile_id: str, tenant_id: int, operation_id: str) -> None:
        """Release only this operation's fence; a retry is idempotent."""
        await self.store.complete_profile_deletion(profile_id, tenant_id, operation_id)
        if self.background is not None:
            self.background.unblock_profile(profile_id, operation_id)

    async def _drain(self, profile_id: str, deadline: float, authority: ProfileDeletionAuthority) -> None:
        """Cancel queued runs and wait for active workers to end their runs."""
        notified: set[tuple[str, str]] = set()
        while True:
            active = await self.store.list_runs(profile_id, ("queued", "running", "stopping"))
            if not active:
                return
            for run in active:
                transition = await self.store.request_stop(
                    profile_id, run.id, deletion_authority=authority)
                if transition is None:
                    continue
                key = (run.id, transition.run.status)
                if key not in notified:
                    notified.add(key)
                    await self._notify(run.id)
                if transition.run.status == "stopping":
                    await self._notify("stop:" + run.id)
            if time.monotonic() >= deadline:
                raise ProfileDeletionBusyError(profile_id)
            await asyncio.sleep(min(self.poll_seconds, max(0.0, deadline - time.monotonic())))

    async def _drain_background(self, profile_id: str, deadline: float,
                                authority: ProfileDeletionAuthority) -> None:
        """Cancel and await this profile's background titles before purging its rows. Reviews are fenced in the database."""
        if self.background is None:
            return
        try:
            drained = await self.background.drain_profile(
                profile_id, timeout=max(0.0, deadline - time.monotonic()),
                profile_incarnation=authority.expected_incarnation)
        except Exception as exc:
            log.warning("draining profile background work failed profile=%s", profile_id, exc_info=True)
            raise ProfileDeletionUnavailableError(profile_id) from exc
        if not drained:
            raise ProfileDeletionBusyError(profile_id)

    async def _retire(self, profile_id: str, deadline: float, authority: ProfileDeletionAuthority) -> None:
        """Retire every sandbox generation, or keep the deletion incomplete."""
        if self.coordinator is None:
            return
        remaining = max(0.0, deadline - time.monotonic())
        try:
            await self.coordinator.retire_profile(
                profile_id, timeout_seconds=remaining, deletion_authority=authority)
        except SandboxBusy as exc:
            raise ProfileDeletionBusyError(profile_id) from exc
        except ProfileDeletionBusyError:
            raise
        except ProfileDeletionConflict:
            raise
        except Exception as exc:  # a broken dependency is retryable, never success
            log.warning("retiring profile sandboxes failed profile=%s", profile_id, exc_info=True)
            raise ProfileDeletionUnavailableError(profile_id) from exc

    async def _notify(self, key: str) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.notify(key)
        except Exception:
            # Persisted run state remains authoritative if live notification fails.
            pass
