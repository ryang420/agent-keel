"""skill_manage: the foreground agent keeps a method as a learned skill at the user's explicit request (learning
design §3). The store applies the change with the run as provenance; the model cannot name another owner. No
sandbox."""

from __future__ import annotations

import hashlib
import json

from keel.store.base import ProfileIncarnationMismatch, SkillSource
from keel.tools.base import ToolContext, ToolOutcome, error_result, tool, tool_json

_ACTIONS = ("create", "revise", "retire")
_KEYS = ("action", "skill_id", "expected_version", "name", "description", "body", "category")
_UNAVAILABLE = "Learned skills are not available."


@tool("skill_manage", sandbox=False)
async def skill_manage(args: dict, ctx: ToolContext) -> ToolOutcome:
    if ctx.store is None or not ctx.profile_id:
        return error_result(_UNAVAILABLE)
    profile = await ctx.store.get_profile(ctx.profile_id)
    if profile is None or not profile.learning or (
            ctx.profile_incarnation is not None and profile.incarnation != ctx.profile_incarnation):
        return error_result(_UNAVAILABLE)
    action = args.get("action")
    if action == "list":
        skills = await ctx.store.learned_skills(ctx.profile_id, states=("active", "suspended"))
        return ToolOutcome(tool_json({"success": True, "skills": [skill.summary() for skill in skills]}))
    if action not in _ACTIONS:
        return error_result("action must be one of list, create, revise or retire.")
    op = {key: args[key] for key in _KEYS if key in args}
    message_id = None
    if ctx.run_id and ctx.session_id:
        message_id = await ctx.store.run_user_message_id(ctx.session_id, ctx.run_id)
    source = SkillSource("foreground", run_id=ctx.run_id or None, session_id=ctx.session_id or None,
                         message_id=message_id, tool_call_id=ctx.tool_call_id or None)
    key = None
    if ctx.run_id and ctx.tool_call_id:
        digest = hashlib.sha256(json.dumps(op, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        key = f"skill:{ctx.run_id}:{ctx.tool_call_id}:{digest}"
    try:
        result = await ctx.store.change_learned_skill(ctx.profile_id, op, source=source, mutation_key=key,
                                                      profile_incarnation=ctx.profile_incarnation,
                                                      require_learning=True)
    except ProfileIncarnationMismatch:
        return error_result(_UNAVAILABLE)
    return ToolOutcome(tool_json(result), error=not result.get("success"))
