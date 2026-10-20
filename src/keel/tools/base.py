"""What every tool gets and returns, the registry, and shared helpers: argument checks, truncation (Hermes's
head/tail marker), redaction and event previews."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import posixpath
import re
from typing import Awaitable, Callable
import uuid

from keel.sandbox.base import ExecResult, Sandbox, SandboxError, SandboxExecutor

MAX_TOOL_RESULT_CHARS = 100_000
PREVIEW_CHARS = 500
HANDLERS: dict[str, Callable[[dict, "ToolContext"], Awaitable["ToolOutcome"]]] = {}

_KEY_WORDS = r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY)"
_JSON_KEY_WORDS = r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
_VALUE = r"""(?:"[^"]{0,2000}"|'[^']{0,2000}'|[^\s'"]+)"""
# Every repetition before a keyword is bounded and every pattern starts at a \b, a quote or a line start, so a
# model-controlled run of word characters cannot make redaction quadratic.
_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[A-Za-z0-9+/=\s]{0,20000}"
                r"(?:-----END [A-Z ]{0,30}PRIVATE KEY-----)?"), "[REDACTED]"),
    (re.compile(r"\bA(?:KIA|SIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
                r"|xox[abpr]-[A-Za-z0-9-]{10,})"), "[REDACTED]"),
    (re.compile(r"(?i)\b(bearer)[ \t]+[A-Za-z0-9._~+/=-]+"), r"\1 [REDACTED]"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]{0,20}://[^\s:/@]{1,200}):[^\s@/]{1,200}@", re.I), r"\1:[REDACTED]@"),
    (re.compile(r"(--(?:password|passwd|token|secret|api-key)(?:=|[ \t]+))" + _VALUE, re.I), r"\1[REDACTED]"),
    (re.compile(r"\b([A-Za-z0-9_]{0,64}" + _KEY_WORDS + r"[A-Za-z0-9_]{0,64})[ \t]{0,20}=[ \t]{0,20}" + _VALUE,
                re.I), r"\1=[REDACTED]"),
    # "password": "x" (a quoted key), and YAML `password: x` where the key starts the line.
    (re.compile(r'("[A-Za-z0-9_.-]{0,64}' + _JSON_KEY_WORDS + r'[A-Za-z0-9_.-]{0,64}")[ \t]{0,20}:[ \t]{0,20}'
                r"""(?:"[^"]{0,2000}"|'[^']{0,2000}'|[^\s,}"']+)""", re.I), r"\1: [REDACTED]"),
    (re.compile(r"(?m)^([ \t]{0,40}[A-Za-z0-9_-]{0,64}" + _JSON_KEY_WORDS + r"[A-Za-z0-9_-]{0,64})[ \t]{0,20}:"
                r"""[ \t]{1,20}(?:"[^"]{0,2000}"|'[^']{0,2000}'|[^\s,}"']+)""", re.I), r"\1: [REDACTED]"),
]


class ToolArgumentError(Exception):
    """The model's arguments are unusable; the message goes back to it as the tool's error."""


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    error: bool = False
    sandbox_error: SandboxError | None = None  # internal run-ending signal, never part of the tool JSON


@dataclass
class ToolContext:
    backend: SandboxExecutor | None = None
    sandbox: Sandbox | None = None
    cwd: str = ""
    tool_call_id: str = ""
    skills: tuple = ()
    on_exec: Callable[[str | None], Awaitable[None]] | None = None
    store: object | None = None
    profile_id: str = ""
    profile_incarnation: str | None = None
    session_id: str = ""
    memory_chars: int = 2200
    user_chars: int = 1375

    def resolve(self, path: str) -> str:
        return posixpath.normpath(posixpath.join(self.cwd, path))

    async def exec(self, argv: list[str], *, timeout_s: float, cwd: str | None = None,
                   env: dict[str, str] | None = None, stdin: bytes | None = None) -> ExecResult:
        """Run through the I/O facade; on_exec is a convenience pointer, never coordinated cleanup authority.

        The pointer survives cancellation for legacy stop callers. Without `cwd` the command starts in "/", which
        always exists (the session directory may not): tools pass absolute paths or cd themselves.
        """
        exec_id = "x" + uuid.uuid4().hex[:16]
        if self.on_exec is not None:
            await self.on_exec(exec_id)
        try:
            result = await self.backend.exec(self.sandbox, argv, cwd=cwd or "/", timeout_s=timeout_s,
                                             exec_id=exec_id, env=env, stdin=stdin)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.on_exec is not None:
                await self.on_exec(None)
            raise
        if self.on_exec is not None:
            await self.on_exec(None)
        return result


