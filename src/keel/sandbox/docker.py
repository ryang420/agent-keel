"""The coordinated Docker sandbox backend: exact-ID operations driven through the docker CLI.

There is no name-based lifecycle path. Every operation addresses a recorded full container ID (or a name only
while creating a new generation under a recorded attempt), and a payload is dispatched exactly once through the
installed in-container supervisor, which owns cancellation and proves termination with its terminal receipt.
Killing the docker client never signals the payload; a lost acknowledgement is reconciled against the ledger.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import posixpath
import re
import shutil
import stat
import tarfile
import tempfile
import time
from typing import Awaitable, Callable

from keel.sandbox.base import (
    TIMEOUT_EXIT_CODE, ExecResult, PolicyError, SandboxError, SandboxPolicy, collect, profile_mount_root,
    validate_mount_sources,
)
from keel.sandbox.records import (
    ContainerObservation, DockerOutcome, ExecTicket, LifecycleAttempt, SandboxKey, SandboxUncertain,
)
from keel.sandbox.runtime import (
    HELPER_PROTOCOL_VERSION, HelperArtifact, artifact_directory, decode_receipt, normalize_architecture,
)

log = logging.getLogger(__name__)

# Coordinated execution: the installed helper, its private runtime root, and the resolved parent path.
_HELPER_PATH = "/.keel-runtime/keel-supervisor"
_RUNTIME_ROOT = "/tmp/.keel-exec"
_RESERVED_RUNTIME = "/.keel-runtime"
_EXACT_ID = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_EXEC_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NOT_FOUND = ("no such container", "no such object")
_NO_SUCH_IMAGE = "no such image"
_DAEMON_ERROR = "error response from daemon"
_MAX_HELPER_BYTES = 64 * 1024 * 1024
_MAX_TAR_BYTES = _MAX_HELPER_BYTES + 1024 * 1024  # the helper plus tar headers and the directory entry


@dataclass(frozen=True)
class _DockerResult:
    """A docker CLI result, including why no exit status is available (timeout, missing CLI, signal).

    `output` is stdout only: identities and structured results are parsed from it. Docker prints daemon warnings
    (platform mismatch, missing swap accounting) on stderr even when a command succeeds, so stderr is kept apart in
    `diagnostic` and only matched for daemon error messages or reported, bounded, as a diagnostic.
    """

    code: int | None
    output: str
    timed_out: bool = False
    diagnostic: str = ""

    @property
    def text(self) -> str:
        """stdout and stderr together, for daemon-message matching and diagnostics; never parsed as a result."""
        return "\n".join(part for part in (self.output.strip(), self.diagnostic.strip()) if part)


@dataclass(frozen=True)
class _DockerBinaryResult:
    """A docker CLI result whose stdout is a binary stream (a tar archive); stderr is kept separate."""

    code: int | None
    output: bytes
    diagnostic: str
    timed_out: bool = False


def _not_found(output: str) -> bool:
    """Only the daemon's own missing-container messages prove absence, never an arbitrary error."""
    lowered = output.lower()
    return any(marker in lowered for marker in _NOT_FOUND)


def _no_such_image(output: str) -> bool:
    """The daemon's own missing-image message proves the image cannot be inspected."""
    return _NO_SUCH_IMAGE in output.lower()


def _definite_daemon_error(output: str) -> bool:
    """A normal daemon rejection is definite: the daemon answered, the request did not reach the effect."""
    return _DAEMON_ERROR in output.lower() or output.lstrip().startswith("Error:")


def _reserved_runtime_overlap(path: str) -> bool:
    normalized = posixpath.normpath(path)
    return (normalized == _RESERVED_RUNTIME or normalized.startswith(_RESERVED_RUNTIME + "/")
            or _RESERVED_RUNTIME.startswith(normalized.rstrip("/") + "/"))


_HELPER_NAME = "keel-supervisor"
_HELPER_ENTRY = f"{_RESERVED_RUNTIME.lstrip('/')}/{_HELPER_NAME}"


