"""A background review may save memory. It never writes the session it read."""

import asyncio
import json
import sys

import pytest

from keel.background import Background
from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
from keel.providers.base import ModelError, ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor

OK = [TextDelta("ok"), Completed(Usage(1, 1, 0, 0))]
TEA = json.dumps({"action": "add", "target": "user", "content": "The user likes tea"})
REVIEW_SAVE = [ToolCallReady(ToolCall("c1", "memory", TEA)), Completed(Usage(1, 1, 0, 0))]
REVIEW_DONE = [TextDelta("Nothing to save."), Completed(Usage(1, 1, 0, 0))]
REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n"
    "\n"
    "Memory has TWO distinct stores — pick the right one for each fact:\n"
    "- target \"user\": who the user is — persona, preferences, communication and work style, personal details "
    "they revealed, and expectations about how you should behave.\n"
    "- target \"memory\": facts about the environment you operate in — tool quirks, project conventions, config "
    "gotchas, paths and endpoints that matter.\n"
    "\n"
    "One fact goes to ONE store, never both. If nothing is worth saving, just say 'Nothing to save.' and stop."
)


async def no_sleep(seconds):
    pass


@pytest.fixture(autouse=True)
def _clear_review_claims():
    """The in-flight set is process-wide. A failed test must not leave the next one unable to review."""
    yield
    review = sys.modules.get("keel.review")
    if review is not None:
        review._running.clear()


def executor_for(store, provider, *, turns=1, background=None):
    registry = ProviderRegistry()
    registry.register(provider)
    background = background or Background()
    executor = RunExecutor(store, InMemoryEventBus(), registry, sleep=no_sleep, background=background,
                           memory_review_turns=turns)
    return executor, background


async def start(store, session="s1", text="hello"):
    await store.ensure_session("p1", session)
    await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m",
                           reasoning_effort="low", input_parts=[{"type": "text", "text": text}],
                           instructions=None)
    return await store.claim_run("w1", 9_999_999)


async def pin_title(store, session):
    """A user title skips the background rename, so the scripted provider serves the review only."""
    await store.set_title("p1", session, f"Kept {session}", "user")


async def wait_for_background(background):
    tasks = [task for task in background._tasks]
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=2)
    assert not pending


def review_requests(provider):
    return [request for request in provider.requests
            if request.messages and request.messages[-1].text.startswith("Review the conversation")]


async def test_the_tenth_completed_run_starts_a_review_that_does_not_touch_the_session(store):
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True)
    await store.ensure_session("p1", "s1")
    for _ in range(9):
        assert await store.note_memory_turn("s1", 10) is False
    assert await store.note_memory_turn("s1", 10) is True
    assert (await store.get_session("p1", "s1")).turns_since_memory == 0

    provider = FakeProvider()
    provider.script = [OK, REVIEW_SAVE, REVIEW_DONE]
    executor, background = executor_for(store, provider, turns=1)
    run = await start(store)
    await pin_title(store, "s1")
    await executor.execute(run)
    events = [event.type for event in await store.events_after(run.id, 0)]
    await wait_for_background(background)

    assert (await store.memory_entries("p1"))["user"] == ["The user likes tea"]
    assert [message.message.role for message in await store.messages("s1")] == ["user", "assistant"]
    assert (await store.get_run("p1", run.id)).status == "completed"
    assert [event.type for event in await store.events_after(run.id, 0)] == events
    review = review_requests(provider)
    assert len(review) == 1
    assert review[0].messages[-1].text == REVIEW_PROMPT
    assert [tool["name"] for tool in review[0].tools] == ["memory"]
    assert review[0].system == (await store.get_session("p1", "s1")).system_prompt
    assert review[0].cache_scope == "s1" and review[0].reasoning_effort == "low"


class _OverlappingReviews(FakeProvider):
    """Sleeps only on a review. FakeProvider's script is a queue, so it cannot tell a review from a user turn."""

    def __init__(self):
        super().__init__()
        self.reviews = 0

    async def stream(self, request):
        self.requests.append(request)
        if request.messages and request.messages[-1].text.startswith("Review the conversation"):
            self.reviews += 1
            await asyncio.sleep(0.2)
            yield TextDelta("Nothing to save.")
            yield Completed(Usage(1, 1, 0, 0))
            return
        yield TextDelta("ok")
        yield Completed(Usage(1, 1, 0, 0))


async def test_a_review_already_running_for_the_profile_is_skipped(store):
    provider = _OverlappingReviews()
    executor, background = executor_for(store, provider, turns=1)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True)
    first, second = await start(store, "s1"), await start(store, "s2")
    await asyncio.gather(executor.execute(first), executor.execute(second))
    await wait_for_background(background)
    assert provider.reviews == 1
    assert (await store.get_run("p1", first.id)).status == "completed"
    assert (await store.get_run("p1", second.id)).status == "completed"


async def test_a_failing_review_leaves_the_run_completed(store):
    provider = FakeProvider()
    provider.script = [OK, [ModelError("bad_request", "down")]]
    executor, background = executor_for(store, provider, turns=1)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True)
    run = await start(store)
    await pin_title(store, "s1")
    await executor.execute(run)
    await wait_for_background(background)
    done = await store.get_run("p1", run.id)
    assert done.status == "completed" and done.error_code is None
    assert (await store.memory_entries("p1")) == {"memory": [], "user": []}
    assert review_requests(provider)
    assert [message.message.role for message in await store.messages("s1")] == ["user", "assistant"]


async def test_a_run_that_uses_the_memory_tool_still_counts_as_one(store):
    """The tool zeros the counter during the run. The increment is after the run, so the run still counts as 1
    and a counter that was about to review does not."""
    provider = FakeProvider()
    provider.script = [[ToolCallReady(ToolCall("c1", "memory", TEA)), Completed(Usage(1, 1, 0, 0))], OK]
    executor, background = executor_for(store, provider, turns=10)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True)
    run = await start(store)
    await store.db.execute("UPDATE sessions SET turns_since_memory = 9 WHERE id = ?", "s1")
    await pin_title(store, "s1")
    await executor.execute(run)
    await wait_for_background(background)
    assert (await store.get_session("p1", "s1")).turns_since_memory == 1
    assert review_requests(provider) == []


async def test_a_dropped_review_does_not_stick_the_profile(store):
    provider = FakeProvider()
    provider.script = [OK, OK, REVIEW_SAVE, REVIEW_DONE]
    registry = ProviderRegistry()
    registry.register(provider)
    bus = InMemoryEventBus()
    dropped = RunExecutor(store, bus, registry, sleep=no_sleep, background=Background(limit=0),
                          memory_review_turns=1)
    later, background = executor_for(store, provider, turns=1)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True)
    first = await start(store, "s1")
    await pin_title(store, "s1")
    await dropped.execute(first)
    second = await start(store, "s2")
    await pin_title(store, "s2")
    await later.execute(second)
    await wait_for_background(background)
    assert (await store.get_run("p1", first.id)).status == "completed"
    assert (await store.memory_entries("p1"))["user"] == ["The user likes tea"]
