"""Exact-ID Docker primitives and helper installation.

The default tests drive a fake docker CLI and synthetic helper bytes: no Docker daemon, no network and no build.
The real-Docker probes at the end are opt-in with KEEL_TEST_DOCKER=1.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import uuid

import pytest

from keel.sandbox import docker as docker_module
from keel.sandbox.base import TIMEOUT_EXIT_CODE, SandboxError, parse_policy, profile_mount_root
from keel.sandbox.docker import DockerSandbox, _DockerBinaryResult, _DockerResult, _env_file
from keel.sandbox.records import (
    CleanupReceipt, ExecTicket, LifecycleAttempt, SandboxBinding, SandboxKey, SandboxUncertain, UseHandle,
)
from keel.sandbox import runtime as runtime_module
from keel.sandbox.runtime import HelperArtifact

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = "dep"
DAEMON = "daemon-1"
IMAGE = "img:1"


def exact_id() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def token() -> str:
    return uuid.uuid4().hex


def policy(**extra):
    return parse_policy({"image": IMAGE, "workdir": "/w", **extra}, "/")


class FakeDocker:
    """A docker CLI stand-in; handlers return (exit code, output) or a `_DockerResult`."""

    def __init__(self, handler, *, binary: bool = False, calls: list | None = None) -> None:
        self.handler = handler
        self.binary = binary
        self.calls: list[tuple[tuple[str, ...], bytes | None]] = calls if calls is not None else []

    async def __call__(self, *args, stdin=None, timeout=60.0):
        self.calls.append((args, stdin))
        result = self.handler(list(args), stdin)
        if asyncio.iscoroutine(result):
            result = await result
        if self.binary:
            if isinstance(result, _DockerBinaryResult):
                return result
            if isinstance(result, _DockerResult):
                return _DockerBinaryResult(result.code, b"", result.output, result.timed_out)
            code, output = result
            return _DockerBinaryResult(code, output.encode() if isinstance(output, str) else output, "")
        if isinstance(result, _DockerResult):
            return result
        code, output = result
        return _DockerResult(code, output.decode("utf-8", "replace") if isinstance(output, bytes) else output)

    def commands(self) -> list[list[str]]:
        return [list(args) for args, _ in self.calls]


def attach(backend: DockerSandbox, handler) -> FakeDocker:
    fake = FakeDocker(handler)
    backend._docker_raw = fake
    backend._docker_bytes = FakeDocker(handler, binary=True, calls=fake.calls)
    return fake


@pytest.fixture
def backend(tmp_path) -> DockerSandbox:
    instance = DockerSandbox(deployment=DEPLOYMENT, receipt_timeout_s=0, mount_root=str(tmp_path))
    instance._identity = (DAEMON, "amd64")  # cached daemon identity; no info/version calls in unit tests
    return instance


def mount_source(backend: DockerSandbox, name: str = "data") -> str:
    root = profile_mount_root(backend.mount_root, "p1")
    path = Path(root) / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def make_key(*, deployment=DEPLOYMENT, profile_id="p1", daemon_id=DAEMON) -> SandboxKey:
    return SandboxKey(deployment, profile_id, daemon_id)


def make_attempt(kind: str, *, container_id: str | None = None, intended_name: str | None = None,
                 key: SandboxKey | None = None, generation_token: str | None = None) -> LifecycleAttempt:
    return LifecycleAttempt(
        id=f"op-{kind}-{uuid.uuid4().hex[:8]}", kind=kind, key=key or make_key(), generation=3,
        generation_token=generation_token or token(), authority="current", outcome="dispatched", owner="w1",
        owner_token=token(), created_at=1.0, updated_at=1.0, intended_name=intended_name,
        original_create_operation_id="op-create-original", usage_id="usage-1", usage_token=token(),
        container_id=container_id)


def make_ticket(*, container_id: str | None = None, name: str = "keel-sbx-generation") -> ExecTicket:
    binding = SandboxBinding(3, token(), container_id or exact_id(), name, policy(user="1000:1000"), "a" * 16)
    use = UseHandle(make_key(), "usage-1", token(), "w1")
    return ExecTicket("exec-" + uuid.uuid4().hex[:8], use, binding, token())


def inspect_output(container_id: str, *, name: str = "/keel-sbx-generation", running: bool = False,
                   labels: dict | None = None) -> str:
    return f"{container_id}|{name}|{'true' if running else 'false'}|{json.dumps(labels or {})}\n"


def receipt_json(ticket: ExecTicket, **overrides) -> str:
    value = {"version": 1, "exec_id": ticket.exec_id, "control_token": ticket.control_token,
             "leader_status": 0, "control_closed": True, "descendants_complete": True,
             "uncertain_reason": None}
    value.update(overrides)
    return json.dumps(value) + "\n"


def image_volumes(volumes=None):
    result = None if volumes is None else {path: {} for path in volumes}
    return (0, json.dumps(result) + "\n")


# ---------------------------------------------------------------------------
# daemon_identity
# ---------------------------------------------------------------------------

async def test_daemon_identity_normalizes_and_is_cached():
    instance = DockerSandbox(deployment=DEPLOYMENT)
    calls = []

    async def raw(*args, stdin=None, timeout=60.0):
        calls.append(args)
        return _DockerResult(0, "daemon-9\n" if args[0] == "info" else "x86_64\n")

    instance._docker_raw = raw
    assert await instance.daemon_identity() == ("daemon-9", "amd64")
    assert await instance.daemon_identity() == ("daemon-9", "amd64")
    assert [list(call) for call in calls] == [["info", "--format", "{{.ID}}"],
                                              ["version", "--format", "{{.Server.Arch}}"]]


@pytest.mark.parametrize("raw", [("s390x",), ("arm64", "timeout")])
async def test_daemon_identity_rejects_unsupported_architectures_or_failure(raw):
    instance = DockerSandbox(deployment=DEPLOYMENT)
    arch, *failure = raw

    async def docker(*args, stdin=None, timeout=60.0):
        if args[0] == "info":
            return _DockerResult(1, "cannot connect") if failure else _DockerResult(0, "daemon-9\n")
        return _DockerResult(0, arch + "\n")

    instance._docker_raw = docker
    with pytest.raises(SandboxError):
        await instance.daemon_identity()


# ---------------------------------------------------------------------------
# inspect_id
# ---------------------------------------------------------------------------

async def test_inspect_id_uses_the_full_id_and_returns_the_observation(backend):
    container = exact_id()
    fake = attach(backend, lambda args, stdin: (
        0, inspect_output(container, name="/keel-sbx-g", running=True,
                          labels={"keel.deployment": DEPLOYMENT, "keel.policy": "abc"})))

    outcome = await backend.inspect_id(container)

    assert outcome.state == "succeeded" and outcome.uncertain is False
    observation = outcome.observation
    assert observation.daemon_id == DAEMON
    assert observation.container_id == container
    assert observation.container_name == "keel-sbx-g"
    assert observation.running is True
    assert observation.policy_digest == "abc"
    assert fake.commands()[0][-1] == container


@pytest.mark.parametrize("output", [
    "Error: No such object: ",
    "Error response from daemon: No such container: ",
])
async def test_inspect_id_treats_a_definite_not_found_as_absence(backend, output):
    container = exact_id()
    attach(backend, lambda args, stdin: (1, output + container + "\n"))

    outcome = await backend.inspect_id(container)

    assert outcome.state == "definite_failure" and outcome.uncertain is False
    assert outcome.observation is None


@pytest.mark.parametrize("result", [
    (1, "Error response from daemon: permission denied"),
    (1, "error during connect: this error may indicate that the docker daemon is not running"),
    (125, "unknown error"),
    (0, "not the expected format"),
    (0, inspect_output("0" * 64)),
    _DockerResult(None, "", timed_out=True),
    _DockerResult(-9, "killed"),
])
async def test_inspect_id_never_infers_absence_from_an_arbitrary_result(backend, result):
    attach(backend, lambda args, stdin: result)
    outcome = await backend.inspect_id(exact_id())
    assert outcome.uncertain is True


async def test_inspect_id_rejects_a_reusable_name_before_dispatch(backend):
    fake = attach(backend, lambda args, stdin: (0, ""))
    with pytest.raises(ValueError):
        await backend.inspect_id(backend.container_name("p1"))
    assert fake.calls == []


# ---------------------------------------------------------------------------
# create_generation
# ---------------------------------------------------------------------------

def _create_handler(container: str, *, volumes=None, create=(0, None), image=None):
    def handler(args, stdin):
        if args[:2] == ["image", "inspect"]:
            return image if image is not None else image_volumes(volumes)
        if args[0] == "create":
            if isinstance(create, _DockerResult):
                return create
            code, output = create
            return code, container + "\n" if output is None else output
        raise AssertionError(f"unexpected docker call {args!r}")

    return handler


async def test_create_generation_creates_by_identity_and_never_starts(backend):
    container = exact_id()
    attempt = make_attempt("create", intended_name="keel-sbx-abc-generation")
    secret = "secret-" + uuid.uuid4().hex
    fake = attach(backend, _create_handler(container))
    selected = policy(env={"TOKEN": secret}, network="net", mounts=[])

    outcome = await backend.create_generation(attempt, selected)

    assert outcome.state == "succeeded" and outcome.uncertain is False
    observation = outcome.observation
    assert observation.container_id == container
    assert observation.container_name == "keel-sbx-abc-generation"
    assert observation.running is False
    assert observation.policy_digest == selected.digest()
    create = next(args for args in fake.commands() if args[0] == "create")
    assert "run" not in create and "start" not in create and "rm" not in create
    labels = {create[i + 1] for i, value in enumerate(create) if value == "--label"}
    assert "keel.managed=1" in labels
    assert f"keel.deployment={DEPLOYMENT}" in labels
    assert "keel.profile=p1" in labels
    assert "keel.generation=3" in labels
    assert f"keel.generation-token={attempt.generation_token}" in labels
    assert f"keel.policy={selected.digest()}" in labels
    assert f"keel.create-operation={attempt.original_create_operation_id}" in labels
    for flag, value in (("--cap-drop", "ALL"), ("--cap-add", "DAC_OVERRIDE"), ("--cap-add", "CHOWN"),
                        ("--cap-add", "FOWNER"), ("--security-opt", "no-new-privileges"),
                        ("--tmpfs", "/tmp:rw,nosuid,size=512m"), ("--tmpfs", "/var/tmp:rw,nosuid"),
                        ("--tmpfs", "/run:rw,nosuid"), ("--runtime", "runc"), ("--workdir", "/w"),
                        ("--network", "net")):
        assert any(create[i] == flag and create[i + 1] == value for i in range(len(create) - 1)), (flag, value)
    assert create[0] == "create" and create[1:3] == ["--init", "--name"]
    assert create[-3:] == [IMAGE, "sleep", "infinity"]
    assert secret not in create and not any(value == f"TOKEN={secret}" for value in create)


async def test_create_generation_names_a_missing_intended_name_from_the_generation_token(backend):
    container = exact_id()
    attempt = make_attempt("create")
    fake = attach(backend, _create_handler(container))

    outcome = await backend.create_generation(attempt, policy())

    name = fake.commands()[1][fake.commands()[1].index("--name") + 1]
    assert fake.commands()[0][0] == "image"  # the image volume check precedes creation
    assert name.startswith("keel-sbx-") and attempt.generation_token[:16] in name
    assert name != backend.container_name("p1")
    assert outcome.observation.container_name == name


async def test_create_generation_rejects_a_reusable_profile_name(backend):
    fake = attach(backend, _create_handler(exact_id()))
    attempt = make_attempt("create", intended_name=backend.container_name("p1"))
    with pytest.raises(ValueError):
        await backend.create_generation(attempt, policy())
    assert fake.calls == []


@pytest.mark.parametrize("target", ["/.keel-runtime", "/.keel-runtime/helper", "/.keel-runtime/data/sub"])
async def test_create_generation_rejects_policy_mount_overlap_with_the_runtime_path(backend, target):
    fake = attach(backend, _create_handler(exact_id()))
    selected = policy(mounts=[{"source": "/srv/mount", "target": target, "read_only": True}])
    outcome = await backend.create_generation(make_attempt("create"), selected)
    assert outcome.state == "definite_failure" and outcome.uncertain is False
    assert not any(args[0] == "create" for args in fake.commands())


@pytest.mark.parametrize("volume", ["/.keel-runtime", "/.keel-runtime/helper"])
async def test_create_generation_rejects_image_declared_volume_overlap(backend, volume):
    fake = attach(backend, _create_handler(exact_id(), volumes=[volume]))
    outcome = await backend.create_generation(make_attempt("create"), policy())
    assert outcome.state == "definite_failure" and outcome.uncertain is False
    assert not any(args[0] == "create" for args in fake.commands())


async def test_create_generation_accepts_a_mount_beside_the_runtime_path(backend):
    fake = attach(backend, _create_handler(exact_id(), volumes=["/data"]))
    selected = policy(mounts=[{"source": mount_source(backend), "target": "/data", "read_only": False}])
    outcome = await backend.create_generation(make_attempt("create"), selected)
    assert outcome.state == "succeeded"
    assert any(args[0] == "create" for args in fake.commands())


@pytest.mark.parametrize("create,state", [
    ((1, "Error response from daemon: image not found\n"), "definite_failure"),
    ((125, "unknown flag: --something\n"), "uncertain"),
    (_DockerResult(None, "", timed_out=True), "uncertain"),
    (_DockerResult(-15, "killed"), "uncertain"),
])
async def test_create_generation_separates_definite_rejection_from_uncertainty(backend, create, state):
    attach(backend, _create_handler(exact_id(), create=create))
    outcome = await backend.create_generation(make_attempt("create"), policy())
    assert outcome.state == state


async def test_create_generation_requires_an_exact_returned_id(backend):
    attach(backend, _create_handler(exact_id(), create=(0, "not-an-id\n")))
    outcome = await backend.create_generation(make_attempt("create"), policy())
    assert outcome.uncertain is True


async def test_create_generation_blocks_on_daemon_identity_drift(backend):
    backend._identity = ("another-daemon", "amd64")
    fake = attach(backend, _create_handler(exact_id()))
    outcome = await backend.create_generation(make_attempt("create"), policy())
    assert outcome.state == "definite_failure" and outcome.uncertain is False
    assert "daemon" in (outcome.diagnostic or "").lower()
    assert fake.calls == []


async def test_create_generation_is_uncertain_when_the_daemon_identity_cannot_be_read(backend):
    async def identity():
        raise SandboxError("could not read the Docker daemon identity")

    backend.daemon_identity = identity
    fake = attach(backend, _create_handler(exact_id()))
    outcome = await backend.create_generation(make_attempt("create"), policy())
    assert outcome.uncertain is True
    assert fake.calls == []


@pytest.mark.parametrize("image,state", [
    ((1, "Error: No such image: img:1\n"), "definite_failure"),
    ((1, "Error response from daemon: toomanyrequests\n"), "definite_failure"),
    ((125, "unknown flag: --something\n"), "uncertain"),
    (_DockerResult(None, "", timed_out=True), "uncertain"),
    (_DockerResult(-9, "killed"), "uncertain"),
    ((0, "not json\n"), "uncertain"),
    ((0, "[1, 2]\n"), "uncertain"),
])
async def test_create_generation_classifies_image_inspection_failures(backend, image, state):
    fake = attach(backend, _create_handler(exact_id(), image=image))
    outcome = await backend.create_generation(make_attempt("create"), policy())
    assert outcome.state == state
    assert not any(args[0] == "create" for args in fake.commands())


async def test_create_generation_requires_a_create_attempt(backend):
    attach(backend, _create_handler(exact_id()))
    with pytest.raises(ValueError):
        await backend.create_generation(make_attempt("start", container_id=exact_id()), policy())


# ---------------------------------------------------------------------------
# install_helper
# ---------------------------------------------------------------------------

@pytest.fixture
def artifact_directory(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setattr(docker_module, "artifact_directory", lambda: directory)
    return directory


def make_artifact(directory: Path, *, arch: str = "amd64", protocol: int = 1,
                  body: bytes | None = None) -> HelperArtifact:
    data = body if body is not None else b"\x7fELF" + arch.encode() + b"-fixture"
    path = directory / f"keel-supervisor-{arch}"
    path.write_bytes(data)
    path.chmod(0o755)
    return HelperArtifact(path, hashlib.sha256(data).hexdigest(), protocol, arch)


def helper_readback(data: bytes, *, directory_mode: int = 0o755, directory_uid: int = 0,
                    directory_gid: int = 0, mode: int = 0o755, uid: int = 0, gid: int = 0,
                    body: bytes | None = None, extra: list[tarfile.TarInfo] | None = None) -> bytes:
    """What `docker cp <id>:/.keel-runtime -` returns after a correct install."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        directory = tarfile.TarInfo(".keel-runtime")
        directory.type = tarfile.DIRTYPE
        directory.mode = directory_mode
        directory.uid, directory.gid = directory_uid, directory_gid
        archive.addfile(directory)
        helper = tarfile.TarInfo(".keel-runtime/keel-supervisor")
        payload = data if body is None else body
        helper.size = len(payload)
        helper.mode = mode
        helper.uid, helper.gid = uid, gid
        archive.addfile(helper, io.BytesIO(payload))
        for member in extra or []:
            archive.addfile(member)
    return buffer.getvalue()