def _helper_tar(data: bytes) -> bytes:
    """A tar that installs `/.keel-runtime` and the helper root-owned and mode 0755.

    `docker cp` preserves the archive's metadata, and copying a host directory would also copy the host copy's owner and
    mode (a 0700 directory the sandbox user cannot traverse). Building the tar here fixes the metadata explicitly, so
    nothing about the host leaks into the container.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        directory = tarfile.TarInfo(_RESERVED_RUNTIME.lstrip("/"))
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.uid = directory.gid = 0
        directory.uname = directory.gname = "root"
        directory.mtime = 0
        archive.addfile(directory)
        helper = tarfile.TarInfo(_HELPER_ENTRY)
        helper.size = len(data)
        helper.mode = 0o755
        helper.uid = helper.gid = 0
        helper.uname = helper.gname = "root"
        helper.mtime = 0
        archive.addfile(helper, io.BytesIO(data))
    return buffer.getvalue()


def _verify_helper_tar(data: bytes, size: int, digest: str) -> str | None:
    """Confirm a readback holds exactly the root-owned 0755 helper; return a bounded diagnostic otherwise."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = archive.getmembers()
            if [member.name for member in members] != [_RESERVED_RUNTIME.lstrip("/"), _HELPER_ENTRY]:
                return "the helper readback does not hold exactly the reserved directory and helper"
            directory, helper = members
            if (not directory.isdir() or stat.S_IMODE(directory.mode) != 0o755
                    or directory.uid != 0 or directory.gid != 0):
                return "the reserved directory is not a root-owned 0755 directory"
            if (not helper.isfile() or stat.S_IMODE(helper.mode) != 0o755
                    or helper.uid != 0 or helper.gid != 0 or helper.size != size):
                return "the helper is not a root-owned 0755 file of the recorded size"
            stream = archive.extractfile(helper)
            if stream is None:
                return "the helper readback does not hold a readable file"
            actual = hashlib.sha256()
            total = 0
            while chunk := stream.read(1024 * 1024):
                total += len(chunk)
                if total > size:
                    return "the installed helper is larger than the recorded artifact"
                actual.update(chunk)
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return "the helper readback is not a readable tar archive"
    if total != size or actual.hexdigest() != digest:
        return "the installed helper does not match the recorded digest"
    return None


def _parse_labels(value: object) -> tuple[tuple[str, str], ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, str):
        raise ValueError("labels are not text")
    labels = []
    for part in value.split(","):
        if not part:
            continue
        key, separator, item = part.partition("=")
        if not separator or not key:
            raise ValueError("a label is malformed")
        labels.append((key, item))
    return tuple(sorted(labels))


def _parse_observation(output: str, daemon_id: str, expected_id: str) -> ContainerObservation | None:
    parts = output.splitlines()[0].split("|", 3) if output.splitlines() else []
    if len(parts) != 4:
        return None
    container_id, name, running, labels_text = parts
    name = name.lstrip("/")
    if container_id != expected_id or not name or running not in ("true", "false"):
        return None
    try:
        raw_labels = json.loads(labels_text) if labels_text else {}
    except ValueError:
        return None
    if raw_labels is None:
        raw_labels = {}
    if not isinstance(raw_labels, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in raw_labels.items()):
        return None
    labels = tuple(sorted(raw_labels.items()))
    return ContainerObservation(daemon_id, container_id, name, labels, running == "true",
                                raw_labels.get("keel.policy"))



