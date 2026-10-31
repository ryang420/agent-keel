"""Helpers for the procedural learning acceptance tests (learning design §10): profiles, scripted model steps, runs,
and drains of the review and dream queues with the time rules relaxed."""

import json

from keel.background import Background
from keel.dream import Dreamer, DreamSettings
from keel.dream_gates import DreamRules
from keel.events import InMemoryEventBus
from keel.models import Completed, Message, TextDelta, TextPart, ToolCall, ToolCallReady, Usage
from keel.providers.base import ProviderRegistry
from keel.review import MemoryReviewer, ReviewSettings
from keel.review_observe import StagedObservation
from keel.runner import RunExecutor

MODELS = [{"provider": "fake", "model": "m"}]
OK = [TextDelta("ok"), Completed(Usage(1, 1, 0, 0))]
DONE = [TextDelta("Done."), Completed(Usage(1, 1, 0, 0))]
DRAIN = ReviewSettings(turns=1, idle_seconds=0.0, max_age_seconds=0.0)
FORCE = DreamSettings(rules=DreamRules(min_observations=1, debounce_seconds=0.0, interval_seconds=1e9,
                                       daily_attempts=1000))


async def no_sleep(seconds):
    pass


def step(tool, /, **args):
    """One scripted model response that calls `tool` with `args`."""
    return [ToolCallReady(ToolCall(f"c-{tool}", tool, json.dumps(args))), Completed(Usage(1, 1, 0, 0))]


def registry(provider) -> ProviderRegistry:
    found = ProviderRegistry()
    found.register(provider)
    return found


async def learning_profile(store, profile="p1", *, learning=True, tenant_id=None, skills=()):
    return await store.upsert_profile(profile, models=MODELS, sandbox={}, soul="", skills=list(skills), memory=True,
                                      learning=learning, tenant_id=tenant_id)


def executor_for(store, provider) -> RunExecutor:
    return RunExecutor(store, InMemoryEventBus(), registry(provider), sleep=no_sleep, background=Background())


async def turn(store, executor, session, text, profile="p1"):
    await store.ensure_session(profile, session)
    await store.set_title(profile, session, f"Pinned {session}", "user")  # no model-title call in the script
    await store.create_run(profile_id=profile, session_id=session, provider="fake", model="m",
                           reasoning_effort="low", input_parts=[{"type": "text", "text": text}], instructions=None)
    run = await store.claim_run("w1", 9_999_999)
    await executor.execute(run)
    return await store.get_run(profile, run.id)


async def user_message_ids(store, session) -> list[int]:
    return [item.id for item in await store.messages(session) if item.message.role == "user"]


async def observe(store, session, *, outcome="corrected", topic="csv export", profile="p1") -> str:
    await store.ensure_session(profile, session)
    row = await store.db.fetchrow("SELECT generation FROM sessions WHERE id = ?", session)
    return await store._insert_observation(profile, session, row["generation"],
                                           StagedObservation(topic, "When exporting", "Drop empty rows", outcome, ()),
                                           now=store.clock())


async def seed_run(store, profile="p1", session="s0"):
    """One completed run: the dream takes its model from the profile's last completed run."""
    await store.ensure_session(profile, session)
    run = await store.create_run(profile_id=profile, session_id=session, provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}], instructions=None)
    await store.append_message(session, run.id, Message("user", (TextPart("hi"),)))
    await store.finish_run(run.id, "completed", output="ok")


async def drain_reviews(store, providers) -> None:
    reviewer = MemoryReviewer(store, providers, DRAIN, sleep=no_sleep)
    for _ in range(20):
        if not await reviewer.run_once("acceptance"):
            return


async def drain_dreams(store, providers, settings=FORCE) -> None:
    dreamer = Dreamer(store, providers, settings, sleep=no_sleep)
    for _ in range(10):
        if not await dreamer.run_once("acceptance"):
            return
