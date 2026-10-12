"""One real model run that calls `terminal` in a Docker sandbox. Needs KEEL_LIVE_BEDROCK=1 and KEEL_TEST_DOCKER=1."""

import asyncio
import json
import os
import uuid

import pytest

from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.bedrock import BedrockMantleProvider
from keel.runner import RunExecutor
from keel.sandbox.docker import DockerSandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("KEEL_LIVE_BEDROCK") != "1" or os.environ.get("KEEL_TEST_DOCKER") != "1",
    reason="set KEEL_LIVE_BEDROCK=1 and KEEL_TEST_DOCKER=1")


async def test_a_live_run_uses_the_terminal(store):
    model = os.environ.get("KEEL_LIVE_MODEL", "openai.gpt-6-luna")
    registry = ProviderRegistry()
    registry.register(BedrockMantleProvider(os.environ.get("AWS_REGION", "us-east-1")))
    backend = DockerSandbox(deployment=f"live-{uuid.uuid4().hex[:8]}")
    image = os.environ.get("KEEL_TEST_DOCKER_IMAGE", "python:3.12-slim")
    await store.upsert_profile("p1", models=[{"provider": "bedrock", "model": model}], soul="", skills=[],
                               sandbox={"image": image, "workdir": "/work"})
    executor = RunExecutor(store, InMemoryEventBus(), registry, sandbox=backend)
    try:
        await store.ensure_session("p1", "s1")
        await store.create_run(profile_id="p1", session_id="s1", provider="bedrock", model=model,
                               reasoning_effort="low", instructions=None, input_parts=[{"type": "text", "text":
                               "Use the terminal tool to run: echo keel-$((6*7)). Then reply with exactly what it printed."}])
        run = await store.claim_run("w1", 9_999_999)
        await asyncio.wait_for(executor.execute(run), 180)
        done = await store.get_run("p1", run.id)
        assert done.status == "completed" and "keel-42" in done.output
        assert any(e.type == "tool.started" for e in await store.events_after(run.id, 0))
        tool = [m.message for m in await store.messages("s1") if m.message.role == "tool"][0]
        assert json.loads(tool.text)["output"] == "keel-42"
    finally:
        await registry.aclose()
        await backend.remove_orphans()