def install_handler(container: str, data: bytes, *, running: bool = False, inspect=None, install=None,
                    readback=None):
    """Answer the install sequence: inspect, tar install on stdin, and tar readback."""
    captured: dict[str, bytes | None] = {"tar": None}

    def handler(args, stdin):
        if args[0] == "inspect":
            return inspect if inspect is not None else (0, inspect_output(container, running=running))
        if args[0] == "cp" and args[1] == "-":
            captured["tar"] = stdin
            return install if install is not None else (0, "")
        if args[0] == "cp" and args[1] == f"{container}:/.keel-runtime":
            return readback if readback is not None else (0, helper_readback(data))
        raise AssertionError(f"unexpected docker call {args!r}")

    return handler, captured


async def test_install_helper_installs_a_root_owned_helper_into_the_stopped_id(backend, artifact_directory):
    container = exact_id()
    artifact = make_artifact(artifact_directory)
    handler, captured = install_handler(container, artifact.path.read_bytes())
    fake = attach(backend, handler)

    outcome = await backend.install_helper(make_attempt("install", container_id=container), artifact)

    assert outcome.state == "succeeded"
    assert fake.commands() == [
        ["inspect", "--type", "container", "--format",
         "{{.Id}}|{{.Name}}|{{.State.Running}}|{{json .Config.Labels}}", container],
        ["cp", "-", f"{container}:/"],
        ["cp", f"{container}:/.keel-runtime", "-"],
    ]
    with tarfile.open(fileobj=io.BytesIO(captured["tar"]), mode="r:") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == [".keel-runtime", ".keel-runtime/keel-supervisor"]
    directory, helper = members
    assert directory.isdir() and stat.S_IMODE(directory.mode) == 0o755
    assert (directory.uid, directory.gid) == (0, 0)
    assert helper.isfile() and stat.S_IMODE(helper.mode) == 0o755 and (helper.uid, helper.gid) == (0, 0)
    assert helper.size == len(artifact.path.read_bytes())
    assert str(artifact.path) not in json.dumps(fake.commands())  # the package path never reaches the daemon


