"""session_search: find and read this profile's earlier conversations (cross-session memory design §3). The store
checks ownership; this module bounds the output and returns plain data, never instructions. No sandbox."""

from __future__ import annotations

from collections import Counter

# The module, not its names: keel.history imports keel.tools.base, so name imports here would be circular.
from keel import history as h
from keel.tools.base import ToolContext, ToolOutcome, error_result, optional_int, tool, tool_json

DEFAULT_HITS, MAX_HITS, PER_SESSION = 5, 10, 2
DEFAULT_WINDOW, MAX_WINDOW = 4, 10
MESSAGE_CHARS, RESPONSE_CHARS = 4_000, 16_000
NOT_FOUND = "No such session or message in this profile's history."


@tool("session_search", sandbox=False)
async def session_search(args: dict, ctx: ToolContext) -> ToolOutcome:
    if ctx.store is None or not ctx.profile_id:
        return error_result("History search is not available.")
    profile = await ctx.store.get_profile(ctx.profile_id)
    if profile is None or not profile.memory or (
            ctx.profile_incarnation is not None and profile.incarnation != ctx.profile_incarnation):
        return error_result("History search is not available.")
    session_id = args.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return error_result("session_id must be a string.")
    try:
        if args.get("message_id") is not None:
            return await _chunk(args, ctx)
        if args.get("query") is not None:
            return await _search(args, ctx, session_id)
        if args.get("around_message_id") is not None:
            if session_id is None:
                return error_result("around_message_id needs session_id.")
            return await _context(args, ctx, session_id, optional_int(args, "around_message_id", 1, minimum=1))
        if session_id is not None:
            return await _context(args, ctx, session_id, None)
        return await _browse(args, ctx)
    except h.QueryError as exc:
        return error_result(str(exc))


def _bounds(args: dict) -> tuple[float | None, float | None]:
    before = h.parse_time(args["before"], "before") if args.get("before") is not None else None
    after = h.parse_time(args["after"], "after") if args.get("after") is not None else None
    return before, after


async def _search(args: dict, ctx: ToolContext, session_id: str | None) -> ToolOutcome:
    query = args["query"]
    if not isinstance(query, str):
        return error_result("query must be a string.")
    tokens = h.query_tokens(query)
    limit = min(optional_int(args, "limit", DEFAULT_HITS, minimum=1), MAX_HITS)
    before, after = _bounds(args)
    rows = await ctx.store.search_history(
        ctx.profile_id, tokens, session_id=session_id,
        exclude_session_id=None if session_id is not None else (ctx.session_id or None), before=before, after=after)
    wanted = h.phrase(query)
    rows.sort(key=lambda row: (-row.hits, wanted not in h.phrase(row.text), -row.created_at, -row.message_id))
    hits, per_session = [], Counter()
    for row in rows:
        if session_id is None and per_session[row.session_id] >= PER_SESSION:
            continue
        per_session[row.session_id] += 1
        text, truncated = h.excerpt(row.text, tokens)
        hits.append({"session_id": row.session_id, "session_title": row.title, "message_id": row.message_id,
                     "role": row.role, "at": h.iso(row.created_at), "run_status": row.run_status or "unknown",
                     "excerpt": text, "truncated": truncated})
        if len(hits) == limit:
            break
    return ToolOutcome(tool_json({"hits": hits, "index_complete": await ctx.store.history_index_complete(
        ctx.profile_id)}))


def _entry(item, text: str, offset: int = 0) -> dict:
    piece = text[offset:offset + MESSAGE_CHARS]
    end = offset + len(piece)
    return {"message_id": item.id, "role": item.message.role, "at": h.iso(item.created_at),
            "run_status": item.run_status or "unknown", "text": piece, "chars": len(text),
            "next_offset": end if end < len(text) else None}


async def _context(args: dict, ctx: ToolContext, session_id: str, around: int | None) -> ToolOutcome:
    window = min(optional_int(args, "window", DEFAULT_WINDOW, minimum=1), MAX_WINDOW)
    found = await ctx.store.history_context(ctx.profile_id, session_id, around, window)
    if found is None:
        return error_result(NOT_FOUND)
    items, earlier, later = found
    entries = [_entry(item, h.render(item.message)) for item in items]
    anchor = next((i for i, item in enumerate(items) if item.id == around), len(items) - 1)
    # Drop the entries farthest from the anchor until the response fits; their ids become the continuation anchors.
    while sum(len(entry["text"]) for entry in entries) > RESPONSE_CHARS and len(entries) > 1:
        if anchor >= len(entries) - 1 - anchor:
            earlier = entries.pop(0)["message_id"]
            anchor -= 1
        else:
            later = entries.pop()["message_id"]
    return ToolOutcome(tool_json({"session_id": session_id, "messages": entries, "earlier_message_id": earlier,
                                  "later_message_id": later}))


async def _chunk(args: dict, ctx: ToolContext) -> ToolOutcome:
    message_id = optional_int(args, "message_id", 0, minimum=1)
    offset = optional_int(args, "offset", 0, minimum=0)
    item = await ctx.store.history_message(ctx.profile_id, message_id)
    if item is None:
        return error_result(NOT_FOUND)
    entry = _entry(item, h.render(item.message), offset)
    return ToolOutcome(tool_json({"session_id": item.session_id, "offset": offset, **entry}))


async def _browse(args: dict, ctx: ToolContext) -> ToolOutcome:
    limit = min(optional_int(args, "limit", DEFAULT_HITS, minimum=1), MAX_HITS)
    before, after = _bounds(args)
    sessions = await ctx.store.history_sessions(ctx.profile_id, before=before, after=after, limit=limit)
    return ToolOutcome(tool_json({
        "sessions": [{"session_id": s.id, "title": s.title, "created_at": h.iso(s.created_at),
                      "last_active_at": h.iso(s.last_active_at), "current": s.id == ctx.session_id} for s in sessions],
        "index_complete": await ctx.store.history_index_complete(ctx.profile_id)}))
