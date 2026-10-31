"""One memory review. It sees the session's prompt and the history a model call would (assembled bounds, loaded
images) so the call can hit the cache, and it writes memory only. It never appends a message or an event."""

from __future__ import annotations

from keel import compress, prompts
from keel.models import Message, ModelRequest, ReasoningReady, TextDelta, TextPart, ToolCallReady
from keel.providers.retry import stream_with_retries
from keel.tools import ToolContext, run_tool

REVIEW_ITERATIONS = 4
_running: set[str] = set()


def claim(profile_id: str) -> bool:
    """True when this profile has no review running. Called before spawn, so two completions cannot both start one."""
    if profile_id in _running:
        return False
    _running.add(profile_id)
    return True


def release(profile_id: str) -> None:
    _running.discard(profile_id)


async def run_review(executor, run) -> None:
    try:
        await _review(executor, run)
    finally:
        release(run.profile_id)


async def _review(executor, run) -> None:
    if run.profile_incarnation is None:
        return
    profile = await executor.store.get_profile(run.profile_id)
    if profile is None or (run.profile_incarnation is not None
                           and profile.incarnation != run.profile_incarnation):
        return
    session = await executor.store.get_session(run.profile_id, run.session_id)
    if session is None or not session.system_prompt:
        return
    provider = executor.providers.get(run.provider)
    stored = await executor.store.messages(run.session_id)
    budget = compress.tail_budget(provider.capabilities(run.model).context_window)
    items = compress.assemble(stored, compress.Boundaries.of(session), budget)
    messages = await executor._with_images([item.message for item in items], {},
                                           profile_id=run.profile_id, session_id=run.session_id)
    messages.append(Message("user", (TextPart(prompts.text("memory_review")),)))
    ctx = ToolContext(store=executor.store, profile_id=run.profile_id,
                      profile_incarnation=run.profile_incarnation, session_id=run.session_id,
                      run_id=run.id, memory_source="review", memory_chars=executor.memory_chars,
                      user_chars=executor.user_chars, allowed=frozenset({"memory"}))
    tools = prompts.tool_definitions(("memory",))
    for _ in range(REVIEW_ITERATIONS):
        request = ModelRequest(provider=run.provider, model=run.model, system=session.system_prompt,
                               messages=tuple(messages), tools=tools, reasoning_effort="low",
                               cache_scope=run.session_id)
        calls, text, reasoning = [], [], []
        async for event in stream_with_retries(provider, request, sleep=executor._sleep,
                                                 context=f"run {run.id} memory review"):
            if isinstance(event, TextDelta):
                text.append(event.text)
            elif isinstance(event, ReasoningReady):
                reasoning.append(event.item)
            elif isinstance(event, ToolCallReady):
                calls.append(event.call)
        messages.append(Message("assistant", (TextPart("".join(text)),) if text else (), tool_calls=tuple(calls),
                                reasoning=tuple(reasoning)))
        if not calls:
            return
        for call in calls:
            ctx.tool_call_id = call.id
            outcome = await run_tool(call.name, call.arguments, ctx)
            messages.append(Message("tool", (TextPart(outcome.content),), tool_call_id=call.id, tool_name=call.name))