async def test_install_helper_requires_a_stopped_container(backend, artifact_directory):
    container = exact_id()
    artifact = make_artifact(artifact_directory)
    handler, _ = install_handler(container, artifact.path.read_bytes(), running=True)
    fake = attach(backend, handler)

    outcome = await backend.install_helper(make_attempt("install", container_id=container), artifact)

    assert outcome.state == "definite_failure" and "stopped" in (outcome.diagnostic or "")
    assert not any(args[0] == "cp" for args in fake.commands())


@pytest.mark.parametrize("inspect,state", [
    (_DockerResult(None, "", timed_out=True), "uncertain"),
    (_DockerResult(-9, "killed"), "uncertain"),
    ((0, "malformed\n"), "uncertain"),
    ((1, "error during connect\n"), "uncertain"),
    ((1, "Error: No such container: " + "0" * 64 + "\n"), "definite_failure"),
])
async def test_install_helper_refuses_without_a_definite_stopped_observation(backend, artifact_directory, inspect,
                                                                             state):
    container = exact_id()
    artifact = make_artifact(artifact_directory)
    handler, _ = install_handler(container, artifact.path.read_bytes(), inspect=inspect)
    fake = attach(backend, handler)

    outcome = await backend.install_helper(make_attempt("install", container_id=container), artifact)

    assert outcome.state == state
    assert not any(args[0] == "cp" for args in fake.commands())


