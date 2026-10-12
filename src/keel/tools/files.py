"""read_file, write_file, patch and search_files. Result shapes follow Hermes's file tools (tools/file_tools.py,
tools/file_operations.py); everything goes through the sandbox backend."""

from __future__ import annotations

import difflib
import json
import re

from keel.tools.base import (
    ToolArgumentError, ToolOutcome, error_result, optional_int, optional_str, require, tool, tool_json,
    truncate_middle,
)

MAX_READ_BYTES = 10 * 1024 * 1024
READ_CHAR_BUDGET = 90_000  # serialized size, leaving room under MAX_TOOL_RESULT_CHARS
MAX_LINE_CHARS = 2000
MAX_MATCH_CHARS = 500
MAX_LINES = 2000
SEARCH_TIMEOUT_S = 60.0
_MATCH = re.compile(r"^(.*?):(\d+):(.*)$")


def _cap(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]} … [{what} truncated, {len(text)} chars]"


def _escaped_len(text: str) -> int:
    """Characters `text` takes inside a JSON string."""
    return len(json.dumps(text, ensure_ascii=False)) - 2


async def _read_bytes(ctx, path: str) -> bytes | ToolOutcome:
    try:
        data = await ctx.backend.read_file(ctx.sandbox, path, MAX_READ_BYTES)
    except FileNotFoundError:
        return error_result(f"File not found: {path}")
    except IsADirectoryError:
        return error_result(f"{path} is a directory; use search_files to list it.")
    except OSError as exc:
        return error_result(f"Cannot read {path}: {exc}")
    if b"\0" in data[:8192]:
        return error_result(f"Cannot read binary file: {path}")
    return data


@tool("read_file")
async def read_file(args: dict, ctx) -> ToolOutcome:
    path = ctx.resolve(require(args, "path", str))
    offset = optional_int(args, "offset", 1, minimum=1)
    limit = min(optional_int(args, "limit", MAX_LINES, minimum=1), MAX_LINES)
    data = await _read_bytes(ctx, path)
    if isinstance(data, ToolOutcome):
        return data
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    start = offset - 1
    numbered, used = [], 0
    for index, line in enumerate(lines[start:start + limit]):
        entry = f"{start + index + 1}|{_cap(line, MAX_LINE_CHARS, 'line')}"
        size = _escaped_len(entry) + 2  # the joining newline is two characters once serialized
        if numbered and used + size > READ_CHAR_BUDGET:
            break
        numbered.append(entry)
        used += size
    end = start + len(numbered)
    result = {"content": "\n".join(numbered), "total_lines": len(lines), "file_size": len(data),
              "truncated": end < len(lines)}
    if end < len(lines):
        result["next_offset"] = end + 1
    return ToolOutcome(tool_json(result))


@tool("write_file")
async def write_file(args: dict, ctx) -> ToolOutcome:
    path = ctx.resolve(require(args, "path", str))
    data = require(args, "content", str).encode("utf-8")
    try:
        await ctx.backend.write_file(ctx.sandbox, path, data)
    except OSError as exc:
        return error_result(f"Cannot write {path}: {exc}")
    return ToolOutcome(tool_json({"bytes_written": len(data), "path": path}))


@tool("patch")
async def patch(args: dict, ctx) -> ToolOutcome:
    path = ctx.resolve(require(args, "path", str))
    old, new = require(args, "old_string", str), require(args, "new_string", str)
    replace_all = args.get("replace_all") is True
    if not old:
        raise ToolArgumentError("old_string must not be empty.")
    if old == new:
        return error_result("new_string must differ from old_string.")
    data = await _read_bytes(ctx, path)
    if isinstance(data, ToolOutcome):
        return data
    text = data.decode("utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        return error_result(f"old_string not found in {path}.")
    if count > 1 and not replace_all:
        return error_result(f"old_string appears {count} times in {path}; include more context or set "
                            "replace_all=true.")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        await ctx.backend.write_file(ctx.sandbox, path, updated.encode("utf-8"))
    except OSError as exc:
        return error_result(f"Cannot write {path}: {exc}")
    diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), updated.splitlines(keepends=True),
                                        fromfile=f"a{path}", tofile=f"b{path}"))
    return ToolOutcome(tool_json({"success": True, "diff": diff, "files_modified": [path]}))


@tool("search_files")
async def search_files(args: dict, ctx) -> ToolOutcome:
    pattern = require(args, "pattern", str)
    target = optional_str(args, "target", "content", ("content", "files"))
    path = ctx.resolve(optional_str(args, "path", "."))
    limit = optional_int(args, "limit", 50, minimum=1)
    offset = optional_int(args, "offset", 0, minimum=0)
    if target == "files":
        result = await ctx.exec(["find", path, "-type", "f", "-name", pattern, "-not", "-path", "*/.git/*"],
                                timeout_s=SEARCH_TIMEOUT_S)
        files = [line for line in result.output.splitlines() if line and not line.startswith("find: ")]
        return ToolOutcome(tool_json({"files": files[offset:offset + limit], "total_count": len(files)}))
    mode = optional_str(args, "output_mode", "content", ("content", "files_only", "count"))
    context = optional_int(args, "context", 0, minimum=0)
    file_glob = args.get("file_glob")
    if file_glob is not None and not isinstance(file_glob, str):
        raise ToolArgumentError("Argument file_glob must be a string.")
    argv = ["grep", "-rIEH", "--exclude-dir=.git"]
    if mode == "files_only":
        argv.append("-l")
    elif mode == "count":
        argv.append("-c")
    else:
        argv.append("-n")
        if context:
            argv += ["-C", str(context)]
    if file_glob:
        argv.append(f"--include={file_glob}")
    result = await ctx.exec([*argv, "--", pattern, path], timeout_s=SEARCH_TIMEOUT_S)
    if result.exit_code not in (0, 1, 2):
        return error_result(f"Search failed: {result.output.strip()[:500]}")
    all_lines = [line for line in result.output.splitlines() if line]
    warnings = [line for line in all_lines if line.startswith("grep: ")]
    lines = [line for line in all_lines if not line.startswith("grep: ")]
    if mode == "files_only":
        found = {"files": lines[offset:offset + limit], "total_count": len(lines)}
    elif mode == "count":
        counts = {}
        for line in lines:
            name, _, number = line.rpartition(":")
            if number.isdigit() and int(number) > 0:
                counts[name] = int(number)
        shown = list(counts.items())[offset:offset + limit]
        found = {"counts": dict(shown), "total_count": sum(counts.values())}
    elif context:
        # offset/limit apply to context groups: the blocks grep separates with "--" lines.
        groups, current = [], []
        for line in lines:
            if line == "--":
                groups.append(current)
                current = []
            else:
                current.append(line)
        groups.append(current)
        groups = [group for group in groups if group]
        total = sum(1 for line in lines if _MATCH.match(line))
        shown = groups[offset:offset + limit]
        found = {"output": truncate_middle("\n--\n".join("\n".join(group) for group in shown)),
                 "total_count": total}
    else:
        matches = [{"path": m[1], "line": int(m[2]), "content": _cap(m[3], MAX_MATCH_CHARS, "match")}
                   for m in map(_MATCH.match, lines) if m]
        found = {"matches": matches[offset:offset + limit], "total_count": len(matches)}
    if result.exit_code == 2:
        if not found["total_count"]:
            return error_result(f"Search failed: {result.output.strip()[:500]}")
    if warnings:
        found["warning"] = "\n".join(warnings)[:500]
    return ToolOutcome(tool_json(found))
