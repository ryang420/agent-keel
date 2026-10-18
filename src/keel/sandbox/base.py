"""What a sandbox is, how a profile's sandbox policy is read, and the process plumbing every backend shares."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import math
import os
import posixpath
import re
import uuid
from typing import Awaitable, Callable, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from keel.sandbox.records import (
        CleanupReceipt, ContainerObservation, DockerOutcome, ExecTicket, LifecycleAttempt,
    )
    from keel.sandbox.runtime import HelperArtifact

RUNTIMES = ("runc", "runsc", "kata")
TIMEOUT_EXIT_CODE = 124
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DRAIN_S = 1.0  # how long output is read after the process has exited
logger = logging.getLogger(__name__)
_TOP_KEYS = {"image", "workdir", "runtime", "network", "mounts", "env", "user", "cpus", "memory_mb",
             "pids_limit", "skills_path"}
_MOUNT_KEYS = {"source", "target", "read_only"}
_IMAGE = re.compile(r"^[^\s\x00-\x1f\x7f-][^\s\x00-\x1f\x7f]*$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(:[A-Za-z0-9_][A-Za-z0-9_.-]*)?$")


class SandboxError(Exception):
    """The sandbox could not be created or reached."""


class PolicyError(ValueError):
    """A profile's sandbox policy is invalid; the message is written for the API caller."""


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class SandboxPolicy:
    image: str
    workdir: str
    runtime: str = "runc"
    network: str | None = None
    mounts: tuple[Mount, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    user: str | None = None
    cpus: float | None = None
    memory_mb: int | None = None
    pids_limit: int | None = None
    skills_path: str | None = None

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


def _path(value, what: str, *, mount: bool = False) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value or (mount and "," in value):
        raise PolicyError(f"{what} must be an absolute path" + (" without commas." if mount else "."))
    return posixpath.normpath(value)


def _positive(data: dict, name: str, kind):
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"sandbox.{name} must be a positive number.")
    if kind is int:
        if not isinstance(value, int) or value < 1:
            raise PolicyError(f"sandbox.{name} must be a whole number of at least 1.")
        return value
    if not math.isfinite(value) or value <= 0:
        raise PolicyError(f"sandbox.{name} must be a finite number above 0.")
    return float(value)


def profile_mount_root(root: str | None, profile_id: str) -> str | None:
    """The only host tree a profile may bind into its sandbox."""
    if root is None:
        return None
    return os.path.join(os.path.realpath(root), hashlib.sha256(profile_id.encode()).hexdigest())


def _paths_overlap(first: str, second: str) -> bool:
    return first == second or first.startswith(second.rstrip("/") + "/") or second.startswith(first.rstrip("/") + "/")


def validate_mount_sources(mounts: tuple[Mount, ...], mount_root: str | None) -> None:
    if mounts and mount_root is None:
        raise PolicyError("sandbox.mounts requires a deployment-configured mount root.")
    if not mounts:
        return
    root = posixpath.normpath(mount_root)
    resolved_root = os.path.realpath(root)
    if resolved_root != root:
        raise PolicyError("sandbox mount root must not be a symlink.")
    for index, mount in enumerate(mounts):
        source = mount.source
        resolved_source = os.path.realpath(source)
        if ((source != root and not source.startswith(root.rstrip("/") + "/"))
                or (resolved_source != root and not resolved_source.startswith(root.rstrip("/") + "/"))):
            raise PolicyError(f"sandbox.mounts[].source must be under {root}.")
        if any(_paths_overlap(resolved_source, os.path.realpath(prior.source)) for prior in mounts[:index]):
            raise PolicyError("sandbox.mounts[].source entries must not overlap each other.")