@pytest.mark.parametrize("arch,protocol", [("arm64", 1), ("amd64", 2)])
async def test_install_helper_rejects_a_mismatched_artifact(backend, artifact_directory, arch, protocol):
    artifact = make_artifact(artifact_directory, arch=arch, protocol=protocol)
    fake = attach(backend, lambda args, stdin: (0, ""))
    outcome = await backend.install_helper(make_attempt("install", container_id=exact_id()), artifact)
    assert outcome.state == "definite_failure" and outcome.uncertain is False
    assert fake.calls == []


async def test_install_helper_compares_against_the_daemon_architecture(backend, artifact_directory):
    backend._identity = (DAEMON, "arm64")
    artifact = make_artifact(artifact_directory, arch="arm64")
    container = exact_id()
    handler, captured = install_handler(container, artifact.path.read_bytes())
    attach(backend, handler)
    outcome = await backend.install_helper(make_attempt("install", container_id=container), artifact)
    assert outcome.state == "succeeded"
    assert captured["tar"] is not None


async def test_install_helper_rejects_a_digest_mismatch(backend, artifact_directory):
    artifact = make_artifact(artifact_directory)
    (artifact_directory / artifact.path.name).write_bytes(b"tampered")
    attach(backend, lambda args, stdin: (0, ""))
    outcome = await backend.install_helper(make_attempt("install", container_id=exact_id()), artifact)
    assert outcome.state == "definite_failure" and "digest" in (outcome.diagnostic or "").lower()


async def test_install_helper_rejects_a_missing_or_non_executable_file(backend, artifact_directory):
    artifact = make_artifact(artifact_directory)
    artifact.path.unlink()
    attach(backend, lambda args, stdin: (0, ""))
    outcome = await backend.install_helper(make_attempt("install", container_id=exact_id()), artifact)
    assert outcome.state == "definite_failure"
    artifact.path.write_bytes(b"#!/bin/sh\n")
    artifact.path.chmod(0o644)
    outcome = await backend.install_helper(make_attempt("install", container_id=exact_id()), artifact)
    assert outcome.state == "definite_failure"


async def test_install_helper_refuses_a_path_outside_the_package_artifact(backend, tmp_path, artifact_directory):
    outside = tmp_path / "elsewhere"
    outside.write_bytes(b"\x7fELF outside")
    outside.chmod(0o755)
    artifact = HelperArtifact(outside, hashlib.sha256(outside.read_bytes()).hexdigest(), 1, "amd64")
    attach(backend, lambda args, stdin: (0, ""))
    with pytest.raises(ValueError, match="package"):
        await backend.install_helper(make_attempt("install", container_id=exact_id()), artifact)


@pytest.mark.parametrize("result,state", [
    ((1, "Error response from daemon: no space left on device\n"), "definite_failure"),
    ((125, "unknown flag: --something\n"), "uncertain"),
    (_DockerResult(None, "", timed_out=True), "uncertain"),
    (_DockerResult(-9, "killed"), "uncertain"),
])
async def test_install_helper_classifies_install_results(backend, artifact_directory, result, state):
    container = exact_id()
    artifact = make_artifact(artifact_directory)
    handler, _ = install_handler(container, artifact.path.read_bytes(), install=result)
    fake = attach(backend, handler)
    outcome = await backend.install_helper(make_attempt("install", container_id=container), artifact)
    assert outcome.state == state
    assert not any(args[0] == "cp" and args[1].startswith(f"{container}:") for args in fake.commands())


