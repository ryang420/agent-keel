"""The tools offered to the model, and the one entry point the agent loop calls."""

from __future__ import annotations

from dataclasses import replace
import json
import logging

from keel.sandbox.base import SandboxError
from keel.tools import files, memory, shell, skill_tools  # noqa: F401  (registers their tools)
from keel.tools.base import (
    HANDLERS, MAX_TOOL_RESULT_CHARS, ToolArgumentError, ToolContext, ToolOutcome, cap_json_result, error_result,
)

log = logging.getLogger(__name__)
__all__ = ["ToolContext", "ToolOutcome", "run_tool"]


async def run_tool(name: str, arguments: str, ctx: ToolContext) -> ToolOutcome:
    """Run one tool call. Every failure becomes an error result the model can read; nothing raises except a
    cancellation. Sandbox failures retain an internal marker for coordinated run failure handling."""
    handler = HANDLERS.get(name)
    if handler is None:
        return error_result(f"Unknown tool: {name}")
    try:
        args = json.loads(arguments or "{}")
    except ValueError as exc:
        return error_result(f"Invalid JSON arguments: {exc}")
    if not isinstance(args, dict):
        return error_result("Tool arguments must be a JSON object.")
    if name != "memory" and ctx.backend is None:
        return error_result("This tool needs a sandbox.")
    try:
        outcome = await handler(args, ctx)
    except ToolArgumentError as exc:
        return error_result(str(exc))
    except SandboxError as exc:
        outcome = replace(error_result(f"The sandbox is unavailable: {exc}"), sandbox_error=exc)
    except Exception as exc:
        log.exception("tool %s failed", name)
        return error_result(f"The tool failed: {type(exc).__name__}: {exc}")
    if len(outcome.content) > MAX_TOOL_RESULT_CHARS:
        outcome = replace(outcome, content=cap_json_result(outcome.content))
    return outcome
