"""Cross-session memory acceptance, memory rows (design §7). Scripted model: these prove the plumbing; whether a real
model chooses the right facts is the live evaluation's job (evals/memory)."""

import asyncio
import json

from keel.background import Background
from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.review import MemoryReviewer, ReviewSettings
from keel.runner import RunExecutor
from keel.store import open_store

MODELS = [{"provider": "fake", "model": "m"}]
OK = [TextDelta("ok"), Completed(Usage(1, 1, 0, 0))]
NOTHING = [TextDelta("Nothing to save."), Completed(Usage(1, 1, 0, 0))]
DRAIN = ReviewSettings(turns=1, idle_seconds=0.0, max_age_seconds=0.0)


def save(text, target="user", call_id="c1"):
    return [ToolCallReady(ToolCall(call_id, "memory", json.dumps({"action": "add", "target": target,
                                                                  "content": text}))),
            Completed(Usage(1, 1, 0, 0))]


async def no_sleep(seconds):
    pass


def parts(store, provider):
    registry = ProviderRegistry()
    registry.register(provider)
    executor = RunExecutor(store, InMemoryEventBus(), registry, sleep=no_sleep, background=Background())
    return executor, MemoryReviewer(store, registry, DRAIN, sleep=no_sleep)


async def turn(store, executor, session, text="hello"):
    await store.ensure_session("p1", session)
    await store.set_title("p1", session, f"Pinned {session}", "user")
    await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m", reasoning_effort="low",
                           input_parts=[{"type": "text", "text": text}], instructions=None)
    run = await store.claim_run("w1", 9_999_999)
    await executor.execute(run)
    return await store.get_run("p1", run.id)


async def drain(reviewer):
    for _ in range(20):
        if not await reviewer.run_once("w1"):
            return


async def memory_profile(store):
    await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="", skills=[], memory=True)


async def test_a_reviewed_preference_reaches_a_fresh_session_and_not_an_open_one(store, clock):
    """§7 row 1: a preference saved by the background review is in the snapshot of a session started after the
    commit, and not in a session whose prompt was frozen before it."""
    store.coordination_clock = clock
    await memory_profile(store)
    provider = FakeProvider(script=[OK, save("Prefers the conclusion first"), NOTHING, OK, OK])
    executor, reviewer = parts(store, provider)
    await turn(store, executor, "a", "From now on, give me the conclusion first.")
    before = (await store.get_session("p1", "a")).system_prompt
    await drain(reviewer)
    assert (await store.memory_entries("p1"))["user"] == ["Prefers the conclusion first"]
    await turn(store, executor, "b", "Explain HTTP caching.")
    assert "Prefers the conclusion first" in provider.requests[-1].system
    assert "Prefers the conclusion first" not in before
    assert (await store.get_session("p1", "a")).system_prompt == before
    await turn(store, executor, "a", "And again.")
    assert provider.requests[-1].system == before


async def test_a_correction_leaves_only_the_corrected_fact_for_a_fresh_session(store, clock):
    """§7 row 3: an explicit correction replaces the applicable fact; a fresh session sees only the corrected one."""
    store.coordination_clock = clock
    await memory_profile(store)
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "Call the user Dr. Okafor"}])
    [entry] = (await store.memory_items("p1"))
    fix = json.dumps({"action": "replace", "target": "user", "entry_id": entry.id,
                      "content": "Call the user Chidi"})
    provider = FakeProvider(script=[[ToolCallReady(ToolCall("c1", "memory", fix)), Completed(Usage(1, 1, 0, 0))],
                                    OK, NOTHING, OK])
    executor, reviewer = parts(store, provider)
    await turn(store, executor, "b", "Please call me Chidi from now on, not Dr. Okafor.")
    await drain(reviewer)
    await turn(store, executor, "c", "Hello.")
    system = provider.requests[-1].system
    assert "Call the user Chidi" in system and "Dr. Okafor" not in system
    assert (await store.memory_entries("p1"))["user"] == ["Call the user Chidi"]


