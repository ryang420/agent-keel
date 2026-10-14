"""The Docker sandbox: one long-lived container per profile, driven through the docker CLI.

Commands run through `docker exec` inside `setsid`, so each is its own process group. Its group id is written to
/tmp/.keel/<exec_id>.pid, and kill_tree signals the whole group from a second `docker exec`: killing the docker
client alone would leave the command running in the container.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
import hashlib
import logging
import os
import shutil
import tempfile
import time
from typing import Collection
import uuid

from keel.sandbox.base import (
    MAX_OUTPUT_BYTES, TIMEOUT_EXIT_CODE, ExecResult, PolicyError, Sandbox, SandboxError, SandboxPolicy, collect,
    profile_mount_root, validate_mount_sources,
)

log = logging.getLogger(__name__)

_WRAP = ('id="$1"; shift; mkdir -p /tmp/.keel; '
         'setsid sh -c \'echo $$ > "/tmp/.keel/$0.pid"; exec "$@"\' "$id" "$@"; '
         'rc=$?; rm -f "/tmp/.keel/$id.pid"; exit $rc')
_SIGNAL = 'p=$(cat "/tmp/.keel/$1.pid" 2>/dev/null) || exit 0; kill -s "$2" -- "-$p" 2>/dev/null; true'
_ALIVE = 'p=$(cat "/tmp/.keel/$1.pid" 2>/dev/null) || exit 1; kill -s 0 -- "-$p" 2>/dev/null'
_READ = '[ -d "$1" ] && exit 3; [ -e "$1" ] || exit 4; [ -r "$1" ] || exit 5; head -c "$2" "$1" 2>/dev/null | base64 | tr -d "\\n"'
_WRITE = 'mkdir -p "$(dirname "$1")" && base64 -d > "$1"'
_MAX_READ = (MAX_OUTPUT_BYTES // 4) * 3 - 4096  # what fits in collect's output cap once base64-encoded
_GONE = ("No such container", "is not running")


@contextmanager
def _env_file(env):
    """Environment values for a docker command as `--env-file` arguments. The file lives in a private (0700)
    directory, is readable by the owner only, and is removed when the command is done: values never reach argv, and
    they do not change the docker client's own environment (which a policy PATH or DOCKER_HOST would)."""
    if not env:
        yield []
        return
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
    def __init__(self, *, docker: str = "docker", deployment: str = "keel", idle_seconds: float = 1800.0,
                 kill_grace_s: float = 3.0, clock=time.monotonic, mount_root: str | None = None) -> None:
        self.docker = docker
        self.deployment = deployment
        self.idle_seconds = idle_seconds
        self.kill_grace_s = kill_grace_s
        self.clock = clock
        self.mount_root = mount_root
        self._active: dict[str, int] = {}       # container -> commands running in it
        self._last_used: dict[str, float] = {}  # container -> clock() of its last use
        self._clients: dict[str, asyncio.subprocess.Process] = {}  # exec_id -> its docker exec client
        self._locks: dict[str, asyncio.Lock] = {}

    def container_name(self, profile_id: str) -> str:
        return "keel-sbx-" + hashlib.sha256(f"{self.deployment}\0{profile_id}".encode()).hexdigest()[:12]

    def reference(self, profile_id: str, policy: SandboxPolicy) -> Sandbox:
        return Sandbox(profile_id, self.container_name(profile_id), policy)

    def run_args(self, profile_id: str, policy: SandboxPolicy, env_file: str | None = None) -> list[str]:
        try:
            validate_mount_sources(policy.mounts, profile_mount_root(self.mount_root, profile_id))
        except PolicyError as exc:
            raise SandboxError(f"The sandbox mount policy is invalid: {exc}") from exc
        # Hardening mirrors the Hermes docker backend: no capabilities beyond file ownership and access, no
        # privilege gain, and writable scratch space on tmpfs (nosuid; /tmp stays executable for scripts).
        args = ["run", "-d", "--init", "--name", self.container_name(profile_id),
                "--label", "keel.managed=1", "--label", f"keel.deployment={self.deployment}",
                "--label", f"keel.profile={profile_id}", "--label", f"keel.policy={policy.digest()}",
                "--runtime", policy.runtime, "--workdir", policy.workdir,
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
        return [*args, policy.image, "sleep", "infinity"]

    async def _docker(self, *args: str, stdin: bytes | None = None, timeout: float = 60.0) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError as exc:
            raise SandboxError("The docker CLI is not available.") from exc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except TimeoutError as exc:
            proc.kill()
            raise SandboxError(f"docker {args[0]} did not answer within {timeout:.0f}s.") from exc
        return proc.returncode, out.decode("utf-8", errors="replace")

    async def ensure(self, profile_id: str, policy: SandboxPolicy) -> Sandbox:
        name = self.container_name(profile_id)
        async with self._locks.setdefault(name, asyncio.Lock()):
            code, out = await self._docker("inspect", "-f", '{{.State.Running}} {{index .Config.Labels "keel.policy"}}',
                                           name)
            if code == 0:
                running, _, digest = out.strip().partition(" ")
                if digest != policy.digest() and not self._active.get(name):
                    await self._docker("rm", "-f", name)
                    code = 1
                elif digest != policy.digest():
                    log.warning("Sandbox %s keeps its old policy until it is idle: commands are running.", name)
                    if running != "true":
                        started, out = await self._docker("start", name)
                        if started != 0:
                            raise SandboxError(f"Could not start the sandbox: {out.strip()[:300]}")
                elif running != "true":
                    started, out = await self._docker("start", name)
                    if started != 0:
                        raise SandboxError(f"Could not start the sandbox: {out.strip()[:300]}")
            if code != 0:
                with _env_file(policy.env) as env_args:
                    created, out = await self._docker(*self.run_args(profile_id, policy, env_args[1] if env_args else None),
                                                      timeout=300)
                if created != 0:
                    raise SandboxError(f"Could not create the sandbox: {out.strip()[:300]}")
        self._last_used[name] = self.clock()
        return self.reference(profile_id, policy)

    async def exec(self, sandbox: Sandbox, argv: list[str], *, cwd: str, timeout_s: float, exec_id: str,
                   env: dict[str, str] | None = None, stdin: bytes | None = None) -> ExecResult:
        result = await self._exec_once(sandbox, argv, cwd, timeout_s, exec_id, env, stdin)
        if (result.exit_code != 0 and result.output.startswith("Error response from daemon")
                and any(marker in result.output for marker in _GONE)
                and await self._container_gone(sandbox.name)):
            # The container vanished (removed, daemon restarted): recreate it and try once more.
            sandbox = await self.ensure(sandbox.profile_id, sandbox.policy)
            result = await self._exec_once(sandbox, argv, cwd, timeout_s, exec_id, env, stdin)
        return result

    async def _container_gone(self, name: str) -> bool:
        """True when the container is missing or stopped, so the daemon error was not the command's own output."""
        code, out = await self._docker("inspect", "-f", "{{.State.Running}}", name)
        return code != 0 or out.strip() != "true"

    async def _exec_once(self, sandbox: Sandbox, argv: list[str], cwd: str, timeout_s: float, exec_id: str,
                         env: dict[str, str] | None, stdin: bytes | None) -> ExecResult:
        args = ["exec", "-i", "-w", cwd]
        if sandbox.policy.user:
            args += ["-u", sandbox.policy.user]
        started = time.monotonic()
        with _env_file(env) as env_args:
            args += [*env_args, sandbox.name, "sh", "-c", _WRAP, "keel-exec", exec_id, *argv]
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.docker, *args,
                    stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            except FileNotFoundError as exc:
                raise SandboxError("The docker CLI is not available.") from exc
            name = sandbox.name
            self._active[name] = self._active.get(name, 0) + 1
            self._clients[exec_id] = proc
            try:
                output, timed_out = await collect(proc, stdin, timeout_s, lambda: self.kill_tree(sandbox, exec_id))
            finally:
                self._active[name] -= 1
                self._last_used[name] = self.clock()
                self._clients.pop(exec_id, None)
        code = TIMEOUT_EXIT_CODE if timed_out else (proc.returncode if proc.returncode is not None else -1)
        return ExecResult(code, output.decode("utf-8", errors="replace"), timed_out,
                          round(time.monotonic() - started, 3))

    async def _helper(self, sandbox: Sandbox, script: str, *args: str, stdin: bytes | None = None) -> ExecResult:
        return await self.exec(sandbox, ["sh", "-c", script, "keel-helper", *args], cwd="/", timeout_s=60,
                               exec_id="h" + uuid.uuid4().hex[:16], stdin=stdin)

    async def read_file(self, sandbox: Sandbox, path: str, max_bytes: int) -> bytes:
        result = await self._helper(sandbox, _READ, path, str(min(max_bytes, _MAX_READ)))
        if result.exit_code == 3:
            raise IsADirectoryError(path)
        if result.exit_code == 4:
            raise FileNotFoundError(path)
        if result.exit_code == 5:
            raise PermissionError(path)
        if result.exit_code != 0:
            raise OSError(f"could not read {path}: {result.output.strip()[:300]}")
        try:
            if "[output truncated" in result.output:
                raise ValueError("output truncated")
            return base64.b64decode(result.output)
        except ValueError as exc:  # binascii.Error is a ValueError
            raise OSError(f"could not read {path}") from exc

    async def write_file(self, sandbox: Sandbox, path: str, data: bytes) -> None:
        result = await self._helper(sandbox, _WRITE, path, stdin=base64.b64encode(data))
        if result.exit_code != 0:
            raise PermissionError(result.output.strip()[:300] or path)

    async def kill_tree(self, sandbox: Sandbox, exec_id: str) -> None:
        await self._docker("exec", sandbox.name, "sh", "-c", _SIGNAL, "keel-kill", exec_id, "TERM", timeout=15)
        deadline = time.monotonic() + self.kill_grace_s
        while time.monotonic() < deadline:
            code, _ = await self._docker("exec", sandbox.name, "sh", "-c", _ALIVE, "keel-alive", exec_id, timeout=15)
            if code != 0:
                break
            await asyncio.sleep(0.2)
        else:
            await self._docker("exec", sandbox.name, "sh", "-c", _SIGNAL, "keel-kill", exec_id, "KILL", timeout=15)
        client = self._clients.get(exec_id)
        if client is not None and client.returncode is None:
            client.kill()

    async def destroy(self, sandbox: Sandbox) -> None:
        await self._docker("rm", "-f", sandbox.name)
        self._last_used.pop(sandbox.name, None)

    async def reap_idle(self) -> int:
        """Remove containers unused for idle_seconds that are running nothing; they are recreated on next use."""
        now, removed = self.clock(), 0
        for name, last in list(self._last_used.items()):
            if now - last >= self.idle_seconds and not self._active.get(name):
                await self._docker("rm", "-f", name)
                self._last_used.pop(name, None)
                removed += 1
        return removed

    async def remove_orphans(self, keep: Collection[str] = frozenset()) -> int:
        """At start: remove this deployment's containers, except those of the profiles in `keep` (profiles with a run
        in flight, possibly on another instance). A removed container is recreated on its next use."""
        code, out = await self._docker("ps", "-a", "--filter", "label=keel.managed=1",
                                       "--filter", f"label=keel.deployment={self.deployment}",
                                       "--format", '{{.ID}} {{.Label "keel.profile"}}')
        if code != 0:
            log.warning("could not list leftover sandbox containers: %s", out.strip()[:500])
        ids = []
        for line in out.splitlines() if code == 0 else []:
            container, _, profile = line.strip().partition(" ")
            if container and profile not in keep:
                ids.append(container)
        if ids:
            rm_code, rm_out = await self._docker("rm", "-f", *ids)
            if rm_code != 0:
                log.warning("could not remove leftover sandbox containers: %s", rm_out.strip()[:500])
        self._last_used.clear()
        return len(ids)

    async def aclose(self) -> None:
        pass
