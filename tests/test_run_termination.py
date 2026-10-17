"""Postgres-only races over run termination: two independent stores, released together by a barrier.

Every race must end with exactly one terminal event, and the event must agree with the stored run. The same
scenarios run against SQLite in `tests/test_store.py`; only Postgres can exercise row-lock contention.
"""

import asyncio
import os

import pytest

from keel.store import open_store

DSN = os.environ.get("KEEL_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(DSN is None, reason="needs KEEL_TEST_POSTGRES_DSN")
REPEATS = 10


@pytest.fixture
async def stores():
    import asyncpg

    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP TABLE IF EXISTS schema_version, memories, run_events, runs, messages, sessions, "
                       "profiles, tenants, blobs")
    await conn.close()
    first = await open_store(DSN)
    second = await open_store(DSN)
    try:
        yield first, second
    finally:
        await second.close()
        await first.close()


async def make_run(store, session: str, *, claimed: bool = True):
    await store.ensure_session("p1", session)
    run = await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                 instructions=None)
    if claimed:
        assert (await store.claim_run("w0", run.created_at + 300)).id == run.id
    return run


async def race(*calls):
    barrier = asyncio.Barrier(len(calls))

    async def run(call):
        await barrier.wait()
        return await call()

    return await asyncio.gather(*(run(call) for call in calls))


def terminal_types(events):
    return [event.type for event in events if event.type in ("run.completed", "run.failed", "run.cancelled")]


async def test_concurrent_finishes_commit_one_outcome(stores):
    first, second = stores
    for index in range(REPEATS):
        run = await make_run(first, f"finish-{index}")
        results = await race(lambda: first.finish_run(run.id, "completed", output="done"),
                             lambda: second.finish_run(run.id, "failed", error_code="internal_error",
                                                       error="boom"))
        winners = [result for result in results if result.event is not None]
        assert len(winners) == 1
        events = await first.events_after(run.id, 0)
        assert [event.type for event in events] == [winners[0].event.type]
        saved = await first.get_run("p1", run.id)
        assert saved.status == winners[0].event.type.removeprefix("run.")


async def test_two_queued_stops_commit_one_cancellation(stores):
    first, second = stores
    for index in range(REPEATS):
        run = await make_run(first, f"stop-{index}", claimed=False)
        results = await race(lambda: first.request_stop("p1", run.id), lambda: second.request_stop("p1", run.id))
        winners = [result for result in results if result.event is not None]
        assert len(winners) == 1 and winners[0].event.data == {}
        events = await first.events_after(run.id, 0)
        assert [(event.type, event.seq) for event in events] == [("run.cancelled", 1)]
        assert all(result.run.status == "cancelled" for result in results)


async def test_a_stop_racing_a_claim_leaves_a_consistent_state(stores):
    first, second = stores
    for index in range(REPEATS):
        run = await make_run(first, f"claim-{index}", claimed=False)
        stopped, claimed = await race(lambda: first.request_stop("p1", run.id),
                                      lambda: second.claim_run("w1", 1e12))
        saved = await first.get_run("p1", run.id)
        events = await first.events_after(run.id, 0)
        if saved.status == "cancelled":
            assert claimed is None and stopped.event is not None
            assert [event.type for event in events] == ["run.cancelled"]
        else:
            assert (saved.status, claimed.id) == ("stopping", run.id)
            assert stopped.event is None and events == []


async def test_completion_racing_a_stop_always_wins_or_is_already_finished(stores):
    first, second = stores
    for index in range(REPEATS):
        run = await make_run(first, f"stop-finish-{index}")
        finished, stopped = await race(lambda: first.finish_run(run.id, "completed", output="done"),
                                       lambda: second.request_stop("p1", run.id))
        saved = await first.get_run("p1", run.id)
        events = await first.events_after(run.id, 0)
        assert saved.status == "completed" and stopped.event is None
        assert terminal_types(events) == [finished.event.type] == ["run.completed"]


async def test_completion_racing_expiry_commits_one_outcome(stores):
    first, second = stores
    for index in range(REPEATS):
        run = await make_run(first, f"expire-{index}")
        await first.db.execute("UPDATE runs SET lease_expires_at = 0 WHERE id = ?", run.id)
        finished, expired = await race(lambda: first.finish_run(run.id, "completed", output="done"),
                                       lambda: second.expire_leases())
        events = await first.events_after(run.id, 0)
        assert len(terminal_types(events)) == 1
        saved = await first.get_run("p1", run.id)
        assert saved.status == terminal_types(events)[0].removeprefix("run.")
        assert sum(result.event is not None for result in [finished, *expired]) == 1


async def test_a_renewal_racing_expiry_has_one_consistent_outcome(stores):
    first, second = stores
    for index in range(REPEATS):
        run = await make_run(first, f"renew-{index}")
        await first.db.execute("UPDATE runs SET lease_expires_at = 0 WHERE id = ?", run.id)
        renewed, expired = await race(lambda: first.renew_lease(run.id, "w0", 1e12),
                                      lambda: second.expire_leases())
        saved = await first.get_run("p1", run.id)
        events = await first.events_after(run.id, 0)
        if saved.status == "running":
            assert renewed == "running" and expired == [] and events == []
        else:
            assert (saved.status, renewed, expired[0].run.status) == ("failed", None, "failed")
            assert [event.type for event in events] == ["run.failed"]


async def test_an_ordinary_event_racing_the_finish_never_follows_the_terminal_one(stores):
    first, second = stores
    for index in range(REPEATS):
        run = await make_run(first, f"append-{index}")
        appended, finished = await race(
            lambda: first.append_event(run.id, "message.delta", {"text": "x"}),
            lambda: second.finish_run(run.id, "completed", output="done"))
        types = [event.type for event in await first.events_after(run.id, 0)]
        assert types[-1] == "run.completed"
        assert types == (["run.completed"] if appended is None else ["message.delta", "run.completed"])
        assert finished.event is not None
