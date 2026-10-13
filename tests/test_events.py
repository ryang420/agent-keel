import asyncio
import os

import asyncpg
import pytest

from keel.events import InMemoryEventBus, PostgresEventBus


async def test_notify_wakes_a_waiter_taken_before_it():
    bus = InMemoryEventBus()
    waiter = bus.waiter("run_1")
    await bus.notify("run_1")
    await asyncio.wait_for(waiter.wait(), 1)


async def test_a_waiter_on_another_key_stays_asleep():
    bus = InMemoryEventBus()
    waiter = bus.waiter("run_1")
    await bus.notify("run_2")
    assert not waiter.is_set()


async def test_after_a_notify_the_next_waiter_is_fresh():
    bus = InMemoryEventBus()
    first = bus.waiter("k")
    await bus.notify("k")
    second = bus.waiter("k")
    assert first.is_set() and not second.is_set()


async def test_discard_forgets_a_key():
    bus = InMemoryEventBus()
    waiter = bus.waiter("k")
    bus.discard("k")
    await bus.notify("k")
    assert not waiter.is_set()


async def test_a_stalled_notify_times_out_and_is_best_effort():
    class Stalled:
        terminated = False

        def is_closed(self):
            return False

        async def execute(self, *args):
            await asyncio.sleep(3600)

        def terminate(self):
            self.terminated = True

    bus = PostgresEventBus("postgresql://unused")
    bus.NOTIFY_TIMEOUT = 0.05
    stalled = bus._send = Stalled()
    waiter = bus.waiter("run_1")
    await asyncio.wait_for(bus.notify("run_1"), 2)  # never raises, never blocks past the timeout
    assert stalled.terminated and bus._send is None
    assert waiter.is_set() and not bus._send_lock.locked()


@pytest.mark.skipif(not os.environ.get("KEEL_TEST_POSTGRES_DSN"), reason="needs KEEL_TEST_POSTGRES_DSN")
async def test_postgres_notify_reaches_another_instance():
    dsn = os.environ["KEEL_TEST_POSTGRES_DSN"]
    listener, sender = PostgresEventBus(dsn), PostgresEventBus(dsn)
    await listener.start()
    await sender.start()
    try:
        waiter = listener.waiter("run_x")
        await sender.notify("run_x")
        await asyncio.wait_for(waiter.wait(), 5)
    finally:
        await listener.close()
        await sender.close()


async def test_close_wakes_a_waiter_taken_before_it():
    bus = InMemoryEventBus()
    waiter = bus.waiter("k")
    await bus.close()
    assert waiter.is_set()


async def test_notify_without_a_database_does_not_raise_and_wakes_locally():
    bus = PostgresEventBus("postgresql://x:" + "y@127.0.0.1:1/z")
    waiter = bus.waiter("k")
    await bus.notify("k")
    assert waiter.is_set()
    await bus.close()


@pytest.mark.skipif(not os.environ.get("KEEL_TEST_POSTGRES_DSN"), reason="needs KEEL_TEST_POSTGRES_DSN")
async def test_notify_reconnects_after_the_send_connection_is_closed():
    dsn = os.environ["KEEL_TEST_POSTGRES_DSN"]
    listener, sender = PostgresEventBus(dsn), PostgresEventBus(dsn)
    await listener.start()
    await sender.start()
    try:
        await sender._send.close()
        waiter = listener.waiter("run_y")
        await sender.notify("run_y")
        await asyncio.wait_for(waiter.wait(), 5)
    finally:
        await listener.close()
        await sender.close()


class FakeListen:
    def __init__(self):
        self.lost = None

    def add_termination_listener(self, callback):
        self.lost = callback

    def is_closed(self):
        return False

    def terminate(self):
        pass

    async def close(self):
        pass


async def test_a_lost_listener_reconnects_with_backoff_and_wakes_every_waiter():
    attempts = []

    class Bus(PostgresEventBus):
        RECONNECT_MIN = 0.01

        async def _connect_listener(self):
            attempts.append(1)
            if len(attempts) == 2:
                raise OSError("the database is restarting")
            conn = FakeListen()
            conn.add_termination_listener(self._on_listen_lost)
            return conn

    bus = Bus("postgresql://unused")
    first = bus._listen = await bus._connect_listener()
    waiter = bus.waiter("run_1")
    first.lost(first)
    for _ in range(200):
        if waiter.is_set():
            break
        await asyncio.sleep(0.01)
    assert waiter.is_set()
    assert len(attempts) == 3 and bus._listen is not first and bus._listen is not None
    await bus.close()


async def test_a_listener_lost_after_close_is_not_reconnected():
    bus = PostgresEventBus("postgresql://unused")
    conn = bus._listen = FakeListen()
    await bus.close()
    bus._on_listen_lost(conn)
    assert bus._reconnect is None


@pytest.mark.skipif(not os.environ.get("KEEL_TEST_POSTGRES_DSN"), reason="needs KEEL_TEST_POSTGRES_DSN")
async def test_the_listener_reconnects_after_its_backend_is_terminated():
    dsn = os.environ["KEEL_TEST_POSTGRES_DSN"]
    listener, sender = PostgresEventBus(dsn), PostgresEventBus(dsn)
    listener.RECONNECT_MIN = 0.05
    await listener.start()
    await sender.start()
    try:
        old = listener._listen
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute("SELECT pg_terminate_backend($1)", old.get_server_pid())
        finally:
            await admin.close()
        for _ in range(200):
            if listener._listen is not None and listener._listen is not old:
                break
            await asyncio.sleep(0.05)
        assert listener._listen is not None and listener._listen is not old
        waiter = listener.waiter("run_z")
        await sender.notify("run_z")
        await asyncio.wait_for(waiter.wait(), 5)
    finally:
        await listener.close()
        await sender.close()
