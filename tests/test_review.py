"""Durable memory review end to end: a completed run queues its range, a worker reviews it with a staged memory and
commits once; failures retry or stop; nothing is written to the session (cross-session memory design §4)."""

import asyncio
import json

from keel.background import Background
from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
from keel.providers.base import ModelError, ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.review import MemoryReviewer, ReviewSettings
from keel.runner import RunExecutor

OK = [TextDelta("ok"), Completed(Usage(1, 1, 0, 0))]
TEA = json.dumps({"action": "add", "target": "user", "content": "The user prefers tea"})
SAVE = [ToolCallReady(ToolCall("c1", "memory", TEA)), Completed(Usage(10, 2, 0, 0))]
DONE = [TextDelta("Nothing to save."), Completed(Usage(12, 1, 0, 0))]
SETTINGS = ReviewSettings(turns=1)


async def no_sleep(seconds):
    pass


async def setup(store, clock, provider, *, memory=True, text="I prefer tea"):
    store.coordination_clock = clock
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=memory)
    registry = ProviderRegistry()
    registry.register(provider)
    executor = RunExecutor(store, InMemoryEventBus(), registry, sleep=no_sleep, background=Background())
    await store.ensure_session("p1", "s1")
    await store.set_title("p1", "s1", "Kept", "user")  # no background title call
    await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort="low",
                           input_parts=[{"type": "text", "text": text}], instructions=None)
    run = await store.claim_run("w1", 9_999_999)
    await executor.execute(run)
    return MemoryReviewer(store, registry, SETTINGS, sleep=no_sleep), run


def review_requests(provider):
    return [request for request in provider.requests if request.system.startswith("You maintain")]


async def test_a_completed_run_is_reviewed_once_by_the_worker_without_touching_the_session(store, clock):
    provider = FakeProvider(script=[OK, SAVE, DONE])
    reviewer, run = await setup(store, clock, provider)
    events = await store.events_after(run.id, 0)
    assert await reviewer.run_once("w1")
    [item] = await store.memory_items("p1")
    assert (item.text, item.source_kind, item.session_id) == ("The user prefers tea", "review", "s1")
    [job] = await store.list_reviews("p1")
    assert job.status == "done" and job.usage[0]["requests"] == 2
    assert job.usage[0]["usage"]["input_tokens"] == 22
    assert [stored.message.role for stored in await store.messages("s1")] == ["user", "assistant"]
    assert await store.events_after(run.id, 0) == events
    [first, second] = review_requests(provider)
    assert [tool["name"] for tool in first.tools] == ["memory"]
    assert first.max_output_tokens == 2000 and first.cache_scope == "" and first.reasoning_effort == "low"
    assert "<message id=" in first.messages[0].text and "I prefer tea" in first.messages[0].text
    assert await reviewer.run_once("w1") is False


async def test_nothing_is_reviewed_without_memory(store, clock):
    provider = FakeProvider(script=[OK])
    reviewer, _ = await setup(store, clock, provider, memory=False)
    assert await reviewer.run_once("w1") is False
    assert review_requests(provider) == []


async def test_an_unfinished_proposal_commits_nothing_and_retries(store, clock):
    loop = [ToolCallReady(ToolCall("c", "memory", TEA)), Completed(Usage(1, 1, 0, 0))]
    provider = FakeProvider(script=[OK, loop, loop, loop, loop])
    reviewer, _ = await setup(store, clock, provider)
    assert await reviewer.run_once("w1")
    assert await store.memory_items("p1") == []
    [job] = await store.list_reviews("p1")
    assert (job.status, job.error_code) == ("pending", "iteration_limit")


async def test_an_unknown_tool_name_is_refused_inside_the_review(store, clock):
    sneaky = [ToolCallReady(ToolCall("c", "terminal", '{"command": "id"}')), Completed(Usage(1, 1, 0, 0))]
    provider = FakeProvider(script=[OK, sneaky, DONE])
    reviewer, _ = await setup(store, clock, provider)
    assert await reviewer.run_once("w1")
    [_, second] = review_requests(provider)
    assert "Unknown tool" in second.messages[-1].text
    assert (await store.list_reviews("p1"))[0].status == "done"


async def test_a_model_removed_from_the_profile_fails_without_retry(store, clock):
    provider = FakeProvider(script=[OK])
    reviewer, _ = await setup(store, clock, provider)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "other"}], sandbox={}, soul="",
                               skills=[], memory=True)
    assert await reviewer.run_once("w1")
    [job] = await store.list_reviews("p1")
    assert (job.status, job.error_code) == ("failed", "model_unavailable")


async def test_a_provider_error_is_recorded_and_retried(store, clock):
    provider = FakeProvider(script=[OK] + [[ModelError("server_error", "down")]] * 3)  # three retried calls
    reviewer, _ = await setup(store, clock, provider)
    assert await reviewer.run_once("w1")
    [job] = await store.list_reviews("p1")
    assert (job.status, job.error_code) == ("pending", "model_server_error") and job.attempts == 1


async def test_a_deadline_ends_the_attempt(store, clock, monkeypatch):
    import keel.review as review

    monkeypatch.setattr(review, "DEADLINE_S", 0.05)
    provider = FakeProvider(script=[OK, ["hang"]])
    reviewer, _ = await setup(store, clock, provider)
    assert await reviewer.run_once("w1")
    [job] = await store.list_reviews("p1")
    assert (job.status, job.error_code) == ("pending", "deadline")


async def test_a_survivor_retries_a_job_whose_worker_died(store, clock):
    provider = FakeProvider(script=[OK, ["hang"], SAVE, DONE])
    reviewer, _ = await setup(store, clock, provider)
    task = asyncio.create_task(reviewer.run_once("w1"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if review_requests(provider):
            break
    task.cancel()  # shutdown mid-review: the worker never commits
    await asyncio.gather(task, return_exceptions=True)
    [job] = await store.list_reviews("p1")
    assert job.status == "pending" and job.attempts == 0  # shutdown released it
    other = MemoryReviewer(store, reviewer.providers, SETTINGS, sleep=no_sleep)
    assert await other.run_once("w2")
    assert [item.text for item in await store.memory_items("p1")] == ["The user prefers tea"]


async def test_a_stale_job_from_a_recreated_profile_writes_nothing(store, clock):
    provider = FakeProvider(script=[OK, SAVE, DONE])
    reviewer, _ = await setup(store, clock, provider)
    clock.now += 60
    claimed = await store.claim_review("w1", 30.0, turns=1, idle_seconds=60.0, max_age_seconds=600.0)
    record = await store.begin_profile_deletion("p1", 1, "op-1")
    record = await store.advance_profile_deletion(record, "draining")
    await store.purge_profile_data(record)
    await store.complete_profile_deletion("p1", 1, "op-1")
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True, tenant_id=1)
    assert await store.review_owner_gone(claimed) == "profile_gone"
    assert await store.commit_review(claimed, {}, end=(0, 0), done=True, evidence=None, usage=None) == "lost"
    assert await store.memory_items("p1") == []
