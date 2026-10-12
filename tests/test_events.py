import asyncio
import os

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
