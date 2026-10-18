"""terminal and execute_code. Result shape {"output", "exit_code", "error"[, "cwd"]} follows Hermes's terminal
tool (tools/terminal_tool_result.py). There are no background processes, PTYs or persistent kernels."""

from __future__ import annotations

import asyncio
import posixpath
import re
import uuid

from keel.sandbox.base import SandboxError
from keel.tools.base import (
    ToolArgumentError, ToolOutcome, error_result, optional_int, require, tool, tool_json, truncate_middle,
)

TERMINAL_DEFAULT_TIMEOUT = 180
TERMINAL_MAX_TIMEOUT = 600
CODE_TIMEOUT = 300
OUTPUT_LIMIT = 50_000
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


# The command is passed as an argument, never spliced into the script, so its text cannot join the marker line.
# The EXIT trap reports the final directory and keeps the command's exit status. The token is fresh per call.
# The exec itself starts in "/" (which always exists): the cd happens here, so a session directory that has since
# vanished falls back to the policy workdir (or "/") instead of making every exec fail before bash starts. An empty
# fallback means the caller named the directory itself: then a missing directory is reported, not replaced.
_WRAPPER = ('__keel_token=$1; __keel_cmd=$2; __keel_dir=$3; __keel_fb=$4; '
            'trap \'__keel_rc=$?; printf "\\n%s%s\\n" "$__keel_token" "$(pwd)"; exit $__keel_rc\' EXIT; '
            'if ! cd -- "$__keel_dir" 2>/dev/null; then '
            'if [ -z "$__keel_fb" ]; then printf "\\n%sNODIR\\n" "$__keel_token"; exit 125; fi; '
            'cd -- "$__keel_fb" 2>/dev/null || cd /; '
            'echo "[cwd $__keel_dir no longer exists; using $(pwd)]" >&2; fi; '
            'eval "$__keel_cmd"')


def _argv(command: str, token: str, directory: str, fallback: str = "") -> list[str]:
    return ["bash", "-c", _WRAPPER, "keel", token, command, directory, fallback]


def _split_cwd(output: str, token: str) -> tuple[str, str | None]:
    """Split off this call's cwd report (its last occurrence). Text after the report line, such as the sandbox's
    truncation note, belongs to the output. A report that is not an absolute single-line path is ignored."""
    index = output.rfind("\n" + token)
    if index < 0:
        return output, None
    line, _, rest = output[index + 1 + len(token):].partition("\n")
    note = rest.strip()
    if note and not note.startswith("[output truncated"):
        # More than one line follows the token: the path held a newline. Report no cwd rather than a wrong one.
        return output[:index], None
    return output[:index] + ("\n" + note if note else ""), line if line.startswith("/") else None


def _call_name(ctx) -> str:
    return (_UNSAFE.sub("", ctx.tool_call_id)[:64] or "call") + "-" + uuid.uuid4().hex[:8]


async def _shown(ctx, text: str) -> str:
    """Head/tail-truncate long output and save the full text in the sandbox, in the workdir or else /tmp."""
    if len(text) <= OUTPUT_LIMIT:
        return text
    shown = truncate_middle(text, OUTPUT_LIMIT)
    name = f"{_call_name(ctx)}.txt"
    reason = ""
    for root in (ctx.sandbox.policy.workdir, "/tmp"):
        path = posixpath.join(root, ".keel", "outputs", name)
        try:
            await ctx.backend.write_file(ctx.sandbox, path, text.encode("utf-8"))
            return shown + f"\n\n[Full output saved to {path}]"
        except SandboxError:
            raise
        except Exception as exc:
            reason = reason or f"{type(exc).__name__}: {exc}"
    return shown + f"\n\n[Full output could not be saved: {reason}]"


def _body(result, output: str, timeout: int) -> dict:
    body = {"output": output, "exit_code": result.exit_code, "error": None}
    if result.timed_out:
        body["error"] = f"Command timed out after {timeout}s and was killed."
    return body


@tool("terminal")
async def terminal(args: dict, ctx) -> ToolOutcome:
    command = require(args, "command", str)
    if not command.strip():
        raise ToolArgumentError("command is empty.")
    for flag in ("background", "pty"):
        if args.get(flag):
            raise ToolArgumentError(f"{flag} is not supported: commands run in the foreground only.")
    timeout = min(optional_int(args, "timeout", TERMINAL_DEFAULT_TIMEOUT, minimum=1), TERMINAL_MAX_TIMEOUT)
    workdir = args.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        raise ToolArgumentError("Argument workdir must be a string.")
    token = f"__KEEL_CWD_{uuid.uuid4().hex}__"
    if workdir:
        argv = _argv(command, token, ctx.resolve(workdir))
    else:
        argv = _argv(command, token, ctx.cwd, ctx.sandbox.policy.workdir)
    result = await ctx.exec(argv, timeout_s=timeout)
    if workdir and "\n" + token + "NODIR\n" in result.output:
        return error_result(f"The workdir {ctx.resolve(workdir)} does not exist.")
    output, new_cwd = _split_cwd(result.output, token)
    body = _body(result, await _shown(ctx, output.rstrip("\n")), timeout)
    if new_cwd and not workdir and new_cwd != ctx.cwd:
        ctx.cwd = new_cwd
        body["cwd"] = new_cwd
    return ToolOutcome(tool_json(body), error=result.timed_out)


async def _remove(ctx, path: str) -> None:
    """Best effort, through the same registered execution path as every other helper."""
    try:
        await ctx.backend.exec(ctx.sandbox, ["rm", "-f", path], cwd="/", timeout_s=30,
                               exec_id="r" + uuid.uuid4().hex[:16])
    except SandboxError:
        raise
    except Exception:
        pass


@tool("execute_code")
async def execute_code(args: dict, ctx) -> ToolOutcome:
    code = require(args, "code", str)
    path = f"/tmp/.keel/code-{_call_name(ctx)}.py"
    try:
        await ctx.backend.write_file(ctx.sandbox, path, code.encode("utf-8"))
    except OSError as exc:
        return error_result(f"Cannot write the script: {exc}")
    try:
        token = f"__KEEL_CWD_{uuid.uuid4().hex}__"
        result = await ctx.exec(_argv(f"python3 {path}", token, ctx.cwd, ctx.sandbox.policy.workdir),
                                timeout_s=CODE_TIMEOUT)
    except (Exception, asyncio.CancelledError):
        try:
            await _remove(ctx, path)
        except Exception:
            pass  # retain the original failure/cancellation; the facade has recorded cleanup uncertainty
        raise
    await _remove(ctx, path)
    output, new_cwd = _split_cwd(result.output, token)
    body = _body(result, await _shown(ctx, output.rstrip("\n")), CODE_TIMEOUT)
    if new_cwd and new_cwd != ctx.cwd:  # only a vanished session directory changes it: the script's cwd is not kept
        ctx.cwd = new_cwd
        body["cwd"] = new_cwd
    return ToolOutcome(tool_json(body), error=result.timed_out)
