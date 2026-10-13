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
    def __init__(self, closed=False):
        self.lost = None
        self.closed = closed

    async def add_listener(self, channel, callback):
        pass

    def add_termination_listener(self, callback):
        self.lost = callback

    def is_closed(self):
        return self.closed

    def terminate(self):
        self.closed = True

    async def close(self):
        self.closed = True


def fake_database(monkeypatch, outcomes):
    """Each connect takes the next outcome: an exception to raise, or a FakeListen to return."""
    attempts, delays = [], []
    real_sleep = asyncio.sleep

    async def connect(dsn, timeout=None):
        attempts.append(dsn)
        outcome = outcomes.pop(0) if outcomes else FakeListen()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return attempts, delays, real_sleep


async def wait_until(condition, sleep):
    for _ in range(200):
        if condition():
            return
        await sleep(0.01)


async def test_a_lost_listener_reconnects_with_backoff_and_wakes_every_waiter(monkeypatch):
    down = OSError("the database is restarting")
    attempts, delays, real_sleep = fake_database(monkeypatch, [down, down, down, down])
    bus = PostgresEventBus("postgresql://unused")
    bus.RECONNECT_MIN, bus.RECONNECT_MAX = 0.01, 0.04
    first = bus._listen = FakeListen()
    first.add_termination_listener(bus._on_listen_lost)
    before = bus.waiter("run_1")
    first.lost(first)
    during = bus.waiter("run_2")
    assert bus._listen is None
    await wait_until(lambda: before.is_set() and during.is_set(), real_sleep)
    assert before.is_set() and during.is_set()
    assert len(attempts) == 5 and delays == [0.01, 0.02, 0.04, 0.04]
    assert bus._listen is not None and bus._listen is not first and not bus._listen.is_closed()
    await bus.close()


async def test_a_listener_that_closes_while_starting_is_retried(monkeypatch):
    dead = FakeListen(closed=True)
    attempts, delays, real_sleep = fake_database(monkeypatch, [dead])
    bus = PostgresEventBus("postgresql://unused")
    first = bus._listen = FakeListen()
    first.add_termination_listener(bus._on_listen_lost)
    first.lost(first)
    await wait_until(lambda: bus._listen is not None, real_sleep)
    assert len(attempts) == 2 and delays == [bus.RECONNECT_MIN]
    assert bus._listen is not dead and not bus._listen.is_closed()
    await bus.close()


async def test_a_listener_lost_after_close_is_not_reconnected():
    bus = PostgresEventBus("postgresql://unused")
    conn = bus._listen = FakeListen()
    await bus.close()
    bus._listen = conn
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