@pytest.mark.parametrize("readback,state", [
    (_DockerResult(None, "", timed_out=True), "uncertain"),
    (_DockerResult(-9, "killed"), "uncertain"),
    (_DockerBinaryResult(1, b"", "error during connect\n"), "uncertain"),
    (_DockerBinaryResult(1, b"", "Error response from daemon: permission denied\n"), "definite_failure"),
    (_DockerBinaryResult(1, b"", "Error: No such container: " + "0" * 64 + "\n"), "definite_failure"),
])
async def test_install_helper_classifies_readback_failures(backend, artifact_directory, readback, state):
    container = exact_id()
    artifact = make_artifact(artifact_directory)
    handler, _ = install_handler(container, artifact.path.read_bytes(), readback=readback)
    attach(backend, handler)
    outcome = await backend.install_helper(make_attempt("install", container_id=container), artifact)
    assert outcome.state == state


@pytest.mark.parametrize("changes", [
    {"directory_mode": 0o700}, {"directory_uid": 1000}, {"directory_gid": 1000},
    {"mode": 0o644}, {"uid": 1000}, {"gid": 1000}, {"body": b"tampered"},
])
async def test_install_helper_rejects_a_readback_that_is_not_the_root_owned_helper(backend, artifact_directory,
                                                                                   changes):
    container = exact_id()
    artifact = make_artifact(artifact_directory)
    data = artifact.path.read_bytes()
    handler, _ = install_handler(container, data, readback=(0, helper_readback(data, **changes)))
    attach(backend, handler)

    outcome = await backend.install_helper(make_attempt("install", container_id=container), artifact)

    assert outcome.state == "definite_failure" and outcome.uncertain is False


async def test_install_helper_rejects_extra_content_in_the_reserved_directory(backend, artifact_directory):
    container = exact_id()
    artifact = make_artifact(artifact_directory)
    data = artifact.path.read_bytes()
    extra = tarfile.TarInfo(".keel-runtime/extra")
    extra.mode = 0o644
    handler, _ = install_handler(container, data, readback=(0, helper_readback(data, extra=[extra])))
    attach(backend, handler)

    outcome = await backend.install_helper(make_attempt("install", container_id=container), artifact)

    assert outcome.state == "definite_failure"


async def test_install_helper_rejects_an_unreadable_readback(backend, artifact_directory):
    container = exact_id()
    artifact = make_artifact(artifact_directory)
    handler, _ = install_handler(container, artifact.path.read_bytes(), readback=(0, b"not a tar archive"))
    attach(backend, handler)

    outcome = await backend.install_helper(make_attempt("install", container_id=container), artifact)

    assert outcome.state == "definite_failure"


# ---------------------------------------------------------------------------
# start_id and remove_id
# ---------------------------------------------------------------------------

async def test_start_id_starts_the_exact_id_and_confirms_running(backend):
    container = exact_id()
    calls = []

    def handler(args, stdin):
        calls.append(args)
        if args[0] == "start":
            return (0, container + "\n")
        if args[0] == "inspect":
            return (0, inspect_output(container, running=True))
        raise AssertionError(args)

    attach(backend, handler)
    outcome = await backend.start_id(make_attempt("start", container_id=container))

    assert outcome.state == "succeeded"
    assert outcome.observation.running is True
    assert calls[0] == ["start", container]
    assert calls[1][-1] == container
    assert all(backend.container_name("p1") not in args for args in calls)


async def test_start_id_requires_a_running_target(backend):
    container = exact_id()

    def handler(args, stdin):
        if args[0] == "start":
            return (0, container + "\n")
        return (0, inspect_output(container, running=False))

    attach(backend, handler)
    outcome = await backend.start_id(make_attempt("start", container_id=container))
    assert outcome.state == "definite_failure" and outcome.uncertain is False


@pytest.mark.parametrize("result,state", [
    ((1, f"Error response from daemon: No such container"),
     "definite_failure"),
    (_DockerResult(None, "", timed_out=True), "uncertain"),
    (_DockerResult(-9, "killed"), "uncertain"),
    ((125, "unexpected daemon response\n"), "uncertain"),
])
async def test_start_id_classifies_dispatch_results(backend, result, state):
    attach(backend, lambda args, stdin: result)
    outcome = await backend.start_id(make_attempt("start", container_id=exact_id()))
    assert outcome.state == state


async def test_remove_id_removes_the_exact_id_and_confirms_absence(backend):
    container = exact_id()
    calls = []

    def handler(args, stdin):
        calls.append(args)
        if args[0] == "rm":
            return (0, container + "\n")
        return (1, f"Error: No such object: {container}\n")

    attach(backend, handler)
    outcome = await backend.remove_id(make_attempt("remove", container_id=container))

    assert outcome.state == "succeeded" and outcome.uncertain is False
    assert calls[0] == ["rm", "-f", container]
    assert calls[1][-1] == container
    assert all(backend.container_name("p1") not in args for args in calls)


@pytest.mark.parametrize("result", [
    _DockerResult(None, "", timed_out=True),
    _DockerResult(-9, "killed"),
    (1, "Error response from daemon: permission denied"),
])
async def test_remove_id_is_uncertain_when_rm_is_not_definite(backend, result):
    attach(backend, lambda args, stdin: result)
    outcome = await backend.remove_id(make_attempt("remove", container_id=exact_id()))
    assert outcome.uncertain is True


async def test_remove_id_is_uncertain_when_absence_cannot_be_confirmed(backend):
    container = exact_id()

    def handler(args, stdin):
        if args[0] == "rm":
            return (0, "")
        return (1, "error during connect")

    attach(backend, handler)
    outcome = await backend.remove_id(make_attempt("remove", container_id=container))
    assert outcome.uncertain is True


async def test_remove_id_reports_a_definite_failure_when_the_id_still_exists(backend):
    container = exact_id()

    def handler(args, stdin):
        if args[0] == "rm":
            return (0, "")
        return (0, inspect_output(container, running=True))

    attach(backend, handler)
    outcome = await backend.remove_id(make_attempt("remove", container_id=container))
    assert outcome.state == "definite_failure" and outcome.uncertain is False


async def test_remove_id_requires_a_recorded_exact_target(backend):
    fake = attach(backend, lambda args, stdin: (0, ""))
    with pytest.raises(ValueError):
        await backend.remove_id(make_attempt("remove"))
    assert fake.calls == []


