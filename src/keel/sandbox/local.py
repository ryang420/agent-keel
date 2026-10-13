"""A sandbox that runs commands as host processes. For tests and local development only: it isolates nothing,
and sandbox paths are host paths."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import time

from keel.sandbox.base import TIMEOUT_EXIT_CODE, ExecResult, Sandbox, SandboxPolicy, collect


class LocalSandbox:
    def __init__(self, *, kill_grace_s: float = 3.0) -> None:
        self.kill_grace_s = kill_grace_s
        self._groups: dict[str, int] = {}  # exec_id -> process group id, kept until the command ends or is killed

    async def ensure(self, profile_id: str, policy: SandboxPolicy) -> Sandbox:
        Path(policy.workdir).mkdir(parents=True, exist_ok=True)
        return self.reference(profile_id, policy)

    def reference(self, profile_id: str, policy: SandboxPolicy) -> Sandbox:
        return Sandbox(profile_id, f"local-{profile_id}", policy)

    async def exec(self, sandbox: Sandbox, argv: list[str], *, cwd: str, timeout_s: float, exec_id: str,
                   env: dict[str, str] | None = None, stdin: bytes | None = None) -> ExecResult:
        started = time.monotonic()
        environment = {**os.environ, **dict(sandbox.policy.env), **(env or {})}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=environment,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            return ExecResult(127, f"{exc}\n", False, round(time.monotonic() - started, 3))
        self._groups[exec_id] = proc.pid
        output, timed_out = await collect(proc, stdin, timeout_s, lambda: self.kill_tree(sandbox, exec_id))
        # Reached only when the command ended or timed out; a cancelled caller leaves the group registered so
        # kill_tree can still end it.
        self._groups.pop(exec_id, None)
        code = TIMEOUT_EXIT_CODE if timed_out else (proc.returncode if proc.returncode is not None else -1)
        return ExecResult(code, output.decode("utf-8", errors="replace"), timed_out,
                          round(time.monotonic() - started, 3))

    async def read_file(self, sandbox: Sandbox, path: str, max_bytes: int) -> bytes:
        with open(path, "rb") as handle:
            return handle.read(max_bytes)

    async def write_file(self, sandbox: Sandbox, path: str, data: bytes) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def kill_tree(self, sandbox: Sandbox, exec_id: str) -> None:
        # The group stays registered until it is gone or SIGKILL was sent, so a cancelled kill can be retried.
        pgid = self._groups.get(exec_id)
        if pgid is None:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.monotonic() + self.kill_grace_s
            while time.monotonic() < deadline:
                os.killpg(pgid, 0)
                await asyncio.sleep(0.05)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._groups.pop(exec_id, None)

    async def destroy(self, sandbox: Sandbox) -> None:
        pass

    async def aclose(self) -> None:
        pass
