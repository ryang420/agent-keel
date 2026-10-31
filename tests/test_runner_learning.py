"""Learned skills at the prompt boundary (learning design §2, §3, §10)."""

import json

from keel import prompts
from keel.background import Background
from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.store.base import SkillSource

MODELS = [{"provider": "fake", "model": "m"}]
OK = [TextDelta("ok"), Completed(Usage(1, 1, 0, 0))]
METHOD = {"action": "create", "name": "weekly-report", "description": "How this user wants the weekly report",
          "body": "1. Conclusion first.\n2. Then a table by region."}


async def no_sleep(seconds):
    pass


def executor_for(store, provider):
    registry = ProviderRegistry()
    registry.register(provider)
    return RunExecutor(store, InMemoryEventBus(), registry, sleep=no_sleep, background=Background())


async def turn(store, executor, session, text="hello"):
    await store.ensure_session("p1", session)
    await store.set_title("p1", session, f"Pinned {session}", "user")  # no model-title call in the script
    await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m", reasoning_effort="low",
                           input_parts=[{"type": "text", "text": text}], instructions=None)
    run = await store.claim_run("w1", 9_999_999)
    await executor.execute(run)
    return await store.get_run("p1", run.id)


async def learning_profile(store, learning=True):
    await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="", skills=[], memory=True, learning=learning)


def names(request):
    return [tool["name"] for tool in request.tools]


async def test_learning_offers_the_skill_tools_without_a_sandbox_and_lists_active_skills(store):
    await learning_profile(store)
    await store.change_learned_skill("p1", METHOD, source=SkillSource("consumer"))
    provider = FakeProvider(script=[OK])
    await turn(store, executor_for(store, provider), "a")
    request = provider.requests[0]
    assert names(request) == ["memory", "session_search", "skills_list", "skill_view", "skill_manage"]
    assert prompts.text("learning") in request.system
    assert "  learned:\n    - weekly-report: How this user wants the weekly report" in request.system


async def test_without_learning_nothing_changes(store):
    await learning_profile(store, learning=False)
    await store.change_learned_skill("p1", METHOD, source=SkillSource("consumer"))
    provider = FakeProvider(script=[OK])
    await turn(store, executor_for(store, provider), "a")
    assert names(provider.requests[0]) == ["memory", "session_search"]
    assert "weekly-report" not in provider.requests[0].system


async def test_a_from_now_on_request_becomes_a_skill_listed_in_the_next_session_only(store):
    """§10 row 1: a foreground skill at version 1 with the user message as evidence, listed in the next session's
    index; the open session's prompt and tools stay byte for byte, with one model call per answer."""
    await learning_profile(store)
    create = [ToolCallReady(ToolCall("c1", "skill_manage", json.dumps(METHOD))), Completed(Usage(1, 1, 0, 0))]
    provider = FakeProvider(script=[create, OK, OK, OK])
    executor = executor_for(store, provider)
    run = await turn(store, executor, "a", "From now on, write the weekly report conclusion first, then a table.")
    [skill] = await store.learned_skills("p1")
    assert (skill.version, skill.origin) == (1, "foreground")
    [version] = await store.learned_skill_versions("p1", skill.id)
    user_id = await store.run_user_message_id("a", run.id)
    assert version.message_ids == (user_id,) and version.run_id == run.id
    await turn(store, executor, "a", "Thanks.")
    first, second, third = provider.requests[0], provider.requests[1], provider.requests[2]
    assert first.system == second.system == third.system and first.tools == second.tools == third.tools
    assert "weekly-report" not in first.system
    await turn(store, executor, "b", "Weekly report please.")
    assert "- weekly-report: How this user wants the weekly report" in provider.requests[3].system
    assert len(provider.requests) == 4
