"""Command runs (docs/design/2026-10-31-command-runs.md): the request's `command` object, the argv that runs it in
the sandbox, and the part of its output a run keeps."""

from __future__ import annotations

import math
import posixpath

from keel.sandbox.base import env_is_valid

MAX_ARGV_ITEMS = 64
MAX_ARGV_BYTES = 16 * 1024
OUTPUT_TAIL_CHARS = 50_000
MISSING_CWD_EXIT_CODE = 125
_KEYS = frozenset({"argv", "cwd", "timeout_s", "env"})
# The exec starts in "/", which always exists, and this fixed prologue enters the command's directory: a docker exec
# into a missing directory fails before the sandbox supervisor starts and leaves the exec without a cleanup receipt.
# The directory and the argv are positional parameters, so no shell ever parses them.
_ENTER = (f'cd "$1" 2>/dev/null || {{ echo "keel: cannot enter $1" >&2; exit {MISSING_CWD_EXIT_CODE}; }}; '
          'shift; exec "$@"')


class CommandError(ValueError):
    """The request's command is unusable; the message is written for the API caller."""


def _text(value: object) -> bool:
    """A string docker can pass on: no NUL, and encodable as UTF-8 (JSON can carry lone surrogates)."""
    if not isinstance(value, str) or "\0" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def parse_command(raw: object, *, workdir: str | None, max_timeout_s: int) -> dict:
    """Validate a command run's `command` against the profile's sandbox workdir and return it normalised.

    Applying it again to its own result returns the same dict, so a stored command can be checked again later.
    """
    if workdir is None:
        raise CommandError("A command run needs a profile with a sandbox.")
    if not isinstance(raw, dict):
        raise CommandError("command must be an object.")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise CommandError(f"command.{unknown[0]} is not a known setting.")
    argv = raw.get("argv")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV_ITEMS or not all(_text(a) for a in argv)
            or not argv[0]):
        raise CommandError(f"command.argv must be a list of 1 to {MAX_ARGV_ITEMS} strings without NUL, the first "
                           "one not empty.")
    if sum(len(a.encode("utf-8")) for a in argv) > MAX_ARGV_BYTES:
        raise CommandError(f"command.argv must be at most {MAX_ARGV_BYTES} bytes in total.")
    cwd = raw.get("cwd")
    if not _text(cwd) or not cwd.startswith("/"):
        raise CommandError("command.cwd must be an absolute path.")
    cwd = posixpath.normpath(cwd)
    root = posixpath.normpath(workdir)
    if cwd != root and not cwd.startswith(root.rstrip("/") + "/"):
        raise CommandError(f"command.cwd must be under the sandbox workdir {root}.")
    timeout = raw.get("timeout_s")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
            or not 1 <= timeout <= max_timeout_s):
        raise CommandError(f"command.timeout_s must be a number from 1 to {max_timeout_s}.")
    env = {} if raw.get("env") is None else raw["env"]
    if not env_is_valid(env) or not all(_text(value) for value in env.values()):
        raise CommandError("command.env must map variable names to strings without line breaks or NUL.")
    return {"argv": list(argv), "cwd": cwd, "timeout_s": timeout, "env": dict(env)}


def wrapped_argv(command: dict) -> list[str]:
    """The argv to exec with cwd "/": the prologue enters command["cwd"], then execs command["argv"] as given."""
    return ["sh", "-c", _ENTER, "keel-command", command["cwd"], *command["argv"]]


def output_tail(text: str) -> str:
    """What a command run keeps of its output: the last OUTPUT_TAIL_CHARS characters."""
    return text[-OUTPUT_TAIL_CHARS:]