# ---------------------------------------------------------------------------
# exec_bound and control_exec
# ---------------------------------------------------------------------------

class FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeStdout:
    def __init__(self, process: "FakeProcess") -> None:
        self.process = process

    async def read(self, size: int) -> bytes:
        await asyncio.sleep(0)
        if self.process.chunks:
            return self.process.chunks.pop(0)
        if self.process.final_code is not None:
            self.process.returncode = self.process.final_code
        return b""


class FakeProcess:
    def __init__(self, chunks: list[bytes] | None = None, final_code: int | None = 0) -> None:
        self.chunks = list(chunks or [])
        self.final_code = final_code
        self.returncode = None
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(self)
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9


def attach_spawn(backend: DockerSandbox, process: FakeProcess, *, env_capture: dict | None = None):
    spawned: list[list[str]] = []

    async def spawn(args, wants_stdin):
        spawned.append(list(args))
        if env_capture is not None and "--env-file" in args:
            env_capture["content"] = Path(args[args.index("--env-file") + 1]).read_text()
        return process

    backend._spawn = spawn
    return spawned


async def test_exec_bound_dispatches_once_by_exact_id_with_separate_transports(backend):
    ticket = make_ticket()
    secret = "exec-secret-" + uuid.uuid4().hex
    process = FakeProcess([b"payload output\n"], 0)
    env_capture = {}
    spawned = attach_spawn(backend, process, env_capture=env_capture)
    fake = attach(backend, lambda args, stdin: (0, receipt_json(ticket, leader_status=3))
                  if "receipt" in args else (0, ""))

    result, receipt = await backend.exec_bound(ticket, ["sh", "-c", "echo hi"], cwd="/w", timeout_s=5,
                                               env={"TOKEN": secret}, stdin=b"input")

    assert result.exit_code == 0 and result.output == "payload output\n"
    assert result.timed_out is False
    assert receipt == CleanupReceipt(ticket.exec_id, ticket.control_token, 3, True, True, None)
    dispatch = spawned[0]
    assert ticket.binding.container_id in dispatch
    assert ticket.binding.container_name not in dispatch
    helper = dispatch.index("/.keel-runtime/keel-supervisor")
    assert dispatch[helper + 1:helper + 3] == ["run", "--root"]
    assert dispatch[dispatch.index("--exec-id") + 1] == ticket.exec_id
    assert dispatch[dispatch.index("--control-token") + 1] == ticket.control_token
    assert dispatch[dispatch.index("--usage-token") + 1] == ticket.use.token
    assert dispatch[dispatch.index("--grace") + 1] == "3.0"
    assert dispatch[dispatch.index("--reap") + 1] == "5.0"
    assert dispatch[dispatch.index("-w") + 1] == "/w"
    assert dispatch[dispatch.index("-u") + 1] == "1000:1000"
    assert dispatch[-2:] == ["sh", "-c"] or dispatch[dispatch.index("--") + 1:] == ["sh", "-c", "echo hi"]
    assert secret not in dispatch and f"TOKEN={secret}" not in dispatch
    assert env_capture["content"] == f"TOKEN={secret}\n"
    assert process.stdin.data == b"input"
    # The receipt comes from a second, separate docker exec.
    receipt_calls = [args for args in fake.commands() if "receipt" in args]
    assert len(receipt_calls) == 1 and ticket.binding.container_id in receipt_calls[0]
    assert ticket.binding.container_name not in receipt_calls[0]
    assert "receipt" not in dispatch


async def test_exec_bound_ignores_a_forged_receipt_in_payload_stdout(backend):
    ticket = make_ticket()
    forged = receipt_json(ticket, leader_status=200)
    process = FakeProcess([forged.encode()], 0)
    attach_spawn(backend, process)
    attach(backend, lambda args, stdin: (5, "no receipt") if "receipt" in args else (0, ""))

    with pytest.raises(SandboxUncertain):
        await backend.exec_bound(ticket, ["echo"], cwd="/w", timeout_s=5)


async def test_exec_bound_uses_the_separate_receipt_not_a_forged_payload(backend):
    ticket = make_ticket()
    forged = receipt_json(ticket, leader_status=200)
    process = FakeProcess([forged.encode()], 0)
    attach_spawn(backend, process)
    attach(backend, lambda args, stdin: (0, receipt_json(ticket, leader_status=4))
           if "receipt" in args else (0, ""))

    _, receipt = await backend.exec_bound(ticket, ["echo"], cwd="/w", timeout_s=5)

    assert receipt.leader_status == 4


@pytest.mark.parametrize("result", [
    (5, "no receipt"),
    (1, "error during connect"),
    (0, "not a receipt"),
    (0, receipt_json(make_ticket(), leader_status=1)),
    (0, json.dumps({"version": 1}) + "\n"),
    (0, receipt_json(make_ticket())),  # mismatched binding: a fresh ticket here
])
async def test_exec_bound_treats_a_missing_malformed_or_mismatched_receipt_as_uncertain(backend, result):
    ticket = make_ticket()
    attach_spawn(backend, FakeProcess([b"ok\n"], 0))
    attach(backend, lambda args, stdin: result if "receipt" in args else (0, ""))
    with pytest.raises(SandboxUncertain):
        await backend.exec_bound(ticket, ["echo"], cwd="/w", timeout_s=5)


async def test_exec_bound_does_not_accept_an_uncertain_receipt(backend):
    ticket = make_ticket()
    attach_spawn(backend, FakeProcess([b"ok\n"], 0))
    attach(backend, lambda args, stdin: (0, receipt_json(ticket, descendants_complete=False,
                                                          uncertain_reason="descendants_unverified"))
           if "receipt" in args else (0, ""))
    with pytest.raises(SandboxUncertain):
        await backend.exec_bound(ticket, ["echo"], cwd="/w", timeout_s=5)


