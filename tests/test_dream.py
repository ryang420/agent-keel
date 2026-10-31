"""The dream end to end with scripted model output: it reads its input, stages gated changes, commits once; failures
retry or stop and commit nothing (learning design §5). Plumbing only: not evidence that learning helps."""

import json

from keel.dream import Dreamer, DreamSettings, TOOLS
from keel.dream_gates import DreamRules
from keel.models import Completed, Message, TextDelta, TextPart, ToolCall, ToolCallReady, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.review_observe import StagedObservation

MODELS = [{"provider": "fake", "model": "m"}]
SETTINGS = DreamSettings(rules=DreamRules(min_observations=2))
DONE = [TextDelta("Done."), Completed(Usage(10, 1, 0, 0))]


async def no_sleep(seconds):
    pass


def step(tool, /, **args):
    return [ToolCallReady(ToolCall(f"c-{tool}", tool, json.dumps(args))), Completed(Usage(10, 2, 0, 0))]


async def setup(store, clock, provider, *, route=True, settings=SETTINGS):
    store.coordination_clock = clock
    await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="", skills=["shipped-skill"], memory=True,
                               learning=True)
    if route:
        await store.ensure_session("p1", "s0")
        run = await store.create_run(profile_id="p1", session_id="s0", provider="fake", model="m",
                                     reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                     instructions=None)
        await store.append_message("s0", run.id, Message("user", (TextPart("Drop the empty rows first"),)))
        await store.finish_run(run.id, "completed", output="ok")
    ids = []
    for session in ("s1", "s2"):
        await store.ensure_session("p1", session)
        row = await store.db.fetchrow("SELECT generation FROM sessions WHERE id = ?", session)
        ids.append(await store._insert_observation(
            "p1", session, row["generation"], StagedObservation("csv cleanup", "When exporting", "Drop empty rows",
                                                                "corrected", ()), now=store.clock()))
    clock.now += 3600
    registry = ProviderRegistry()
    registry.register(provider)
    return Dreamer(store, registry, settings, sleep=no_sleep), ids


async def test_a_dream_creates_a_skill_from_two_sessions_and_commits_once(store, clock):
    provider = FakeProvider()
    dreamer, ids = await setup(store, clock, provider)
    provider.script = [step("create_skill", name="csv-cleanup", description="Clean a CSV export",
                            body="1. Drop empty rows.", observations=ids, rationale="corrected twice"), DONE]
    assert await dreamer.run_once("w1")
    [made] = await store.learned_skills("p1")
    assert (made.name, made.origin) == ("csv-cleanup", "dream")
    [job] = await store.list_dreams("p1")
    assert job.status == "done" and job.usage[-1]["requests"] == 2
    first, second = provider.requests
    assert [tool["name"] for tool in first.tools] == list(TOOLS)
    assert first.max_output_tokens == 4000 and first.cache_scope == "" and first.reasoning_effort == "low"
    assert first.system.startswith("You consolidate") and ids[0] in first.messages[0].text
    assert json.loads(second.messages[-1].text)["success"]
    assert await dreamer.run_once("w1") is False


async def test_thin_evidence_is_refused_to_the_model_and_nothing_is_consumed(store, clock):
    provider = FakeProvider()
    dreamer, ids = await setup(store, clock, provider)
    provider.script = [step("create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.",
                            observations=ids[:1], rationale="once"), DONE]
    assert await dreamer.run_once("w1")
    assert json.loads(provider.requests[1].messages[-1].text)["code"] == "evidence"
    assert await store.learned_skills("p1") == []
    assert {o.state for o in await store.observations("p1")} == {"pending"}
    assert (await store.list_dreams("p1"))[0].status == "done"


async def test_session_search_reads_history_and_unknown_tools_are_refused(store, clock):
    provider = FakeProvider()
    dreamer, _ = await setup(store, clock, provider)
    provider.script = [step("session_search", query="empty rows"), step("terminal", command="id"), DONE]
    assert await dreamer.run_once("w1")
    assert "hits" in json.loads(provider.requests[1].messages[-1].text)
    assert "Unknown tool" in provider.requests[2].messages[-1].text


async def test_running_out_of_iterations_commits_nothing_and_retries(store, clock):
    provider = FakeProvider()
    settings = DreamSettings(iterations=2, rules=DreamRules(min_observations=2))
    dreamer, ids = await setup(store, clock, provider, settings=settings)
    looping = step("dismiss_observation", observation_id=ids[0], reason="noise")
    provider.script = [looping, looping]
    assert await dreamer.run_once("w1")
    [job] = await store.list_dreams("p1")
    assert (job.status, job.error_code) == ("pending", "iteration_limit")
    assert {o.state for o in await store.observations("p1")} == {"pending"}


async def test_a_budget_overrun_fails_the_attempt_for_a_retry(store, clock):
    provider = FakeProvider()
    settings = DreamSettings(input_tokens=50, rules=DreamRules(min_observations=2))
    dreamer, _ = await setup(store, clock, provider, settings=settings)
    assert await dreamer.run_once("w1")
    [job] = await store.list_dreams("p1")
    assert (job.status, job.error_code) == ("pending", "budget") and provider.requests == []


async def test_without_a_route_the_dream_fails_for_the_operator(store, clock):
    provider = FakeProvider()
    dreamer, _ = await setup(store, clock, provider, route=False)
    assert await dreamer.run_once("w1")
    [job] = await store.list_dreams("p1")
    assert (job.status, job.error_code) == ("failed", "no_route") and provider.requests == []


async def test_learning_turned_off_mid_dream_commits_nothing(store, clock):
    class Toggling(FakeProvider):
        async def stream(self, request):
            if len(self.requests) == 1:
                await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="", skills=[], memory=True,
                                           learning=False)
            async for event in super().stream(request):
                yield event

    provider = Toggling()
    dreamer, ids = await setup(store, clock, provider)
    provider.script = [step("create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.",
                            observations=ids, rationale="twice"), DONE]
    assert await dreamer.run_once("w1")
    assert await store.learned_skills("p1") == []
    [job] = await store.list_dreams("p1")
    assert (job.status, job.error_code) == ("discarded", "learning_disabled")


async def test_a_deadline_overrun_fails_the_attempt_for_a_retry(store, clock):
    provider = FakeProvider()
    settings = DreamSettings(deadline_seconds=0.05, rules=DreamRules(min_observations=2))
    dreamer, _ = await setup(store, clock, provider, settings=settings)
    provider.script = [[30.0, *DONE]]
    assert await dreamer.run_once("w1")
    [job] = await store.list_dreams("p1")
    assert (job.status, job.error_code) == ("pending", "deadline")
    assert {o.state for o in await store.observations("p1")} == {"pending"}


async def test_a_cut_off_response_commits_nothing_and_retries(store, clock):
    provider = FakeProvider()
    dreamer, ids = await setup(store, clock, provider)
    provider.script = [[*step("dismiss_observation", observation_id=ids[0], reason="noise")[:1],
                        Completed(Usage(10, 2, 0, 0), status="incomplete")]]
    assert await dreamer.run_once("w1")
    [job] = await store.list_dreams("p1")
    assert (job.status, job.error_code) == ("pending", "incomplete")
    assert {o.state for o in await store.observations("p1")} == {"pending"}
