"""Best-effort tasks that must not belong to a run: a title, a memory review. They are cancelled only at shutdown."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)
_ALL_INCARNATIONS = object()


class Background:
    """Per-profile best-effort tasks, tracked so a profile reset can drain them."""

    def __init__(self, limit: int = 4, close_timeout_s: float = 5.0) -> None:
        self.limit = limit
        self.close_timeout_s = close_timeout_s
        self._tasks: dict[asyncio.Task, tuple[str | None, str | None]] = {}
        self._blocked: dict[str, tuple[str | None, str | None]] = {}

    def block_profile(self, profile_id: str, operation_id: str | None = None,
                      profile_incarnation: str | None = None) -> None:
        """Refuse new background work for a profile whose deletion has been claimed."""
        self._blocked[profile_id] = (operation_id, profile_incarnation)

    def unblock_profile(self, profile_id: str, operation_id: str | None = None,
                        profile_incarnation: str | None = None) -> None:
        current = self._blocked.get(profile_id)
        if (current is not None and current[0] == operation_id
                and (profile_incarnation is None or current[1] == profile_incarnation)):
            del self._blocked[profile_id]

    def spawn(self, coro, name: str, *, profile_id: str | None = None,
              profile_incarnation: str | None = None) -> bool:
        """Start `coro`. At the limit, or for a blocked profile, it is dropped and this returns False."""
        if profile_id is not None and profile_id in self._blocked:
            blocked_incarnation = self._blocked[profile_id][1]
            if (profile_incarnation is None or blocked_incarnation == profile_incarnation):
                coro.close()
                log.info("background task %s dropped: profile deletion is pending", name)
                return False
            # A different captured incarnation may belong to a stale callback or to a profile recreated after a
            # different instance completed deletion. Let durable write checks decide; never let that callback
            # erase this instance's newer operation block.
        if len([task for task in self._tasks if not task.done()]) >= self.limit:
            coro.close()
            log.warning("background task %s dropped: %d already running", name, self.limit)
            return False
        task = asyncio.create_task(self._run(coro), name=name)
        self._tasks[task] = (profile_id, profile_incarnation)
        task.add_done_callback(self._tasks.pop)
        return True

    def profile_tasks(self, profile_id: str, *, profile_incarnation=_ALL_INCARNATIONS) -> list[asyncio.Task]:
        return [task for task, (owner, incarnation) in self._tasks.items()
                if owner == profile_id and not task.done()
                and (profile_incarnation is _ALL_INCARNATIONS or incarnation is None
                     or incarnation == profile_incarnation)]

    async def drain_profile(self, profile_id: str, *, timeout: float,
                            profile_incarnation=_ALL_INCARNATIONS) -> bool:
        """Cancel and await this profile's background work; False if any task outlives `timeout`."""
        tasks = self.profile_tasks(profile_id, profile_incarnation=profile_incarnation)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
            if pending:
                log.warning("profile %s still has %d background tasks after drain", profile_id, len(pending))
                return False
        return not self.profile_tasks(profile_id, profile_incarnation=profile_incarnation)

    async def _run(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("background task %s failed", asyncio.current_task().get_name(), exc_info=True)

    async def close(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=self.close_timeout_s)
        if pending:
            log.warning("%d background tasks still running after %.0fs", len(pending), self.close_timeout_s)