async def test_exec_bound_timeout_cancels_through_the_helper(backend):
    ticket = make_ticket()
    process = FakeProcess([], None)  # never exits until cancelled
    spawn_calls = attach_spawn(backend, process)
    control_calls = []

    def handler(args, stdin):
        if "cancel" in args:
            control_calls.append(args)
            process.returncode = -15
            return (0, "")
        if "receipt" in args:
            return (0, receipt_json(ticket, leader_status=143))
        return (0, "")

    attach(backend, handler)

    result, receipt = await backend.exec_bound(ticket, ["sleep", "30"], cwd="/w", timeout_s=0.05)

    assert result.timed_out is True and result.exit_code == TIMEOUT_EXIT_CODE
    cancel = next(args for args in control_calls if "cancel" in args)
    assert ticket.binding.container_id in cancel and ticket.binding.container_name not in cancel
    assert cancel[cancel.index("--usage-token") + 1] == ticket.use.token
    assert cancel[cancel.index("--control-token") + 1] == ticket.control_token
    assert receipt.leader_status == 143
    assert len(spawn_calls) == 1  # never retried


async def test_exec_bound_cancellation_cancels_through_the_helper_and_propagates(backend):
    ticket = make_ticket()
    process = FakeProcess([], None)
    attach_spawn(backend, process)
    cancelled = []

    def handler(args, stdin):
        if "cancel" in args:
            cancelled.append(args)
            process.returncode = -15
            return (0, "")
        return (0, "")

    attach(backend, handler)
    task = asyncio.create_task(backend.exec_bound(ticket, ["sleep", "30"], cwd="/w", timeout_s=30))
    await asyncio.sleep(0.06)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(cancelled) == 1 and ticket.binding.container_id in cancelled[0]


async def test_exec_bound_blocks_on_daemon_identity_drift(backend):
    backend._identity = ("another-daemon", "amd64")
    ticket = make_ticket()
    attach_spawn(backend, FakeProcess([b"x"], 0))
    attach(backend, lambda args, stdin: (0, receipt_json(ticket)))
    with pytest.raises(SandboxUncertain, match="daemon"):
        await backend.exec_bound(ticket, ["echo"], cwd="/w", timeout_s=5)


async def test_exec_bound_rejects_a_reusable_name_or_invalid_token(backend):
    attach_spawn(backend, FakeProcess([b"x"], 0))
    name_ticket = make_ticket(container_id=backend.container_name("p1"))
    with pytest.raises(ValueError):
        await backend.exec_bound(name_ticket, ["echo"], cwd="/w", timeout_s=5)
    invalid = ExecTicket("exec-1", UseHandle(make_key(), "u", "not-hex", "w1"),
                         SandboxBinding(1, token(), exact_id(), "keel-sbx-n", policy(), "d" * 16), "not-hex")
    with pytest.raises(ValueError):
        await backend.exec_bound(invalid, ["echo"], cwd="/w", timeout_s=5)


async def test_control_exec_cancel_sends_the_binding_then_reads_the_receipt(backend):
    ticket = make_ticket()
    fake = attach(backend, lambda args, stdin: (0, receipt_json(ticket, leader_status=7))
                  if "receipt" in args else (0, ""))

    receipt = await backend.control_exec(ticket, cancel=True)

    assert receipt.leader_status == 7
    cancel = next(args for args in fake.commands() if "cancel" in args)
    assert ticket.binding.container_id in cancel
    assert ticket.binding.container_name not in cancel
    assert cancel[cancel.index("--control-token") + 1] == ticket.control_token
    assert cancel[cancel.index("--usage-token") + 1] == ticket.use.token
    assert any("receipt" in args for args in fake.commands())


async def test_control_exec_accepts_a_terminal_receipt_returned_by_cancel(backend):
    ticket = make_ticket()
    fake = attach(backend, lambda args, stdin: (0, receipt_json(ticket, leader_status=9)))
    receipt = await backend.control_exec(ticket, cancel=True)
    assert receipt.leader_status == 9
    assert len(fake.commands()) == 1


async def test_control_exec_reads_the_receipt_command(backend):
    ticket = make_ticket()
    fake = attach(backend, lambda args, stdin: (0, receipt_json(ticket, leader_status=5)))
    receipt = await backend.control_exec(ticket, cancel=False)
    assert receipt.leader_status == 5
    assert len(fake.commands()) == 1 and "receipt" in fake.commands()[0]
    assert ticket.binding.container_name not in fake.commands()[0]


@pytest.mark.parametrize("result", [
    (4, "control binding rejected"),
    (5, "exec identity is not reserved"),
    (1, "container is not running"),
    (0, "not json"),
    (0, receipt_json(make_ticket())),
])
async def test_control_exec_never_treats_a_control_failure_as_success(backend, result):
    ticket = make_ticket()
    attach(backend, lambda args, stdin: result)
    with pytest.raises(SandboxUncertain):
        await backend.control_exec(ticket, cancel=True)


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------

async def test_inventory_reads_this_deployments_managed_containers(backend):
    first, second = exact_id(), exact_id()
    lines = [
        json.dumps({"ID": first, "Names": "/keel-sbx-a", "State": "running",
                    "Labels": f"keel.managed=1,keel.deployment={DEPLOYMENT},keel.policy=abc"}),
        json.dumps({"ID": second, "Names": "/keel-sbx-b", "State": "exited",
                    "Labels": f"keel.managed=1,keel.deployment={DEPLOYMENT}"}),
    ]
    fake = attach(backend, lambda args, stdin: (0, "\n".join(lines) + "\n"))

    observations = await backend.inventory()

    assert [(o.container_id, o.container_name, o.running) for o in observations] == [
        (first, "keel-sbx-a", True), (second, "keel-sbx-b", False)]
    assert {o.daemon_id for o in observations} == {DAEMON}
    assert observations[0].policy_digest == "abc" and observations[1].policy_digest is None
    command = fake.commands()[0]
    assert command[:2] == ["ps", "-a"] and "--no-trunc" in command
    assert "label=keel.managed=1" in command and f"label=keel.deployment={DEPLOYMENT}" in command


async def test_inventory_fails_closed_on_docker_or_output_errors(backend):
    attach(backend, lambda args, stdin: (1, "error during connect"))
    with pytest.raises(SandboxError):
        await backend.inventory()
    attach(backend, lambda args, stdin: (0, "not json\n"))
    with pytest.raises(SandboxError):
        await backend.inventory()
    attach(backend, lambda args, stdin: (0, json.dumps({"ID": "short", "Names": "/x", "State": "running"}) + "\n"))
    with pytest.raises(SandboxError):
        await backend.inventory()


