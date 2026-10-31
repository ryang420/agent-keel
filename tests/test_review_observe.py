"""The review's observe tool end to end: observations are staged against the chunk and commit with the review, or not
at all (procedural learning design §4)."""

import json

from keel import prompts
from keel.background import Background
from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.review import MemoryReviewer, ReviewSettings
from keel.runner import RunExecutor

OK = [TextDelta("ok"), Completed(Usage(1, 1, 0, 0))]
DONE = [TextDelta("Nothing to save."), Completed(Usage(12, 1, 0, 0))]
SETTINGS = ReviewSettings(turns=1)


async def no_sleep(seconds):
    pass


async def setup(store, clock, run_script, *, learning=True, text="No, put the conclusion first."):
    """Run one foreground turn, then return the run and a function that builds the reviewer from the review's script
    (which may cite message ids, so it is known only after the run)."""
    store.coordination_clock = clock
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True, learning=learning)
    registry = ProviderRegistry()
    registry.register(FakeProvider(script=run_script))
    executor = RunExecutor(store, InMemoryEventBus(), registry, sleep=no_sleep, background=Background())
    await store.ensure_session("p1", "s1")
    await store.set_title("p1", "s1", "Kept", "user")
    await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort="low",
                           input_parts=[{"type": "text", "text": text}], instructions=None)
    run = await store.claim_run("w1", 9_999_999)
    await executor.execute(run)

    def reviewer(script):
        provider = FakeProvider(script=script)
        reviewers = ProviderRegistry()
        reviewers.register(provider)
        return MemoryReviewer(store, reviewers, SETTINGS, sleep=no_sleep), provider

    return run, reviewer


def observe(evidence, outcome="corrected", call_id="o1"):
    args = {"topic": "weekly report", "situation": "The user asked for the weekly report.",
            "approach": "Conclusion first, then a table by region.", "outcome": outcome, "evidence": evidence}
    return ToolCallReady(ToolCall(call_id, "observe", json.dumps(args)))


def calls(*items, usage=Usage(5, 2, 0, 0), status="completed"):
    return [*items, Completed(usage, status=status)]


async def test_a_review_with_learning_records_an_observation_citing_the_conversation(store, clock):
    run, build = await setup(store, clock, [OK])
    user = await store.run_user_message_id("s1", run.id)
    reviewer, provider = build([calls(observe([user])), DONE])
    assert await reviewer.run_once("w1")
    [item] = await store.observations("p1")
    assert (item.outcome, item.evidence, item.state) == ("corrected", (user,), "pending")
    request = provider.requests[0]
    assert [tool["name"] for tool in request.tools] == ["memory", "observe"]
    assert request.system == prompts.text("memory_review") + "\n\n" + prompts.text("memory_review_learning")


async def test_without_learning_the_review_is_unchanged_and_observe_is_unknown(store, clock):
    run, build = await setup(store, clock, [OK], learning=False)
    user = await store.run_user_message_id("s1", run.id)
    reviewer, provider = build([calls(observe([user])), DONE])
    assert await reviewer.run_once("w1")
    first, second = provider.requests
    assert [tool["name"] for tool in first.tools] == ["memory"]
    assert first.system == prompts.text("memory_review")
    assert "Unknown tool: observe" in second.messages[-1].text
    assert await store.observations("p1") == []


async def test_a_method_only_in_a_tool_result_cannot_be_cited(store, clock):
    """§10: the review reads only user and assistant text; a tool message id is refused and nothing is stored."""
    listing = calls(ToolCallReady(ToolCall("m1", "memory", json.dumps({"action": "list"}))))
    run, build = await setup(store, clock, [listing, OK])
    [tool] = [stored.id for stored in await store.messages("s1") if stored.message.role == "tool"]
    reviewer, provider = build([calls(observe([tool])), DONE])
    assert await reviewer.run_once("w1")
    assert "Cite only message ids shown" in provider.requests[1].messages[-1].text
    assert await store.observations("p1") == []
    assert (await store.list_reviews("p1"))[0].status == "done"


async def test_an_iteration_cap_run_cannot_yield_worked(store, clock):
    run, build = await setup(store, clock, [OK])
    await store.db.execute("UPDATE runs SET capped = 1 WHERE id = ?", run.id)
    user = await store.run_user_message_id("s1", run.id)
    reviewer, provider = build([calls(observe([user], "worked", "o1")), calls(observe([user], "corrected", "o2")),
                                DONE])
    assert await reviewer.run_once("w1")
    assert "did not finish its task normally" in provider.requests[1].messages[-1].text
    [item] = await store.observations("p1")
    assert item.outcome == "corrected"


async def test_a_failed_review_leaves_no_observation(store, clock):
    run, build = await setup(store, clock, [OK])
    user = await store.run_user_message_id("s1", run.id)
    reviewer, _ = build([calls(observe([user])), [TextDelta("cut"), Completed(Usage(1, 1, 0, 0), status="incomplete")]])
    assert await reviewer.run_once("w1")
    assert await store.observations("p1") == []
    [job] = await store.list_reviews("p1")
    assert (job.status, job.error_code) == ("pending", "incomplete")
