"""One real model run that calls `terminal` in a Docker sandbox. Needs KEEL_LIVE_BEDROCK=1 and KEEL_TEST_DOCKER=1."""

import asyncio
import json
import os
import subprocess
import uuid

import pytest

from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.bedrock import BedrockMantleProvider
from keel.runner import RunExecutor
from keel.sandbox.coordinator import SandboxCoordinator
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
    coordinator = SandboxCoordinator(store, backend, deployment=backend.deployment, owner="w1",
                                     lease_seconds=30.0, idle_seconds=1800.0, reap_seconds=5.0)
    assert await coordinator.open_admission()
    executor = RunExecutor(store, InMemoryEventBus(), registry, coordinator=coordinator)
    try:
        await store.ensure_session("p1", "s1")
        await store.create_run(profile_id="p1", session_id="s1", provider="bedrock", model=model,
                               reasoning_effort="low", instructions=None, input_parts=[{"type": "text", "text":
                               "Use the terminal tool to run: echo keel-$((6*7)). Then reply with exactly what it printed."}])
        claim = await coordinator.claim()
        run = claim.run
        await asyncio.wait_for(executor.execute(run, usage=claim.use), 180)
        done = await store.get_run("p1", run.id)
        assert done.status == "completed" and "keel-42" in done.output
        assert any(e.type == "tool.started" for e in await store.events_after(run.id, 0))
        tool = [m.message for m in await store.messages("s1") if m.message.role == "tool"][0]
        assert json.loads(tool.text)["output"] == "keel-42"
    finally:
        await registry.aclose()
        remove_deployment(backend.deployment)


def remove_deployment(deployment: str) -> None:
    """Test cleanup by exact ID: remove the containers labelled with this test's own deployment."""
    listed = subprocess.run(["docker", "ps", "-a", "-q", "--no-trunc", "--filter", "label=keel.managed=1",
                             "--filter", f"label=keel.deployment={deployment}"],
                            capture_output=True, text=True, timeout=60)
    if listed.returncode == 0 and listed.stdout.split():
        subprocess.run(["docker", "rm", "-f", *listed.stdout.split()], capture_output=True, timeout=120)
