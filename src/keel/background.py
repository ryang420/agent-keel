"""Best-effort tasks that must not belong to a run: a title, a memory review. They are cancelled only at shutdown."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class Background:
    """Per-profile best-effort tasks, tracked so a profile reset can drain them."""

    def __init__(self, limit: int = 4, close_timeout_s: float = 5.0) -> None:
        self.limit = limit
        self.close_timeout_s = close_timeout_s
        self._tasks: dict[asyncio.Task, str | None] = {}
        self._blocked: set[str] = set()

    def block_profile(self, profile_id: str) -> None:
        """Refuse new background work for a profile whose deletion has been claimed."""
        self._blocked.add(profile_id)

    def unblock_profile(self, profile_id: str) -> None:
        self._blocked.discard(profile_id)

    def spawn(self, coro, name: str, *, profile_id: str | None = None) -> bool:
        """Start `coro`. At the limit, or for a blocked profile, it is dropped and this returns False."""
        if profile_id is not None and profile_id in self._blocked:
            coro.close()
            log.info("background task %s dropped: profile deletion is pending", name)
            return False
        if len([task for task in self._tasks if not task.done()]) >= self.limit:
            coro.close()
            log.warning("background task %s dropped: %d already running", name, self.limit)
            return False
        task = asyncio.create_task(self._run(coro), name=name)
        self._tasks[task] = profile_id
        task.add_done_callback(self._tasks.pop)
        return True

    def profile_tasks(self, profile_id: str) -> list[asyncio.Task]:
        return [task for task, owner in self._tasks.items()
                if owner == profile_id and not task.done()]

    async def drain_profile(self, profile_id: str, *, timeout: float) -> bool:
        """Cancel and await this profile's background work; False if any task outlives `timeout`."""
        tasks = self.profile_tasks(profile_id)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout))
            if pending:
                log.warning("profile %s still has %d background tasks after drain", profile_id, len(pending))
                return False
        return not self.profile_tasks(profile_id)

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
