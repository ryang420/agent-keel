"""Workers claim queued runs, keep their lease alive, honour stop requests, and sweep dead leases."""

from __future__ import annotations

import asyncio
import logging
import time

from keel.store.base import WORKER_LOST_MESSAGE

log = logging.getLogger(__name__)
_MAX_CLAIM_BACKOFF = 10.0
SHUTDOWN_MESSAGE = "The service was shutting down."


class WorkerPool:
    def __init__(self, store, bus, executor, *, owner: str, workers: int = 4, lease_seconds: float = 30.0,
                 poll_seconds: float = 0.5, clock=time.time) -> None:
        self.store = store
        self.bus = bus
        self.executor = executor
        self.owner = owner
        self.workers = workers
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock
        self._tasks: list[asyncio.Task] = []
        self._closing = False
        self._stop = asyncio.Event()
        self._inflight: dict[str, object] = {}

    async def start(self) -> None:
        self._closing = False
        self._stop = asyncio.Event()
        self._tasks = [asyncio.create_task(self._work(), name=f"keel-worker-{i}") for i in range(self.workers)]
        self._tasks.append(asyncio.create_task(self._sweep(), name="keel-sweeper"))

    async def close(self, drain_seconds: float = 4.0) -> None:
        """Stop claiming, give runs in flight `drain_seconds` to finish, then fail what is left as worker_lost.

        Not `cancelled`: that is what a user's stop looks like. The finish goes through finish_run, so a run
        that completes at the same moment still ends exactly once.
        """
        self._closing = True
        self._stop.set()
        sweeper = [t for t in self._tasks if t.get_name() == "keel-sweeper"]
        workers = [t for t in self._tasks if t not in sweeper]
        for task in sweeper:
            task.cancel()
        if workers:
            await asyncio.wait(workers, timeout=drain_seconds)
        for run in list(self._inflight.values()):
            try:
                if await self.store.finish_run(run.id, "failed", error_code="worker_lost", error=SHUTDOWN_MESSAGE):
                    await self.executor.emit(run.id, "run.failed",
                                             {"error_code": "worker_lost", "message": SHUTDOWN_MESSAGE})
            except Exception:
                log.exception("could not fail %s at shutdown", run.id)
        for task in workers:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._inflight.clear()

    async def sweep_once(self) -> int:
        """Fail every run whose lease expired, then kill the commands their dead workers left running. The events
        go first so a slow sandbox cannot delay them."""
        expired = await self.store.expire_leases()
        for run in expired:
            try:
                await self.executor.emit(run.id, "run.failed",
                                         {"error_code": "worker_lost", "message": WORKER_LOST_MESSAGE})
            except Exception:
                log.exception("could not emit run.failed for %s", run.id)
        for run in expired:
            await self.executor.kill_orphaned_command(run)
        return len(expired)

    async def _sweep(self) -> None:
        while not self._closing:
            try:
                await self.sweep_once()
            except Exception:
                log.exception("lease sweep failed")
            await asyncio.sleep(self.lease_seconds)

    async def _idle(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except TimeoutError:
            pass

    async def _work(self) -> None:
        backoff = self.poll_seconds
        while not self._closing:
            try:
                run = await self.store.claim_run(self.owner, self.clock() + self.lease_seconds)
            except Exception:
                # A down database: back off instead of logging a traceback every poll.
                log.exception("claiming a run failed; retrying in %.1fs", backoff)
                await self._idle(backoff)
                backoff = min(backoff * 2, _MAX_CLAIM_BACKOFF)
                continue
            backoff = self.poll_seconds
            if run is None:
                await self._idle(self.poll_seconds)
                continue
            self._inflight[run.id] = run
            try:
                await self._run_one(run)
            except Exception:
                log.exception("run %s crashed the worker loop", run.id)
            finally:
                self._inflight.pop(run.id, None)

    async def _run_one(self, run) -> None:
        stop_key = "stop:" + run.id
        execution = asyncio.create_task(self.executor.execute(run), name=f"keel-run-{run.id}")
        deadline = self.clock() + self.lease_seconds
        try:
            while not execution.done():
                # Take the waiter first so a stop landing during the checks below is not lost.
                waiter = self.bus.waiter(stop_key)
                try:
                    current = await self.store.get_run_by_id(run.id)
                except Exception:
                    log.warning("checking the status of %s failed", run.id, exc_info=True)
                    if self.clock() > deadline:
                        execution.cancel()
                        break
                else:
                    if current is None or current.status != "running":
                        execution.cancel()
                        break
                stop = asyncio.ensure_future(waiter.wait())
                try:
                    done, _ = await asyncio.wait({execution, stop}, timeout=self.lease_seconds / 3,
                                                 return_when=asyncio.FIRST_COMPLETED)
                finally:
                    stop.cancel()
                if execution in done:
                    break
                try:
                    status = await self.store.renew_lease(run.id, self.owner, self.clock() + self.lease_seconds)
                except Exception:
                    log.warning("renewing the lease of %s failed", run.id, exc_info=True)
                    if self.clock() > deadline:
                        # The lease has lapsed; the sweeper owns this run now.
                        execution.cancel()
                        break
                    continue
                if status != "running":
                    # "stopping", or None when the lease was lost to the sweeper.
                    execution.cancel()
                    break
                deadline = self.clock() + self.lease_seconds
            await asyncio.gather(execution, return_exceptions=True)
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        except Exception:
            # Never return with the execution still running.
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        finally:
            self.bus.discard(stop_key)