def tool(name: str):
    def register(handler):
        HANDLERS[name] = handler
        return handler
    return register


def tool_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def error_result(message: str, **extra) -> ToolOutcome:
    return ToolOutcome(tool_json({**extra, "error": message}), error=True)


def require(args: dict, name: str, kind: type):
    value = args.get(name)
    if value is None:
        raise ToolArgumentError(f"Missing required argument: {name}")
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ToolArgumentError(f"Argument {name} must be a {kind.__name__}.")
    return value


def optional_str(args: dict, name: str, default: str, choices: tuple[str, ...] | None = None) -> str:
    value = args.get(name)
    if value is None or value == "":
        return default
    if not isinstance(value, str) or (choices is not None and value not in choices):
        allowed = f" one of {', '.join(choices)}" if choices else " a string"
        raise ToolArgumentError(f"Argument {name} must be{allowed}.")
    return value


def optional_int(args: dict, name: str, default: int, minimum: int | None = None) -> int:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"Argument {name} must be an integer.")
    if minimum is not None and value < minimum:
        raise ToolArgumentError(f"Argument {name} must be at least {minimum}.")
    return value


def truncate_middle(text: str, limit: int = 50_000) -> str:
    """Keep the first 40% and the last 60% of `limit` characters, with Hermes's marker between them."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    omitted = len(text) - head - tail
    return (f"{text[:head]}\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted out of {len(text):,} total] ...\n\n"
            f"{text[-tail:]}")


def _strings(data) -> list:
    """(length, container, key) for every string value in a JSON structure."""
    slots, stack = [], [data]
    while stack:
        node = stack.pop()
        for key, value in (node.items() if isinstance(node, dict) else enumerate(node)):
            if isinstance(value, str):
                slots.append((len(value), node, key))
            elif isinstance(value, (dict, list)):
                stack.append(value)
    return slots


def _lists(data) -> list:
    found, stack = [], [data]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            found.append(node)
        for value in (node.values() if isinstance(node, dict) else node):
            if isinstance(value, (dict, list)):
                stack.append(value)
    return found


def cap_json_result(content: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Bring a serialized result under `limit` and keep it valid JSON. In order: shrink the longest strings, shrink
    every string above smaller thresholds, drop trailing list items (leaving a note), and finally replace the
    result with an error carrying a preview. Text that is not a JSON object or list is cut as raw text."""
    if len(content) <= limit:
        return content
    try:
        data = json.loads(content)
    except ValueError:
        return truncate_middle(content, limit)
    if not isinstance(data, (dict, list)):
        return truncate_middle(content, limit)
    for _ in range(50):
        slots = _strings(data)
        if not slots:
            break
        size, node, key = max(slots, key=lambda slot: slot[0])
        excess = len(tool_json(data)) - limit
        if size <= 500 or excess <= 0:
            break
        node[key] = truncate_middle(node[key], max(500, size - excess - 200))
    for threshold in (200, 80):
        if len(tool_json(data)) <= limit:
            return tool_json(data)
        for size, node, key in _strings(data):
            if size > threshold:
                node[key] = truncate_middle(node[key], threshold)
    dropped = 0
    for _ in range(300):
        if len(tool_json(data)) <= limit:
            break
        lists = [items for items in _lists(data) if items]
        if not lists:
            break
        items = max(lists, key=len)
        count = max(1, len(items) // 10)
        del items[-count:]
        dropped += count
    if dropped:
        note = f"{dropped} items dropped: the result was too large"
        if isinstance(data, dict):
            data["_truncated"] = note
        else:
            data.append(note)
    if len(tool_json(data)) <= limit:
        return tool_json(data)
    for share in (4, 20, 200):
        fallback = tool_json({"error": "result too large", "preview": truncate_middle(content, limit // share)})
        if len(fallback) <= limit:
            return fallback
    return tool_json({"error": "result too large"})


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def preview_for(name: str, arguments: str) -> str:
    """What an event shows about a call: never the full arguments, always redacted, at most 500 characters."""
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return ""
    if not isinstance(args, dict):
        return ""
    if name == "terminal":
        text = str(args.get("command") or "")
    elif name == "execute_code":
        text = next((line.strip() for line in str(args.get("code") or "").splitlines() if line.strip()), "")
    elif name == "search_files":
        text = f"{args.get('pattern') or ''} in {args.get('path') or '.'}"
    elif name == "skill_view":
        text = str(args.get("name") or "") + (f" {args['file_path']}" if args.get("file_path") else "")
    elif name == "skills_list":
        text = str(args.get("category") or "")
    else:
        text = str(args.get("path") or "")
    return redact(text[:4 * PREVIEW_CHARS])[:PREVIEW_CHARS]