def parse_policy(data: dict, mount_root: str | None = None) -> SandboxPolicy:
    if not isinstance(data, dict):
        raise PolicyError("sandbox must be an object.")
    for key in data:
        if key not in _TOP_KEYS:
            raise PolicyError(f"sandbox.{key} is not a known setting.")
    image = data.get("image")
    if not isinstance(image, str) or not image.strip():
        raise PolicyError("sandbox.image is required.")
    if not _IMAGE.fullmatch(image.strip()):
        raise PolicyError("sandbox.image must be an image reference without whitespace, control characters or a leading '-'.")
    runtime = data.get("runtime", "runc")
    if runtime not in RUNTIMES:
        raise PolicyError(f"sandbox.runtime must be one of {', '.join(RUNTIMES)}.")
    network = data.get("network")
    if network is not None and (not isinstance(network, str) or not network.strip()):
        raise PolicyError("sandbox.network must be a network name.")
    raw_mounts = data.get("mounts") or []
    if not isinstance(raw_mounts, list):
        raise PolicyError("sandbox.mounts must be a list.")
    mounts = []
    for raw in raw_mounts:
        if not isinstance(raw, dict):
            raise PolicyError("sandbox.mounts entries must be objects.")
        for key in raw:
            if key not in _MOUNT_KEYS:
                raise PolicyError(f"sandbox.mounts[].{key} is not a known setting.")
        source = _path(raw.get("source"), "sandbox.mounts[].source", mount=True)
        target = _path(raw.get("target"), "sandbox.mounts[].target", mount=True)
        mounts.append(Mount(source, target, bool(raw.get("read_only", False))))
    validate_mount_sources(tuple(mounts), mount_root)
    env = data.get("env") or {}
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and _ENV_NAME.fullmatch(k) and isinstance(v, str)
            and not any(c in v for c in "\0\r\n") for k, v in env.items()):
        raise PolicyError("sandbox.env must map variable names to strings without line breaks or NUL.")
    user = data.get("user")
    if user is not None and (not isinstance(user, str) or not _USER.fullmatch(user)):
        raise PolicyError("sandbox.user must be 'uid' or 'uid:gid'.")
    skills_path = data.get("skills_path")
    return SandboxPolicy(
        image=image.strip(), workdir=_path(data.get("workdir"), "sandbox.workdir"), runtime=runtime,
        network=network, mounts=tuple(mounts), env=tuple(sorted(env.items())), user=user,
        cpus=_positive(data, "cpus", float), memory_mb=_positive(data, "memory_mb", int),
        pids_limit=_positive(data, "pids_limit", int),
        skills_path=None if skills_path is None else _path(skills_path, "sandbox.skills_path"),
    )


def private_mount_conflict(candidate: SandboxPolicy, others: list[SandboxPolicy]) -> str | None:
    """Reject a mount that would expose another profile's host tree."""
    for mount in candidate.mounts:
        source = os.path.realpath(mount.source)
        for other in others:
            for theirs in other.mounts:
                peer = os.path.realpath(theirs.source)
                if _paths_overlap(source, peer):
                    return "sandbox.mounts[].source overlaps another profile's private directory."
    return None


@dataclass(frozen=True)
class Sandbox:
    profile_id: str
    name: str
    policy: SandboxPolicy


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str  # stdout and stderr interleaved, decoded as UTF-8 with replacement
    timed_out: bool
    duration_s: float


class SandboxExecutor(Protocol):
    """The I/O surface tools use, implemented by the bound facade and the local development backend."""

    async def exec(self, sandbox: Sandbox, argv: list[str], *, cwd: str, timeout_s: float, exec_id: str,
                   env: dict[str, str] | None = None, stdin: bytes | None = None) -> ExecResult: ...

    async def read_file(self, sandbox: Sandbox, path: str, max_bytes: int) -> bytes: ...

    async def write_file(self, sandbox: Sandbox, path: str, data: bytes) -> None: ...

    async def kill_tree(self, sandbox: Sandbox, exec_id: str) -> None: ...


class SandboxBackend(SandboxExecutor, Protocol):
    async def ensure(self, profile_id: str, policy: SandboxPolicy) -> Sandbox: ...

    def reference(self, profile_id: str, policy: SandboxPolicy) -> Sandbox:
        """The handle `ensure` would return, without creating or starting anything."""
        ...

    async def destroy(self, sandbox: Sandbox) -> None: ...

    async def aclose(self) -> None: ...


class CoordinatedSandboxBackend(Protocol):
    """The exact-ID operations the lifecycle coordinator drives.

    Wider than `SandboxBackend`, which the agent loop uses: every method addresses a recorded full container ID,
    never a reusable name, and an implementation never calls `ensure` or retries a payload. A definite failure is
    kept apart from uncertainty: a timeout, killed client, transport loss or malformed output is never read as
    "the container does not exist".
    """

    async def daemon_identity(self) -> tuple[str, str]:
        """The Docker daemon ID and normalized architecture, cached once read."""
        ...

    async def inspect_id(self, container_id: str) -> DockerOutcome: ...

    async def inventory(self) -> list[ContainerObservation]: ...

    async def create_generation(self, attempt: LifecycleAttempt, policy: SandboxPolicy) -> DockerOutcome: ...

    async def install_helper(self, attempt: LifecycleAttempt, artifact: HelperArtifact) -> DockerOutcome: ...

    async def start_id(self, attempt: LifecycleAttempt) -> DockerOutcome: ...

    async def remove_id(self, attempt: LifecycleAttempt) -> DockerOutcome: ...

    async def exec_bound(self, ticket: ExecTicket, argv: list[str], *, cwd: str, timeout_s: float,
                         env: dict[str, str] | None = None, stdin: bytes | None = None,
                         authorize_control: Callable[[], Awaitable[None]] | None = None
                         ) -> tuple[ExecResult, CleanupReceipt]: ...

    async def control_exec(self, ticket: ExecTicket, *, cancel: bool) -> CleanupReceipt: ...

    async def aclose(self) -> None: ...