async def test_a_short_session_is_reviewed_once_after_a_restart(pg_dsn, clock):
    """§7 row 4: a session with one completed turn becomes eligible after the idle minute, survives a process restart
    (a new store on the same database) and is committed exactly once."""
    first = await open_store(pg_dsn, clock=clock, coordination_clock=clock)
    try:
        await memory_profile(first)
        executor, _ = parts(first, FakeProvider(script=[OK]))
        await turn(first, executor, "s1", "I work in UTC.")
        early = MemoryReviewer(first, _registry(FakeProvider(script=[])), ReviewSettings(), sleep=no_sleep)
        assert not await early.run_once("w1")
    finally:
        await first.close()
    clock.now += 60
    second = await open_store(pg_dsn, clock=clock, coordination_clock=clock)
    try:
        provider = FakeProvider(script=[save("Works in UTC"), NOTHING])
        reviewer = MemoryReviewer(second, _registry(provider), ReviewSettings(), sleep=no_sleep)
        assert await reviewer.run_once("w2")
        assert not await reviewer.run_once("w2")
        assert (await second.memory_entries("p1"))["user"] == ["Works in UTC"]
        [job] = await second.list_reviews("p1")
        assert job.status == "done"
    finally:
        await second.close()


def _registry(provider):
    registry = ProviderRegistry()
    registry.register(provider)
    return registry


async def test_two_instances_never_run_two_reviews_for_one_profile_or_duplicate_jobs(pg_dsn, clock):
    """§7 row 11, two-instance half (the foreground-correction race is
    tests/test_review_commit.py::test_a_foreground_write_to_a_touched_target_is_a_conflict): two stores on one
    database claim at the same moment; at most one review runs per profile and each session has at most one open
    job."""
    one = await open_store(pg_dsn, clock=clock, coordination_clock=clock)
    two = await open_store(pg_dsn, clock=clock, coordination_clock=clock)
    try:
        await memory_profile(one)
        executor, _ = parts(one, FakeProvider(script=[OK, OK, OK]))
        for session in ("a", "b", "c"):
            await turn(one, executor, session)
        clock.now += 60
        rules = {"turns": 10, "idle_seconds": 60.0, "max_age_seconds": 600.0}
        claims = await asyncio.gather(*(store.claim_review(f"w{i}", 30.0, **rules)
                                        for i, store in enumerate((one, two, one, two))))
        assert len([claim for claim in claims if claim is not None]) == 1
        rows = await one.db.fetch("SELECT session_id, COUNT(*) AS n FROM memory_review_jobs "
                                  "WHERE status IN ('pending', 'running', 'failed') GROUP BY session_id")
        assert {row["session_id"]: row["n"] for row in rows} == {"a": 1, "b": 1, "c": 1}
    finally:
        await one.close()
        await two.close()


async def test_a_committed_review_is_never_applied_twice(store, clock):
    """§7 row 10, committed half (a crash before commit is covered by the fault-injection tests): once a review
    committed, a worker that dies afterwards leaves nothing to redo: no job is claimable
    and the fact is stored once."""
    store.coordination_clock = clock
    await memory_profile(store)
    provider = FakeProvider(script=[OK, save("Works in UTC"), NOTHING])
    executor, reviewer = parts(store, provider)
    await turn(store, executor, "s1", "I work in UTC.")
    await drain(reviewer)
    clock.now += 3600  # every lease long expired
    assert await store.claim_review("w9", 30.0, turns=1, idle_seconds=0.0, max_age_seconds=0.0) is None
    assert (await store.memory_entries("p1"))["user"] == ["Works in UTC"]


async def test_two_turns_keep_prompt_and_tool_bytes_and_make_no_extra_model_call(store, clock):
    """§7 row 13: with memory and history on, a two-turn conversation keeps the system prompt and tool list
    byte-identical, even after the first turn writes memory, and makes one model call per answer: no review or
    history call runs inside the turn."""
    store.coordination_clock = clock
    await memory_profile(store)
    provider = FakeProvider(script=[save("Works in UTC"), OK, OK])
    background = Background()
    registry = _registry(provider)
    executor = RunExecutor(store, InMemoryEventBus(), registry, sleep=no_sleep, background=background)
    await turn(store, executor, "s1", "I work in UTC.")
    await turn(store, executor, "s1", "Thanks.")
    await background.close()
    assert len(provider.requests) == 3
    systems = {request.system for request in provider.requests}
    tools = {json.dumps(request.tools, sort_keys=True) for request in provider.requests}
    assert len(systems) == 1 and len(tools) == 1
    assert {tool["name"] for tool in provider.requests[0].tools} == {"memory", "session_search"}
    assert "Works in UTC" not in provider.requests[-1].system
