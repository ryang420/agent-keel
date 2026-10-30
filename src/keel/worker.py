"""Workers claim queued runs, keep their leases alive, honour stop requests, and sweep dead leases.

A sandbox-enabled worker claims through the coordinator, which reserves the run's usage in the same transaction.
Two leases are then renewed independently on database time: the run lease until the run is terminal, and the usage
lease until the executor's cleanup returns, even after the run has ended. Local stop deadlines come from the
database's observation plus elapsed monotonic time, never from the host wall clock.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time

from keel.sandbox.records import LeaseStamp, RunClaim

log = logging.getLogger(__name__)
_MAX_CLAIM_BACKOFF = 10.0
SHUTDOWN_MESSAGE = "The service was shutting down."
BACKFILL_BATCH = 200
BACKFILL_IDLE_S = 30.0
_BACKGROUND = ("keel-sweeper", "keel-maintenance", "keel-history-backfill")


@dataclass
class _Deadlines:
    """Local monotonic deadlines derived from the last confirmed lease of each kind, and the lease length granted.

    Renewal cadence follows what the database granted (the coordinator's lease for a coordinated claim), never a
    separately configured copy, so a lease cannot lapse between renewals.
    """

    run: float
    usage: float | None
    granted: float
    usage_granted: float


def _confirmed(sent: float, stamp: LeaseStamp) -> float:
    """The local deadline of a lease the database granted: the request's send time plus the granted interval.
    Counting from the send time, not the reply, keeps the local deadline at or before the database's."""
    return sent + (stamp.expires_at - stamp.observed_at)


class WorkerPool:
    def __init__(self, store, bus, executor, *, owner: str, workers: int = 4, lease_seconds: float = 30.0,
                 poll_seconds: float = 0.5, clock=time.time, monotonic=time.monotonic,
                 event_retention_s: float | None = None, maintenance_s: float = 3600.0, coordinator=None) -> None:
        if coordinator is None and getattr(executor, "coordinator", None) is not None:
            # A legacy claim reserves no usage; a sandbox-enabled worker must claim through the coordinator.
            raise ValueError("a sandbox-enabled executor needs a coordinated worker")
        self.store = store
        self.bus = bus
        self.executor = executor
        self.coordinator = coordinator
        self.owner = owner
        self.workers = workers
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock  # host time, only for event retention
        self.monotonic = monotonic
        self.event_retention_s = event_retention_s
        self.maintenance_s = maintenance_s
        self._tasks: list[asyncio.Task] = []
        self._closing = False
        self._stop = asyncio.Event()
        self._inflight: dict[str, object] = {}
        self._pruned_before: float | None = None  # the cutoff of the last successful prune

    async def start(self) -> None:
        self._closing = False
        self._stop = asyncio.Event()
        self._tasks = [asyncio.create_task(self._work(), name=f"keel-worker-{i}") for i in range(self.workers)]
        self._tasks.append(asyncio.create_task(self._sweep(), name="keel-sweeper"))
        self._tasks.append(asyncio.create_task(self._backfill(), name="keel-history-backfill"))
        if self.event_retention_s is not None:
            self._tasks.append(asyncio.create_task(self._maintain(), name="keel-maintenance"))

    async def close(self, drain_seconds: float = 4.0) -> None:
        """Stop claiming, give runs in flight `drain_seconds` to finish, then fail what is left as worker_lost.

        Not `cancelled`: that is what a user's stop looks like. The finish goes through finish_run, so a run
        that completes at the same moment still ends exactly once. Cancelled workers still wait for their
        executor's cleanup, renewing usage meanwhile; work that cannot be cleaned up stays recorded for recovery.
        """
        self._closing = True
        self._stop.set()
        background = [t for t in self._tasks if t.get_name() in _BACKGROUND]
        workers = [t for t in self._tasks if t not in background]
        for task in background:
            task.cancel()
        if workers:
            await asyncio.wait(workers, timeout=drain_seconds)
        for run in list(self._inflight.values()):
            try:
                transition = await self.store.finish_run(run.id, "failed", error_code="worker_lost",
                                                         error=SHUTDOWN_MESSAGE)
                if transition is not None and transition.event is not None:
                    await self.bus.notify(run.id)
            except Exception:
                log.exception("could not fail %s at shutdown", run.id)
        for task in workers:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._inflight.clear()

    async def sweep_once(self) -> int:
        """Fail one bounded batch of expired runs and notify their readers, then ask the coordinator to recover
        their sandbox usage. Termination commits first, so a slow sandbox cannot delay terminal visibility; a
        terminal run is no proof its commands stopped, so nothing is signalled here."""
        transitions = await self.store.expire_leases()
        for transition in transitions:
            if transition.event is not None:
                try:
                    await self.bus.notify(transition.run.id)
                except Exception:
                    log.warning("could not notify about %s", transition.run.id, exc_info=True)
        if self.coordinator is not None:
            for transition in transitions:
                try:
                    await self.coordinator.recover_run(transition.run.id)
                except Exception:
                    log.warning("could not recover the sandbox usage of %s", transition.run.id, exc_info=True)
        return len(transitions)

    async def _sweep(self) -> None:
        while not self._closing:
            try:
                await self.sweep_once()
            except Exception:
                log.exception("lease sweep failed")
            await asyncio.sleep(self.lease_seconds)

    async def prune_once(self) -> int:
        """The first pass after start scans all history; later ones only runs finished since the previous cutoff,
        less one interval of overlap so nothing is skipped."""
        if self.event_retention_s is None:
            return 0
        cutoff = self.clock() - self.event_retention_s
        after = None if self._pruned_before is None else self._pruned_before - self.maintenance_s
        deleted = await self.store.prune_events(cutoff, finished_after=after)
        self._pruned_before = cutoff
        return deleted

    async def _maintain(self) -> None:
        """Hourly: delete old run events. Passes on several instances may overlap; deleting rows already gone is
        harmless."""
        while not self._closing:
            try:
                deleted = await self.prune_once()
                if deleted:
                    log.info("pruned %d run events older than the retention", deleted)
            except Exception:
                log.exception("pruning old run events failed")
            await self._idle(self.maintenance_s)

    async def backfill_once(self) -> int | None:
        return await self.store.backfill_history(BACKFILL_BATCH)

    async def _backfill(self) -> None:
        """Index history older than migration 9, a batch at a time, until none is left; then check twice a minute.
        Instances may overlap: each batch runs under the profile lock and indexing is idempotent."""
        while not self._closing:
            try:
                done = await self.backfill_once()
            except Exception:
                log.exception("history backfill failed")
                done = None
            if done is None:
                await self._idle(BACKFILL_IDLE_S)
            else:
                await asyncio.sleep(0)

    async def _idle(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except TimeoutError:
            pass

    async def _claim(self) -> RunClaim | None:
        if self.coordinator is not None:
            return await self.coordinator.claim()
        # A chat-only worker: the legacy claim, with its deadline on the database clock the sweeper compares with.
        now = await self.store.coordination_now()
        run = await self.store.claim_run(self.owner, now + self.lease_seconds)
        return None if run is None else RunClaim(run, None, LeaseStamp(now, now + self.lease_seconds))

    async def _work(self) -> None:
        backoff = self.poll_seconds
        while not self._closing:
            sent = self.monotonic()
            try:
                claim = await self._claim()
            except Exception:
                # A down database: back off instead of logging a traceback every poll.
                log.exception("claiming a run failed; retrying in %.1fs", backoff)
                await self._idle(backoff)
                backoff = min(backoff * 2, _MAX_CLAIM_BACKOFF)
                continue
            backoff = self.poll_seconds
            if claim is None:
                await self._idle(self.poll_seconds)
                continue
            run = claim.run
            self._inflight[run.id] = run
            try:
                await self._run_one(claim, sent)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("run %s crashed the worker loop", run.id)
            finally:
                self._inflight.pop(run.id, None)

    async def _run_one(self, claim: RunClaim, sent: float) -> None:
        run, use = claim.run, claim.use
        if claim.lease.expires_at - claim.lease.observed_at <= 0:
            # No positive lease interval: dispatch nothing. The sweeper and recovery own the claim.
            log.warning("run %s was claimed without a positive lease; leaving it to the sweeper", run.id)
            return
        granted = claim.lease.expires_at - claim.lease.observed_at
        deadlines = _Deadlines(run=_confirmed(sent, claim.lease),
                               usage=_confirmed(sent, claim.lease) if use is not None else None,
                               granted=granted, usage_granted=granted)
        work = self.executor.execute(run, usage=use) if use is not None else self.executor.execute(run)
        execution = asyncio.create_task(work, name=f"keel-run-{run.id}")
        lost = asyncio.Event()
        heartbeat = None
        if use is not None:
            heartbeat = asyncio.create_task(self._keep_usage(run, use, deadlines, lost, execution),
                                            name=f"keel-usage-{run.id}")
        stop_key = "stop:" + run.id
        try:
            await self._supervise(claim, execution, deadlines, lost, stop_key)
            await self._await_cleanup(execution)
        except asyncio.CancelledError:
            execution.cancel()
            await self._await_cleanup(execution)
            raise
        except Exception:
            # Never return with the execution still running.
            execution.cancel()
            await self._await_cleanup(execution)
            raise
        finally:
            # Usage renewal ends only once the executor's cleanup (including release) has returned.
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            self.bus.discard(stop_key)

    async def _await_cleanup(self, execution: asyncio.Task) -> None:
        """Wait for the executor to return, absorbing repeated cancellation; one is re-raised afterwards."""
        received = 0
        while not execution.done():
            try:
                await asyncio.shield(execution)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    received += 1  # counted even when the execution finished in the same step
                if execution.done():
                    break
                execution.cancel()
            except Exception:
                break
        if received:
            current = asyncio.current_task()
            if current is not None:
                for _ in range(received - 1):
                    current.uncancel()
            raise asyncio.CancelledError()

    async def _supervise(self, claim: RunClaim, execution: asyncio.Task, deadlines: _Deadlines,
                         lost: asyncio.Event, stop_key: str) -> None:
        """Renew the run lease while the run is active; cancel the execution on a stop, a terminal or lost run, or
        a lost usage. Returns once the execution is done or cancelled and the run lease no longer needs renewal."""
        run = claim.run
        cancelled = False
        while not execution.done():
            if lost.is_set() and not cancelled:
                execution.cancel()
                cancelled = True
            waiter = None
            if not cancelled:
                # Take the waiter first so a stop landing during the checks below is not lost.
                waiter = self.bus.waiter(stop_key)
                try:
                    current = await asyncio.wait_for(self.store.get_run_by_id(run.id), self._left(deadlines.run))
                except Exception:
                    log.warning("checking the status of %s failed", run.id, exc_info=True)
                    if self.monotonic() >= deadlines.run:
                        execution.cancel()
                        return  # the run lease has lapsed; the sweeper owns this run now
                else:
                    if current is None or current.status != "running":
                        execution.cancel()
                        cancelled = True
            waits = {execution}
            if not lost.is_set():  # an event already set would end every wait at once and spin the renewals
                waits.add(asyncio.ensure_future(lost.wait()))
            if waiter is not None and not cancelled:
                waits.add(asyncio.ensure_future(waiter.wait()))
            try:
                done, _ = await asyncio.wait(waits, timeout=min(deadlines.granted / 3, self._left(deadlines.run)),
                                             return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiting in waits:
                    if waiting is not execution:
                        waiting.cancel()
            if execution in done:
                return
            sent = self.monotonic()
            try:
                stamp = await asyncio.wait_for(self.store.renew_claim(claim, deadlines.granted),
                                               self._left(deadlines.run))
            except Exception:
                log.warning("renewing the lease of %s failed", run.id, exc_info=True)
                if self.monotonic() >= deadlines.run:
                    execution.cancel()
                    return  # the run lease has lapsed; the sweeper owns this run now
                continue
            if stamp is None or stamp.expires_at <= stamp.observed_at:
                # Terminal, swept or owned by someone else: stop renewing the run lease. A terminal run's cleanup
                # is shielded inside the executor, so cancelling it never interrupts the release.
                execution.cancel()
                return
            deadlines.run = _confirmed(sent, stamp)
            deadlines.granted = stamp.expires_at - stamp.observed_at

    def _left(self, deadline: float) -> float:
        return max(0.0, deadline - self.monotonic())

    async def _keep_usage(self, run, use, deadlines: _Deadlines, lost: asyncio.Event,
                          execution: asyncio.Task) -> None:
        """Renew the usage every third of the lease until cancelled after executor cleanup. A definite loss, or
        reaching the last confirmed deadline without a renewal, cancels the execution: no new exec can be admitted
        anyway, since every admission rechecks the lease in the database."""
        while True:
            await asyncio.sleep(min(deadlines.usage_granted / 3, self._left(deadlines.usage)))
            if self.monotonic() >= deadlines.usage:
                log.warning("run %s: its sandbox usage lease could not be confirmed in time", run.id)
                break
            sent = self.monotonic()
            try:
                stamp = await asyncio.wait_for(self.coordinator.renew_usage(use), self._left(deadlines.usage))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("renewing the sandbox usage of %s failed", run.id, exc_info=True)
                continue
            if stamp is None or stamp.expires_at <= stamp.observed_at:
                log.warning("run %s: its sandbox usage lease was lost", run.id)
                break
            deadlines.usage = _confirmed(sent, stamp)
            deadlines.usage_granted = stamp.expires_at - stamp.observed_at
        if not execution.done():
            note = getattr(self.executor, "note_lease_lost", None)
            if note is not None:
                note(run.id)
            execution.cancel()
        lost.set()
