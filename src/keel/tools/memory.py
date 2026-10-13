"""The memory tool: curated facts for later sessions, applied by the store. No sandbox."""

from __future__ import annotations

from keel.tools.base import ToolContext, ToolOutcome, error_result, tool, tool_json


@tool("memory")
async def memory(args: dict, ctx: ToolContext) -> ToolOutcome:
    if ctx.store is None or not ctx.profile_id:
        return error_result("Memory is not available.")
    target = args.get("target") or "memory"
    limit = ctx.user_chars if target == "user" else ctx.memory_chars
    ops = args.get("operations")
    if ops is None:
        ops = [{key: args.get(key) for key in ("action", "content", "new_text", "old_text")}]
    if not isinstance(ops, list):
        return error_result("operations must be a list.")
    result = await ctx.store.apply_memory(ctx.profile_id, target, limit, ops)
    if result.get("success") and ctx.session_id:
        await ctx.store.reset_memory_turns(ctx.session_id)
    return ToolOutcome(tool_json(result), error=not result.get("success"))