# ---------------------------------------------------------------------------
# Env files may not smuggle extra assignments into `--env-file`.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", [
    {"TOKEN": "value\nINJECTED=1"},
    {"TOKEN": "value\r"},
    {"TOKEN": "value\0"},
    {"TO=KEN": "value"},
    {"": "value"},
    {"TOKEN": 1},
])
def test_env_file_rejects_values_that_cannot_be_written(env):
    with pytest.raises(SandboxError, match="env file"):
        with _env_file(env):
            pass


# ---------------------------------------------------------------------------
# No primitive may resurrect the legacy ensure/replay behaviour.
# ---------------------------------------------------------------------------

async def test_exact_id_primitives_never_call_ensure(backend, artifact_directory):
    async def ensure(*args, **kwargs):
        raise AssertionError("an exact-ID primitive must never call ensure")

    backend.ensure = ensure
    container = exact_id()
    ticket = make_ticket(container_id=container)
    artifact = make_artifact(artifact_directory)

    def handler(args, stdin):
        if args[0] == "image":
            return image_volumes()
        if args[0] == "inspect":
            return (1, f"Error: No such object: {container}\n")
        if "receipt" in args:
            return (0, receipt_json(ticket))
        return (0, container + "\n")

    attach(backend, handler)
    attach_spawn(backend, FakeProcess([b"ok"], 0))

    await backend.inspect_id(container)
    await backend.create_generation(make_attempt("create"), policy())
    await backend.install_helper(make_attempt("install", container_id=container), artifact)
    await backend.start_id(make_attempt("start", container_id=container))
    await backend.remove_id(make_attempt("remove", container_id=container))
    await backend.exec_bound(ticket, ["echo"], cwd="/w", timeout_s=5)
    await backend.control_exec(ticket, cancel=False)


# ---------------------------------------------------------------------------
# Opt-in real Docker probes.
# ---------------------------------------------------------------------------

needs_docker = pytest.mark.skipif(os.environ.get("KEEL_TEST_DOCKER") != "1", reason="set KEEL_TEST_DOCKER=1")


def _built_artifact(tmp_path, arch: str, monkeypatch) -> HelperArtifact:
    import subprocess
    import sys

    directory = tmp_path / "bin"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_supervisor.py"), "--arch", arch,
         "--output", str(directory)], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        pytest.fail(f"helper build failed: {result.stdout}\n{result.stderr}")
    select = lambda: directory  # noqa: E731 - a fixture path, not behaviour
    monkeypatch.setattr(docker_module, "artifact_directory", select)
    monkeypatch.setattr(runtime_module, "artifact_directory", select)
    return runtime_module.helper_artifact(arch)


def _key_for(backend: DockerSandbox, daemon_id: str) -> SandboxKey:
    return make_key(deployment=backend.deployment, daemon_id=daemon_id)


async def _real_backend() -> DockerSandbox:
    deployment = f"test-{uuid.uuid4().hex[:8]}"
    backend = DockerSandbox(deployment=deployment, kill_grace_s=1.0, receipt_timeout_s=30.0)
    return backend


async def _prepare(backend: DockerSandbox, artifact: HelperArtifact, *, image: str, user: str | None,
                   created: list[str]):
    daemon_id, _ = await backend.daemon_identity()
    policy_value = parse_policy({"image": image, "workdir": "/work", "user": user}, "/")
    attempt = make_attempt("create", intended_name=f"keel-sbx-test-{uuid.uuid4().hex[:12]}",
                           key=_key_for(backend, daemon_id))
    created_outcome = await backend.create_generation(attempt, policy_value)
    assert created_outcome.state == "succeeded", created_outcome
    container = created_outcome.observation.container_id
    created.append(container)  # remembered before the next step, so a failure still cleans up
    installed = await backend.install_helper(
        make_attempt("install", container_id=container, key=_key_for(backend, daemon_id)), artifact)
    assert installed.state == "succeeded", installed
    started = await backend.start_id(
        make_attempt("start", container_id=container, key=_key_for(backend, daemon_id)))
    assert started.state == "succeeded", started
    return container, policy_value


@needs_docker
@pytest.mark.parametrize("image,user", [
    ("python:3.12-slim", None),
    ("python:3.12-slim", "1000:1000"),
    ("debian:bookworm-slim", None),
    ("debian:bookworm-slim", "1000:1000"),
])
async def test_real_docker_install_and_exec_bound(tmp_path, image, user, monkeypatch):
    backend = await _real_backend()
    daemon_id, arch = await backend.daemon_identity()
    artifact = _built_artifact(tmp_path, arch, monkeypatch)
    created: list[str] = []
    try:
        container, selected = await _prepare(backend, artifact, image=image, user=user, created=created)
        ticket = ExecTicket("exec-" + uuid.uuid4().hex[:8],
                            UseHandle(_key_for(backend, daemon_id), "u1", token(), "w1"),
                            SandboxBinding(3, token(), container, "keel-sbx-test", selected, artifact.sha256),
                            token())
        result, receipt = await backend.exec_bound(ticket, ["sh", "-c", "printf 'ok %s\\n' \"$TOKEN\"; echo err >&2"],
                                                   cwd="/work", timeout_s=30, env={"TOKEN": "value"})
        assert result.exit_code == 0, result.output
        assert "ok value" in result.output
        assert receipt.control_closed and receipt.descendants_complete and receipt.uncertain_reason is None
        # A timeout is cancelled through the helper and still yields a terminal receipt.
        timed = ExecTicket("exec-" + uuid.uuid4().hex[:8],
                           UseHandle(_key_for(backend, daemon_id), "u2", token(), "w1"),
                           SandboxBinding(3, token(), container, "keel-sbx-test", selected, artifact.sha256),
                           token())
        short, timeout_receipt = await backend.exec_bound(timed, ["sh", "-c", "sleep 30"], cwd="/work",
                                                         timeout_s=1)
        assert short.timed_out and timeout_receipt.control_closed
    finally:
        for container in created:
            removed = await backend.remove_id(make_attempt("remove", container_id=container,
                                                          key=_key_for(backend, daemon_id)))
            assert removed.state == "succeeded", removed
        assert await backend.inventory() == []
        await backend.aclose()
