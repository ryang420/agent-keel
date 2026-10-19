"""The coordinated Docker backend: stable generation names and the removed name-based lifecycle surface.

Coordinated behaviour (create, install, start, exec, cancellation, removal) lives in
`test_docker_operations.py` and the opt-in `test_sandbox_lifecycle_docker.py`.
"""

import pytest

from keel.sandbox.base import SandboxError
from keel.sandbox.docker import DockerSandbox

LEGACY_METHODS = (
    "reference", "ensure", "exec", "_exec_once", "_helper", "read_file", "write_file", "kill_tree", "destroy",
    "reap_idle", "remove_orphans", "_container_gone", "_docker", "run_args",
)
LEGACY_STATE = ("_active", "_last_used", "_clients", "_locks")
COORDINATED_METHODS = (
    "daemon_identity", "inspect_id", "inventory", "create_generation", "install_helper", "start_id", "remove_id",
    "exec_bound", "control_exec",
)


def test_the_container_name_is_stable_per_deployment_and_profile():
    a = DockerSandbox(deployment="d1")
    assert a.container_name("p1") == a.container_name("p1") != a.container_name("p2")
    assert a.container_name("p1") != DockerSandbox(deployment="d2").container_name("p1")
    assert a.container_name("p1").startswith("keel-sbx-")


def test_the_legacy_name_based_lifecycle_surface_is_gone():
    backend = DockerSandbox(deployment="unit")
    for name in LEGACY_METHODS:
        assert not hasattr(backend, name), name
    for name in LEGACY_STATE:
        assert not hasattr(backend, name), name
    for name in COORDINATED_METHODS:
        assert callable(getattr(backend, name)), name


async def test_a_missing_docker_cli_is_a_sandbox_error():
    backend = DockerSandbox(docker="keel-no-such-docker")
    with pytest.raises(SandboxError):
        await backend.daemon_identity()
