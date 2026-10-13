"""Best-effort tasks that must not belong to a run: a title, a memory review. They are cancelled only at shutdown."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class Background:
    def __init__(self, limit: int = 4, close_timeout_s: float = 5.0) -> None:
        self.limit = limit
        self.close_timeout_s = close_timeout_s
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro, name: str) -> bool:
        """Start `coro`. At the limit the task is dropped and this returns False; the run is unaffected."""
        if len([task for task in self._tasks if not task.done()]) >= self.limit:
            coro.close()
            log.warning("background task %s dropped: %d already running", name, self.limit)
            return False
        task = asyncio.create_task(self._run(coro), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

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
