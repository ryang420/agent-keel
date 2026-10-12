"""Wake-ups between the parts of the service: "run X has new events", "stop run X".

The bus carries no payload; readers take a waiter, read the store, and wait on the waiter if
there was nothing new. Taking the waiter before reading closes the race with a notify.

A wake-up is advisory: readers also re-read the store on a timeout, so a failed notify is
logged and never raised.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)


class InMemoryEventBus:
    """For a single instance and for tests."""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Event] = {}

    def waiter(self, key: str) -> asyncio.Event:
        event = self._waiters.get(key)
        if event is None:
            event = self._waiters[key] = asyncio.Event()
        return event

    def _wake(self, key: str) -> None:
        event = self._waiters.pop(key, None)
        if event is not None:
            event.set()

    def discard(self, key: str) -> None:
        self._waiters.pop(key, None)

    async def notify(self, key: str) -> None:
        self._wake(key)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        # Wake blocked readers so they can notice shutdown instead of sleeping forever.
        for event in self._waiters.values():
            event.set()
        self._waiters.clear()


class PostgresEventBus(InMemoryEventBus):
    """Spreads notifies to every instance through LISTEN/NOTIFY.

    Needs direct connections: a pooler in transaction mode (e.g. RDS Proxy) pins or drops LISTEN.
    """

    CHANNEL = "keel_events"
    CONNECT_TIMEOUT = 5
    NOTIFY_TIMEOUT = 5

    def __init__(self, dsn: str) -> None:
        super().__init__()
        self._dsn = dsn
        self._listen: asyncpg.Connection | None = None
        self._send: asyncpg.Connection | None = None
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        self._listen = await asyncpg.connect(self._dsn)
        await self._listen.add_listener(self.CHANNEL, self._on_notify)
        self._listen.add_termination_listener(self._on_listen_lost)
        self._send = await asyncpg.connect(self._dsn)

    def _on_notify(self, connection, pid, channel, payload: str) -> None:
        self._wake(payload)

    def _on_listen_lost(self, connection) -> None:
        if not self._closed:
            log.error("LISTEN connection lost; cross-instance wake-ups fall back to polling")

    async def notify(self, key: str) -> None:
        async with self._send_lock:
            if not self._closed:
                try:
                    if self._send is None or self._send.is_closed():
                        self._send = await asyncpg.connect(self._dsn, timeout=self.CONNECT_TIMEOUT)
                    # Bounded, so a stalled connection cannot hold the lock (and every run) forever.
                    await asyncio.wait_for(self._send.execute("SELECT pg_notify($1, $2)", self.CHANNEL, key),
                                           self.NOTIFY_TIMEOUT)
                except Exception:
                    log.warning("notify failed for %s; will reconnect on the next notify", key, exc_info=True)
                    broken, self._send = self._send, None
                    if broken is not None:
                        broken.terminate()
        self._wake(key)

    async def close(self) -> None:
        async with self._send_lock:
            self._closed = True
            conn, self._send = self._send, None
            if conn is not None:
                await _close_connection(conn)
        listen, self._listen = self._listen, None
        if listen is not None:
            await _close_connection(listen)
        await super().close()


async def _close_connection(conn: asyncpg.Connection) -> None:
    try:
        await conn.close()
    except Exception:
        conn.terminate()
