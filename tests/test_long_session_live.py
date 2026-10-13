"""A real model keeps facts across compactions. Needs KEEL_LIVE_BEDROCK=1; the model is KEEL_LIVE_MODEL."""

import asyncio
import os

import pytest

from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.bedrock import BedrockMantleProvider
from keel.runner import RunExecutor

pytestmark = pytest.mark.skipif(os.environ.get("KEEL_LIVE_BEDROCK") != "1", reason="set KEEL_LIVE_BEDROCK=1")
FILLER = "Here is a log excerpt to keep for later; no need to analyse it. " + "lorem ipsum dolor sit amet " * 400


async def test_planted_facts_survive_compaction(store):
    model = os.environ.get("KEEL_LIVE_MODEL", "openai.gpt-6-luna")
    registry = ProviderRegistry()
    registry.register(BedrockMantleProvider(os.environ.get("AWS_REGION", "us-east-1")))
    window = registry.get("bedrock").capabilities(model).context_window
    executor = RunExecutor(store, InMemoryEventBus(), registry, compression_threshold=12_000 / window)
    await store.upsert_profile("p1", models=[{"provider": "bedrock", "model": model}], sandbox={}, soul="",
                               skills=[])
    turns = ["Hello.", "I am planning a release.", "The release codename is HERON-417.",
             "The release date is 3 November 2026.", *[FILLER] * 6,
             "What are the release codename and the release date? Answer in one line."]
    run_ids = []
    try:
        await store.ensure_session("p1", "s1")
        for text in turns:
            await store.create_run(profile_id="p1", session_id="s1", provider="bedrock", model=model,
                                   reasoning_effort="low", instructions=None,
                                   input_parts=[{"type": "text", "text": text}])
            run = await store.claim_run("w1", 9_999_999)
            await asyncio.wait_for(executor.execute(run), 300)
            run_ids.append(run.id)
        last = await store.get_run("p1", run_ids[-1])
        assert last.status == "completed"
        assert "HERON-417" in last.output and ("3 November" in last.output or "November 3" in last.output)
        events = [e for run_id in run_ids for e in await store.events_after(run_id, 0) if e.type == "context.compacted"]
        assert events
    finally:
        await registry.aclose()