class ExecFileIO:
    """File helpers dispatch through the facade's exec, giving each helper exactly one durable ticket."""

    async def _helper(self, sandbox: Sandbox, script: str, *args: str,
                      stdin: bytes | None = None) -> ExecResult:
        return await self.exec(sandbox, ["sh", "-c", script, "keel-helper", *args], cwd="/", timeout_s=60,
                               exec_id="h" + uuid.uuid4().hex, stdin=stdin)

    async def read_file(self, sandbox: Sandbox, path: str, max_bytes: int) -> bytes:
        # Base64 keeps arbitrary bytes separate from the UTF-8 stdout transport. Leave room for its expansion.
        cap = (MAX_OUTPUT_BYTES // 4) * 3 - 4096
        script = ('[ -d "$1" ] && exit 3; [ -e "$1" ] || exit 4; [ -r "$1" ] || exit 5; '
                  'head -c "$2" "$1" 2>/dev/null | base64 | tr -d "\\n"')
        result = await self._helper(sandbox, script, path, str(min(max_bytes, cap)))
        if result.exit_code == 3:
            raise IsADirectoryError(path)
        if result.exit_code == 4:
            raise FileNotFoundError(path)
        if result.exit_code == 5:
            raise PermissionError(path)
        if result.exit_code != 0:
            raise OSError(f"could not read {path}: {result.output.strip()[:300]}")
        try:
            return base64.b64decode(result.output, validate=True)
        except ValueError as exc:
            raise OSError(f"could not read {path}") from exc

    async def write_file(self, sandbox: Sandbox, path: str, data: bytes) -> None:
        result = await self._helper(sandbox, 'mkdir -p "$(dirname "$1")" && base64 -d > "$1"',
                                    path, stdin=base64.b64encode(data))
        if result.exit_code != 0:
            raise PermissionError(result.output.strip()[:300] or path)


async def _exited(proc: asyncio.subprocess.Process) -> None:
    """Return once the process has exited. `proc.wait()` would also wait for every pipe to close, which a
    background child holding stdout can delay indefinitely."""
    while proc.returncode is None:
        await asyncio.sleep(0.02)


async def collect(proc: asyncio.subprocess.Process, stdin: bytes | None, timeout_s: float,
                  on_timeout: Callable[[], Awaitable[None]]) -> tuple[bytes, bool]:
    """Feed stdin and read the merged output (up to MAX_OUTPUT_BYTES) until the process exits.

    Past the deadline `on_timeout` must end the process; what was read so far is returned with timed_out=True.
    If `on_timeout` raises, the error is logged and the process is killed directly. Once the process has exited,
    output is read for at most DRAIN_S more, so a background child that keeps the pipe open cannot hold the
    command. Every exit path (normal, timeout, error, cancellation) cancels the helper tasks and kills the
    process if it is still running; ending the rest of its process tree after a cancellation is the caller's
    job (kill_tree).
    """
    buffer = bytearray()
    truncated = False

    async def feed() -> None:
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (ConnectionError, BrokenPipeError, OSError):
                pass

    async def read() -> None:
        nonlocal truncated
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            room = MAX_OUTPUT_BYTES - len(buffer)
            if len(chunk) > room:
                truncated = True
            if room > 0:
                buffer.extend(chunk[:room])

    reader = asyncio.ensure_future(read())
    tasks = [reader]
    if stdin is not None and proc.stdin is not None:
        tasks.append(asyncio.ensure_future(feed()))
    timed_out = False
    try:
        try:
            await asyncio.wait_for(_exited(proc), timeout_s)
        except TimeoutError:
            timed_out = True
            try:
                await on_timeout()
            except Exception:
                logger.exception("on_timeout failed; killing the process directly")
                if proc.returncode is None:
                    proc.kill()
            try:
                await asyncio.wait_for(_exited(proc), 5)
            except TimeoutError:
                pass
        try:
            await asyncio.wait_for(reader, DRAIN_S)
        except (TimeoutError, Exception):
            pass
    finally:
        if proc.returncode is None:
            proc.kill()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Release the pipes (a background child may still hold them) so no transport outlives this call.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
    data = bytes(buffer)
    if truncated:
        data += f"\n[output truncated at {MAX_OUTPUT_BYTES} bytes]\n".encode()
    return data, timed_out