@contextmanager
def _env_file(env):
    """Environment values for a docker command as `--env-file` arguments. The file lives in a private (0700)
    directory, is readable by the owner only, and is removed when the command is done: values never reach argv, and
    they do not change the docker client's own environment (which a policy PATH or DOCKER_HOST would)."""
    if not env:
        yield []
        return
    for key, value in dict(env).items():
        if (not isinstance(key, str) or not isinstance(value, str) or not key
                or any(character in key for character in "=\0\n\r")
                or any(character in value for character in "\0\n\r")):
            # A newline would inject an extra assignment into the env file; never echo the value.
            raise SandboxError("a sandbox environment key or value cannot be written to an env file")
    directory = tempfile.mkdtemp(prefix="keel-env-")
    try:
        path = os.path.join(directory, "env")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for key, value in dict(env).items():
                handle.write(f"{key}={value}\n")
        yield ["--env-file", path]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class DockerSandbox:
    def __init__(self, *, docker: str = "docker", deployment: str = "keel",
                 kill_grace_s: float = 3.0, mount_root: str | None = None,
                 reap_seconds: float = 5.0, receipt_timeout_s: float | None = None) -> None:
        self.docker = docker
        self.deployment = deployment
        self.kill_grace_s = kill_grace_s
        self.reap_seconds = reap_seconds
        self.receipt_timeout_s = receipt_timeout_s
        self.mount_root = mount_root
        self._architecture: str | None = None  # the daemon's normalized architecture; its ID is never cached
        self._identity_lock = asyncio.Lock()

    def container_name(self, profile_id: str) -> str:
        return "keel-sbx-" + hashlib.sha256(f"{self.deployment}\0{profile_id}".encode()).hexdigest()[:12]

    def _hardening_args(self, policy: SandboxPolicy, env_file: str | None) -> list[str]:
        # Hardening mirrors the Hermes docker backend: no capabilities beyond file ownership and access, no
        # privilege gain, and writable scratch space on tmpfs (nosuid; /tmp stays executable for scripts).
        args = ["--runtime", policy.runtime, "--workdir", policy.workdir,
                "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--cap-add", "CHOWN", "--cap-add", "FOWNER",
                "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:rw,nosuid,size=512m", "--tmpfs", "/var/tmp:rw,nosuid", "--tmpfs", "/run:rw,nosuid"]
        if policy.network:
            args += ["--network", policy.network]
        if policy.user:
            args += ["--user", policy.user]
        if policy.cpus:
            args += ["--cpus", str(policy.cpus)]
        if policy.memory_mb:
            args += ["--memory", f"{policy.memory_mb}m"]
        if policy.pids_limit:
            args += ["--pids-limit", str(policy.pids_limit)]
        for mount in policy.mounts:
            args += ["--mount", f"type=bind,source={os.path.realpath(mount.source)},target={mount.target}"
                     + (",readonly" if mount.read_only else "")]
        if policy.env and env_file:
            args += ["--env-file", env_file]  # the values are in the file, never on the command line
        return args

    def _container_args(self, name: str, policy: SandboxPolicy, labels: list[tuple[str, str]],
                        env_file: str | None) -> list[str]:
        args = ["--init", "--name", name]
        for key, value in labels:
            args += ["--label", f"{key}={value}"]
        return [*args, *self._hardening_args(policy, env_file), policy.image, "sleep", "infinity"]

    async def _docker_raw(self, *args: str, stdin: bytes | None = None,
                          timeout: float = 60.0) -> _DockerResult:
        """Run one docker command and retain why no exit status may exist."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            return _DockerResult(None, "The docker CLI is not available.")
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except (TimeoutError, ProcessLookupError):
                pass
            return _DockerResult(None, f"docker {args[0]} did not answer within {timeout:.0f}s.", timed_out=True)
        return _DockerResult(proc.returncode, out.decode("utf-8", errors="replace"),
                             diagnostic=err.decode("utf-8", errors="replace")[-4096:])

    async def _docker_bytes(self, *args: str, stdin: bytes | None = None,
                            timeout: float = 60.0) -> _DockerBinaryResult:
        """Run one docker command whose stdout is binary (a tar archive), bounding what is read into memory."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            return _DockerBinaryResult(None, b"", "The docker CLI is not available.")
        try:
            out, err = await asyncio.wait_for(self._pump(proc, stdin), timeout)
        except OverflowError:
            await self._kill(proc)
            return _DockerBinaryResult(None, b"", "the docker output exceeded the read limit")
        except TimeoutError:
            await self._kill(proc)
            return _DockerBinaryResult(None, b"", f"docker {args[0]} did not answer within {timeout:.0f}s.",
                                       timed_out=True)
        return _DockerBinaryResult(proc.returncode, out, err.decode("utf-8", errors="replace"))

    @staticmethod
    async def _pump(proc: asyncio.subprocess.Process, stdin: bytes | None) -> tuple[bytes, bytes]:
        async def read(stream: asyncio.StreamReader | None, cap: int) -> bytes:
            assert stream is not None
            chunks: list[bytes] = []
            total = 0
            while chunk := await stream.read(65536):
                total += len(chunk)
                if total > cap:
                    raise OverflowError
                chunks.append(chunk)
            return b"".join(chunks)

        async def feed() -> None:
            assert proc.stdin is not None and stdin is not None
            try:
                proc.stdin.write(stdin)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            proc.stdin.close()

        # Feed stdin while draining both pipes: writing a large tar before reading stdout/stderr could deadlock.
        tasks = [read(proc.stdout, _MAX_TAR_BYTES), read(proc.stderr, 64 * 1024)]
        if stdin is not None:
            tasks.append(feed())
        results = await asyncio.gather(*tasks)
        return results[0], results[1]

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except (TimeoutError, ProcessLookupError):
            pass

    # --- Exact-ID operations (private to the lifecycle coordinator) --------------------------------------------
    #
    # These methods never create a generation implicitly and never retry a payload. They address only recorded full
    # container IDs; a definite daemon rejection is separated from a timeout, killed client, transport loss or
    # malformed output.

    @staticmethod
    def _exact_id(container_id: str | None) -> str:
        if not isinstance(container_id, str) or not _EXACT_ID.fullmatch(container_id):
            raise ValueError("a full 64-character lowercase container ID is required")
        return container_id

    async def daemon_identity(self) -> tuple[str, str]:
        """The daemon ID, read afresh on every call, and the normalized architecture, cached after the first read.

        The ID is evidence for ownership and absence decisions (a replaced daemon behind the same socket must never
        make a recorded container look absent), so it is never cached; a read failure raises and callers treat it
        as uncertain.
        """
        info = await self._docker_raw("info", "--format", "{{.ID}}")
        daemon_id = info.output.strip()
        if (info.timed_out or info.code != 0 or not daemon_id or len(daemon_id) > 128
                or any(character.isspace() for character in daemon_id)):
            raise SandboxError("could not read the Docker daemon identity: " + info.text[:200])
        if self._architecture is None:
            async with self._identity_lock:
                if self._architecture is None:
                    version = await self._docker_raw("version", "--format", "{{.Server.Arch}}")
                    if version.timed_out or version.code != 0:
                        raise SandboxError(
                            "could not read the Docker daemon architecture: " + version.text[:200])
                    self._architecture = normalize_architecture(version.output.strip())
        return daemon_id, self._architecture

    async def _recorded_key_diagnostic(self, key: SandboxKey) -> str | None:
        daemon_id, _ = await self.daemon_identity()
        if not key.daemon_id or key.daemon_id != daemon_id:
            return (f"the Docker daemon identity changed: recorded {key.daemon_id!r}, "
                    f"daemon {daemon_id!r}")
        if key.deployment != self.deployment:
            return (f"the recorded deployment {key.deployment!r} does not match this backend "
                    f"{self.deployment!r}")
        return None

    async def _attempt_block(self, attempt: LifecycleAttempt) -> DockerOutcome | None:
        """Refuse to dispatch an attempt whose recorded key names another daemon/deployment.

        A definite key drift or deployment mismatch is a failure. If the identity cannot be read, the record may still
        match: the dispatch is not made, and the outcome is uncertain so recovery reconciles it."""
        try:
            diagnostic = await self._recorded_key_diagnostic(attempt.key)
        except SandboxError as exc:
            return DockerOutcome("uncertain", diagnostic=str(exc))
        if diagnostic is None:
            return None
        return DockerOutcome("definite_failure", diagnostic=diagnostic)

    async def _require_recorded_key(self, ticket: ExecTicket) -> None:
        try:
            diagnostic = await self._recorded_key_diagnostic(ticket.use.key)
        except SandboxError as exc:
            raise SandboxUncertain(str(exc)) from exc
        if diagnostic is not None:
            raise SandboxUncertain(diagnostic)

    async def inspect_id(self, container_id: str) -> DockerOutcome:
        """Inspect one exact container ID. Only the daemon's own missing-object message proves absence."""
        exact = self._exact_id(container_id)
        try:
            daemon_id, _ = await self.daemon_identity()
        except SandboxError as exc:
            return DockerOutcome("uncertain", diagnostic=str(exc))
        result = await self._docker_raw(
            "inspect", "--type", "container", "--format",
            "{{.Id}}|{{.Name}}|{{.State.Running}}|{{json .Config.Labels}}", exact)
        if result.timed_out or result.code is None or result.code < 0:
            return DockerOutcome("uncertain", diagnostic="the inspect did not produce a definite result")
        if result.code == 1 and _not_found(result.text):
            return DockerOutcome("definite_failure", diagnostic="the container does not exist")
        if result.code != 0:
            return DockerOutcome("uncertain", diagnostic=result.text[:2000])
        observation = _parse_observation(result.output, daemon_id, exact)
        if observation is None:
            return DockerOutcome("uncertain", diagnostic="the daemon returned malformed inspect output")
        return DockerOutcome("succeeded", observation=observation)

    async def inventory(self) -> list[ContainerObservation]:
        """This deployment's managed containers, including stopped ones, with exact IDs."""
        daemon_id, _ = await self.daemon_identity()
        result = await self._docker_raw(
            "ps", "-a", "--no-trunc", "--filter", "label=keel.managed=1",
            "--filter", f"label=keel.deployment={self.deployment}", "--format", "{{json .}}")
        if result.timed_out or result.code != 0:
            raise SandboxError("could not inventory managed sandbox containers: " + result.text[:300])
        observations = []
        for line in result.output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except ValueError as exc:
                raise SandboxError("the container inventory is malformed") from exc
            if not isinstance(value, dict):
                raise SandboxError("the container inventory is malformed")
            container_id = value.get("ID")
            if not isinstance(container_id, str) or not _EXACT_ID.fullmatch(container_id):
                raise SandboxError("the container inventory did not include exact container IDs")
            names = value.get("Names")
            name = names.split(",")[0].lstrip("/") if isinstance(names, str) else ""
            state = value.get("State")
            if not name or not isinstance(state, str):
                raise SandboxError("the container inventory is malformed")
            try:
                labels = _parse_labels(value.get("Labels"))
            except ValueError as exc:
                raise SandboxError("the container inventory is malformed") from exc
            observations.append(ContainerObservation(daemon_id, container_id, name, labels, state == "running",
                                                    dict(labels).get("keel.policy")))
        return observations

    def _generation_name(self, attempt: LifecycleAttempt) -> str:
        profile_name = self.container_name(attempt.key.profile_id)
        if attempt.intended_name is not None:
            if attempt.intended_name == profile_name or not _CONTAINER_NAME.fullmatch(attempt.intended_name):
                raise ValueError("a generation name must be a distinct, valid Docker container name")
            return attempt.intended_name
        if not _TOKEN.fullmatch(attempt.generation_token):
            raise ValueError("a generation token must be 32 lowercase hexadecimal characters")
        return f"{profile_name}-{attempt.generation_token[:16]}"

    def _generation_labels(self, attempt: LifecycleAttempt, policy: SandboxPolicy) -> list[tuple[str, str]]:
        return [("keel.managed", "1"), ("keel.deployment", self.deployment),
                ("keel.profile", attempt.key.profile_id), ("keel.policy", policy.digest()),
                ("keel.generation", str(attempt.generation)), ("keel.generation-token", attempt.generation_token),
                ("keel.create-operation", attempt.original_create_operation_id or attempt.id)]

    async def _image_volumes(self, image: str) -> tuple[set[str] | None, DockerOutcome | None]:
        """The image's declared volumes, or the outcome that prevents verifying them."""
        result = await self._docker_raw("image", "inspect", "--format", "{{json .Config.Volumes}}", image)
        if result.timed_out or result.code is None or result.code < 0:
            return None, DockerOutcome("uncertain",
                                       diagnostic="the image inspection did not produce a definite result")
        if result.code != 0:
            definite = _no_such_image(result.text) or _definite_daemon_error(result.text)
            return None, DockerOutcome("definite_failure" if definite else "uncertain",
                                       diagnostic=result.text[:2000])
        try:
            value = json.loads(result.output.strip() or "null")
        except ValueError:
            return None, DockerOutcome("uncertain", diagnostic="the daemon returned malformed image metadata")
        if value is None:
            return set(), None
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            return None, DockerOutcome("uncertain", diagnostic="the daemon returned malformed image metadata")
        return set(value), None

    async def create_generation(self, attempt: LifecycleAttempt, policy: SandboxPolicy) -> DockerOutcome:
        """Create (never start) one labeled generation container by its recorded identity."""
        if attempt.kind != "create":
            raise ValueError("create_generation requires a create attempt")
        if attempt.container_id is not None:
            raise ValueError("a create attempt must not already carry a container ID")
        name = self._generation_name(attempt)
        blocked = await self._attempt_block(attempt)
        if blocked is not None:
            return blocked
        for mount in policy.mounts:
            if _reserved_runtime_overlap(mount.target):
                return DockerOutcome(
                    "definite_failure",
                    diagnostic=f"sandbox.mounts[].target {mount.target} overlaps {_RESERVED_RUNTIME}")
        try:
            validate_mount_sources(policy.mounts, profile_mount_root(self.mount_root, attempt.key.profile_id))
        except PolicyError as exc:
            raise SandboxError(f"The sandbox mount policy is invalid: {exc}") from exc
        volumes, failure = await self._image_volumes(policy.image)
        if failure is not None:
            return failure
        for volume in volumes or ():
            if _reserved_runtime_overlap(volume):
                return DockerOutcome(
                    "definite_failure",
                    diagnostic=f"the image declares a volume overlapping {_RESERVED_RUNTIME}: {volume}")
        daemon_id, _ = await self.daemon_identity()
        labels = self._generation_labels(attempt, policy)
        with _env_file(policy.env) as env_args:
            created = await self._docker_raw(
                "create", *self._container_args(name, policy, labels, env_args[1] if env_args else None),
                timeout=300)
        if created.timed_out or created.code is None or created.code < 0:
            return DockerOutcome("uncertain", diagnostic="the create dispatch has an unknown outcome")
        if created.code != 0:
            state = "definite_failure" if _definite_daemon_error(created.text) else "uncertain"
            return DockerOutcome(state, diagnostic=created.text[:2000])
        container_id = created.output.strip()
        if not _EXACT_ID.fullmatch(container_id):
            return DockerOutcome("uncertain",
                                 diagnostic="the create succeeded but returned no exact container ID")
        observation = ContainerObservation(daemon_id, container_id, name, tuple(sorted(labels)), False,
                                          policy.digest())
        return DockerOutcome("succeeded", observation=observation)

    @staticmethod
    def _read_artifact(path: Path) -> bytes | None:
        try:
            status = path.stat()
            if not path.is_file() or not status.st_mode & 0o111 or status.st_size > _MAX_HELPER_BYTES:
                return None
            return path.read_bytes()
        except OSError:
            return None

    async def install_helper(self, attempt: LifecycleAttempt, artifact: HelperArtifact) -> DockerOutcome:
        """Install the verified package artifact into the stopped exact-ID container as a root-owned 0755 helper.

        The install is a tar archive built here, never a host directory copy: `docker cp` preserves the archive's
        metadata, so the reserved directory and the helper are explicitly root-owned 0755 regardless of the host user
        or umask. A readback then proves what the daemon stored before the generation may start.
        """
        if attempt.kind != "install":
            raise ValueError("install_helper requires an install attempt")
        container_id = self._exact_id(attempt.container_id)
        blocked = await self._attempt_block(attempt)
        if blocked is not None:
            return blocked
        _, architecture = await self.daemon_identity()
        if artifact.architecture != architecture:
            return DockerOutcome(
                "definite_failure",
                diagnostic=f"the helper is built for {artifact.architecture}, but the daemon is {architecture}")
        if artifact.protocol != HELPER_PROTOCOL_VERSION:
            return DockerOutcome("definite_failure",
                                 diagnostic=f"the helper protocol {artifact.protocol} is not supported")
        path = Path(artifact.path)
        if Path(os.path.realpath(path)).parent != Path(os.path.realpath(artifact_directory())):
            raise ValueError("the helper must be installed from the package artifact directory")
        data = self._read_artifact(path)
        if data is None:
            return DockerOutcome("definite_failure",
                                 diagnostic="the helper artifact is missing or not an executable regular file")
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            return DockerOutcome("definite_failure",
                                 diagnostic="the helper artifact does not match its recorded digest")
        inspected = await self.inspect_id(container_id)
        if inspected.uncertain:
            return inspected
        if inspected.observation is None:
            return DockerOutcome("definite_failure", diagnostic="the container does not exist")
        if inspected.observation.running:
            return DockerOutcome("definite_failure", observation=inspected.observation,
                                 diagnostic="a helper is only installed into a stopped container")
        installed = await self._docker_raw("cp", "-", f"{container_id}:/", stdin=_helper_tar(data), timeout=120)
        if installed.timed_out or installed.code is None or installed.code < 0:
            return DockerOutcome("uncertain", diagnostic="the helper install has an unknown outcome")
        if installed.code != 0:
            state = "definite_failure" if _definite_daemon_error(installed.text) else "uncertain"
            return DockerOutcome(state, diagnostic=installed.text[:2000])
        readback = await self._docker_bytes("cp", f"{container_id}:{_RESERVED_RUNTIME}", "-", timeout=120)
        if readback.timed_out or readback.code is None or readback.code < 0:
            return DockerOutcome("uncertain", diagnostic="the helper readback has an unknown outcome")
        if readback.code != 0:
            detail = readback.diagnostic.strip()
            definite = _not_found(detail) or _definite_daemon_error(detail)
            return DockerOutcome("definite_failure" if definite else "uncertain",
                                 diagnostic=detail or "the helper readback failed")
        problem = _verify_helper_tar(readback.output, len(data), artifact.sha256)
        if problem is not None:
            return DockerOutcome("definite_failure", diagnostic=problem)
        return DockerOutcome("succeeded")

    async def start_id(self, attempt: LifecycleAttempt) -> DockerOutcome:
        """Start the exact recorded ID only, then confirm it is running."""
        if attempt.kind != "start":
            raise ValueError("start_id requires a start attempt")
        container_id = self._exact_id(attempt.container_id)
        blocked = await self._attempt_block(attempt)
        if blocked is not None:
            return blocked
        started = await self._docker_raw("start", container_id, timeout=120)
        if started.timed_out or started.code is None or started.code < 0:
            return DockerOutcome("uncertain", diagnostic="the start dispatch has an unknown outcome")
        if started.code != 0 and _not_found(started.text):
            return DockerOutcome("definite_failure", diagnostic="the container does not exist")
        if started.code != 0 and not _definite_daemon_error(started.text):
            return DockerOutcome("uncertain", diagnostic=started.text[:2000])
        inspected = await self.inspect_id(container_id)
        if inspected.uncertain:
            return inspected
        if inspected.observation is None:
            return DockerOutcome("definite_failure", diagnostic="the container does not exist after start")
        if not inspected.observation.running:
            return DockerOutcome("definite_failure", observation=inspected.observation,
                                 diagnostic="the container is not running after start")
        return DockerOutcome("succeeded", observation=inspected.observation)

    async def remove_id(self, attempt: LifecycleAttempt) -> DockerOutcome:
        """Remove the exact recorded ID only, then confirm absence by exact-ID inspection."""
        if attempt.kind != "remove":
            raise ValueError("remove_id requires a remove attempt")
        container_id = self._exact_id(attempt.container_id)
        blocked = await self._attempt_block(attempt)
        if blocked is not None:
            return blocked
        removed = await self._docker_raw("rm", "-f", container_id, timeout=120)
        if removed.timed_out or removed.code is None or removed.code < 0:
            return DockerOutcome("uncertain", diagnostic="the remove dispatch has an unknown outcome")
        if removed.code != 0 and not _not_found(removed.text) and not _definite_daemon_error(removed.text):
            return DockerOutcome("uncertain", diagnostic=removed.text[:2000])
        inspected = await self.inspect_id(container_id)
        if inspected.uncertain:
            return inspected
        if inspected.observation is None:
            return DockerOutcome("succeeded", diagnostic="the container is absent")
        return DockerOutcome("definite_failure", observation=inspected.observation,
                             diagnostic="the container still exists after removal")

    def _control_args(self, ticket: ExecTicket, command: str, *, include_usage: bool) -> list[str]:
        args = ["exec"]
        if ticket.binding.policy.user:
            args += ["-u", ticket.binding.policy.user]
        args += [ticket.binding.container_id, _HELPER_PATH, command, "--root", _RUNTIME_ROOT,
                 "--control-token", ticket.control_token]
        if include_usage:
            args += ["--usage-token", ticket.use.token]
        return args

    @staticmethod
    def _decode_receipt(output: str, ticket: ExecTicket, *, require_success: bool) -> CleanupReceipt:
        try:
            receipt = decode_receipt(output.strip().encode("utf-8"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SandboxUncertain("the sandbox terminal receipt is malformed") from exc
        if receipt.exec_id != ticket.exec_id or receipt.control_token != ticket.control_token:
            raise SandboxUncertain("the sandbox terminal receipt does not match the dispatch")
        if require_success and (not receipt.control_closed or not receipt.descendants_complete
                                or receipt.uncertain_reason is not None):
            raise SandboxUncertain("the sandbox cleanup outcome is uncertain")
        return receipt

    async def _send_cancel(self, ticket: ExecTicket) -> None:
        """Ask the supervisor to cancel; never signal a numeric PID or process group."""
        result = await self._docker_raw(*self._control_args(ticket, "cancel", include_usage=True), timeout=15)
        if result.timed_out or result.code not in (0, 4, 5):
            log.warning("sandbox cancellation was not acknowledged: %s", result.text[:200])

    async def _await_receipt(self, ticket: ExecTicket, *, require_success: bool) -> CleanupReceipt:
        if self.receipt_timeout_s is None:
            timeout = max(0.0, self.kill_grace_s) + max(0.0, self.reap_seconds) + 2.0
        else:
            timeout = max(0.0, self.receipt_timeout_s)
        deadline = time.monotonic() + timeout
        while True:
            result = await self._docker_raw(*self._control_args(ticket, "receipt", include_usage=False),
                                            timeout=15)
            if result.code == 0 and not result.timed_out:
                return self._decode_receipt(result.output, ticket, require_success=require_success)
            if result.code == 4:
                raise SandboxUncertain("the sandbox supervisor rejected the control binding")
            if result.code != 5:
                raise SandboxUncertain("the sandbox terminal receipt is unavailable: "
                                       + (result.text[:200] or "the helper did not answer"))
            if time.monotonic() >= deadline:
                raise SandboxUncertain("the sandbox terminal receipt is unavailable")
            await asyncio.sleep(0.2)

    async def _spawn(self, args: list[str], wants_stdin: bool):
        try:
            return await asyncio.create_subprocess_exec(
                self.docker, *args,
                stdin=asyncio.subprocess.PIPE if wants_stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError as exc:
            raise SandboxError("The docker CLI is not available.") from exc

    async def exec_bound(self, ticket: ExecTicket, argv: list[str], *, cwd: str, timeout_s: float,
                         env: dict[str, str] | None = None, stdin: bytes | None = None,
                         authorize_control: Callable[[], Awaitable[None]] | None = None
                         ) -> tuple[ExecResult, CleanupReceipt]:
        """Dispatch one payload through the installed helper, by exact ID, and require its terminal receipt."""
        container_id = self._exact_id(ticket.binding.container_id)
        if not _EXEC_ID.fullmatch(ticket.exec_id):
            raise ValueError("the exec identity is invalid")
        if not _TOKEN.fullmatch(ticket.control_token) or not _TOKEN.fullmatch(ticket.use.token):
            raise ValueError("the supervisor tokens must be 32 lowercase hexadecimal characters")
        await self._require_recorded_key(ticket)
        args = ["exec", "-i"]
        if ticket.binding.policy.user:
            args += ["-u", ticket.binding.policy.user]
        args += ["-w", cwd]
        started = time.monotonic()
        with _env_file(env) as env_args:
            args += [*env_args, container_id, _HELPER_PATH, "run", "--root", _RUNTIME_ROOT,
                     "--exec-id", ticket.exec_id, "--control-token", ticket.control_token,
                     "--usage-token", ticket.use.token, "--grace", str(float(self.kill_grace_s)),
                     "--reap", str(float(self.reap_seconds)), "--", *argv]
            proc = await self._spawn(args, stdin is not None)

            control_error = None

            async def cancel() -> None:
                nonlocal control_error
                if authorize_control is not None:
                    try:
                        await authorize_control()
                    except Exception as exc:
                        control_error = exc
                        # End only this transport. Without current authority the supervisor cannot be signalled.
                        if proc.returncode is None:
                            proc.kill()
                        return
                await self._send_cancel(ticket)

            try:
                output, timed_out = await collect(proc, stdin, timeout_s, cancel)
            except asyncio.CancelledError:
                await cancel()
                raise
            if control_error is not None:
                raise SandboxUncertain("sandbox cleanup authority is no longer valid") from control_error
        code = TIMEOUT_EXIT_CODE if timed_out else (proc.returncode if proc.returncode is not None else -1)
        result = ExecResult(code, output.decode("utf-8", errors="replace"), timed_out,
                            round(time.monotonic() - started, 3))
        receipt = await self._await_receipt(ticket, require_success=True)
        return result, receipt

    async def control_exec(self, ticket: ExecTicket, *, cancel: bool) -> CleanupReceipt:
        """Run the helper's cancel or receipt command by exact ID and decode the bounded terminal receipt."""
        self._exact_id(ticket.binding.container_id)
        if not _TOKEN.fullmatch(ticket.control_token) or not _TOKEN.fullmatch(ticket.use.token):
            raise ValueError("the supervisor tokens must be 32 lowercase hexadecimal characters")
        await self._require_recorded_key(ticket)
        if not cancel:
            result = await self._docker_raw(*self._control_args(ticket, "receipt", include_usage=False),
                                            timeout=15)
            if result.timed_out or result.code != 0:
                raise SandboxUncertain("the sandbox terminal receipt is unavailable")
            return self._decode_receipt(result.output, ticket, require_success=False)
        result = await self._docker_raw(*self._control_args(ticket, "cancel", include_usage=True), timeout=15)
        if result.code == 0 and not result.timed_out:
            if result.output.strip():
                return self._decode_receipt(result.output, ticket, require_success=False)
            return await self._await_receipt(ticket, require_success=False)
        if result.code == 4:
            raise SandboxUncertain("the sandbox supervisor rejected the control binding")
        if result.code == 5:
            raise SandboxUncertain("the sandbox exec endpoint or receipt is unavailable")
        raise SandboxUncertain("the sandbox cancellation did not produce a definite result")

    async def aclose(self) -> None:
        pass
