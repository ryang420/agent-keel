"""Real Docker sandbox retirement for a complete profile deletion.

Opt-in: set ``KEEL_TEST_DOCKER=1``. The packaged supervisor helper for the
daemon's architecture must already be built (``scripts/build_supervisor.py``).
Every container this test creates is labelled with a unique deployment, and the
fixture removes and verifies none survives.
"""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest

from keel.deletion import ProfileDeletionService
from keel.sandbox.coordinator import SandboxCoordinator
from keel.sandbox.docker import DockerSandbox
from keel.sandbox.runtime import helper_artifact
from keel.sandbox.base import SandboxError
from keel.store import open_store
from tests.sandbox_support import queue_run

needs_docker = pytest.mark.skipif(os.environ.get("KEEL_TEST_DOCKER") != "1", reason="set KEEL_TEST_DOCKER=1")
pytestmark = needs_docker
IMAGE = os.environ.get("KEEL_TEST_DOCKER_IMAGE", "python:3.12-slim")
MODELS = [{"provider": "fake", "model": "m"}]
LEASE = 30.0
OP_A = "11111111-1111-4111-8111-111111111111"


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def deployment_containers(deployment: str) -> list[str]:
    output = docker("ps", "-a", "--no-trunc", "-q", "--filter", "label=keel.managed=1",
                    "--filter", f"label=keel.deployment={deployment}")
    return sorted(output.split())


def container_exists(container_id: str) -> bool:
    return subprocess.run(["docker", "inspect", "--type", "container", container_id],
                          capture_output=True, timeout=60).returncode == 0


@pytest.fixture
def deployment():
    name = f"keel-del-{uuid.uuid4().hex[:12]}"
    yield name
    leftovers = deployment_containers(name)
    if leftovers:
        docker("rm", "-f", *leftovers)
    assert deployment_containers(name) == []


@pytest.fixture
def backend(deployment, tmp_path):
    architecture = docker("version", "--format", "{{.Server.Arch}}")
    try:
        helper_artifact(architecture)
    except SandboxError as exc:
        pytest.fail(f"build the packaged helper first (scripts/build_supervisor.py): {exc}")
    return DockerSandbox(deployment=deployment, kill_grace_s=0.5, reap_seconds=2.0,
                         mount_root=str(tmp_path / "mounts"))


async def seed(store, profile_id: str) -> None:
    await store.upsert_profile(profile_id, models=MODELS, soul="", skills=[],
                               sandbox={"image": IMAGE, "workdir": "/work"}, tenant_id=1)


async def live_generation(store, backend, coordinator, profile_id: str, session: str) -> str:
    """One finished run leaves its sandbox generation ready and idle."""
    await seed(store, profile_id)
    await queue_run(store, session, profile_id=profile_id)
    claim = await coordinator.claim()
    assert claim is not None and claim.run.profile_id == profile_id and claim.use is not None
    bound = await coordinator.prepare(claim.use)
    container_id = bound.binding.container_id
    assert container_id and container_exists(container_id)
    await store.finish_run(claim.run.id, "completed", output="ok")
    assert await coordinator.release(claim.use)
    return container_id


async def test_delete_retires_the_profiles_live_sandbox(tmp_path, backend, pg_dsn):
    store = await open_store(pg_dsn)
    coordinator = SandboxCoordinator(store, backend, deployment=backend.deployment, owner="instance-del",
                                     lease_seconds=LEASE, idle_seconds=1800.0, reap_seconds=2.0)
    try:
        assert await coordinator.open_admission()
        target = await live_generation(store, backend, coordinator, "p1", "s1")
        other = await live_generation(store, backend, coordinator, "p2", "s2")
        assert target != other and len(deployment_containers(backend.deployment)) == 2

        service = ProfileDeletionService(store, coordinator=coordinator)
        await service.delete("p1", 1, OP_A, timeout_seconds=60)

        assert await store.get_profile("p1") is None
        assert await store.get_profile_deletion("p1") is not None
        assert not container_exists(target), "the target profile's container must be gone"
        assert container_exists(other), "another profile's sandbox must be untouched"
        assert target not in deployment_containers(backend.deployment)

        await service.complete("p1", 1, OP_A)
        assert await store.get_profile_deletion("p1") is None

        # The surviving generation still retires normally.
        await coordinator.retire_profile("p2", timeout_seconds=60)
        assert not container_exists(other)
    finally:
        await store.close()

