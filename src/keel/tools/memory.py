"""The memory tool: curated facts for later sessions, applied by the store with the run as provenance. No sandbox."""

from __future__ import annotations

import hashlib
import json

from keel.store.base import MemorySource
from keel.tools.base import ToolContext, ToolOutcome, error_result, tool, tool_json

_OP_KEYS = ("action", "content", "new_text", "old_text", "entry_id", "expected_version")


@tool("memory")
async def memory(args: dict, ctx: ToolContext) -> ToolOutcome:
    if ctx.store is None or not ctx.profile_id:
        return error_result("Memory is not available.")
    profile = await ctx.store.get_profile(ctx.profile_id)
    if profile is None or not profile.memory:
        return error_result("Memory is not available.")
    if args.get("action") == "list":
        return ToolOutcome(tool_json(await _listing(ctx)))
    target = args.get("target") or "memory"
    limit = ctx.user_chars if target == "user" else ctx.memory_chars
    ops = args.get("operations")
    if ops is None:
        ops = [{key: args.get(key) for key in _OP_KEYS if args.get(key) is not None}]
    if not isinstance(ops, list):
        return error_result("operations must be a list.")
    message_id = None
    if ctx.run_id and ctx.session_id:
        message_id = await ctx.store.run_user_message_id(ctx.session_id, ctx.run_id)
    source = MemorySource(ctx.memory_source, run_id=ctx.run_id or None, session_id=ctx.session_id or None,
                          message_id=message_id, tool_call_id=ctx.tool_call_id or None)
    key = None
    if ctx.run_id and ctx.tool_call_id:
        payload = json.dumps({"target": target, "ops": ops}, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        key = f"{ctx.memory_source}:{ctx.run_id}:{ctx.tool_call_id}:{digest}"
    result = await ctx.store.apply_memory(ctx.profile_id, target, limit, ops, source=source, mutation_key=key,
                                         profile_incarnation=ctx.profile_incarnation)
    if result.get("success") and ctx.session_id:
        await ctx.store.reset_memory_turns(ctx.session_id, profile_incarnation=ctx.profile_incarnation)
    return ToolOutcome(tool_json(result), error=not result.get("success"))


async def _listing(ctx: ToolContext) -> dict:
    from keel.memory import joined  # keel.memory imports keel.tools.base, so import it late

    items = await ctx.store.memory_items(ctx.profile_id)
    listing = {}
    for target, limit in (("memory", ctx.memory_chars), ("user", ctx.user_chars)):
        mine = [{"id": item.id, "text": item.text, "version": item.version} for item in items if item.target == target]
        used = len(joined([entry["text"] for entry in mine]))
        listing[target] = {"usage": f"{used}/{limit}", "items": mine}
    return listing
