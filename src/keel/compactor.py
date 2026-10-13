"""When and how a run compacts its session (M3 design §5): the threshold and overflow triggers, the placeholder-only
pass, the summary call and what it stores. The pure parts are in keel.compress."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import logging
from typing import Awaitable, Callable, Sequence

from keel import compress
from keel.models import Completed, Message, ModelRequest, TextDelta, TextPart, Usage
from keel.providers.retry import stream_with_retries
from keel.store.base import Run, StoredMessage

log = logging.getLogger(__name__)
FAILURE_PAUSE_SECONDS = 600.0


@dataclass
class SessionContext:
    """A run's copy of its session's compaction state, kept in step with what the run stores."""

    bounds: compress.Boundaries
    system: str
    anchor_tokens: int | None = None
    anchor_id: int | None = None
    failed_at: float | None = None


class Compactor:
    def __init__(self, store, *, threshold: float, sleep, now: Callable[[], datetime],
                 emit: Callable[[str, str, dict], Awaitable[object]]) -> None:
        self.store = store
        self.threshold = threshold
        self._sleep = sleep
        self._now = now
        self._emit = emit

    async def prepare(self, run: Run, provider, stored: Sequence[StoredMessage], context: SessionContext, *,
                      tools: tuple[dict, ...], window: int, rebuild: Callable[[], str],
                      overflow: bool = False) -> tuple[list[compress.Item], Usage]:
        """The messages to send next, and the summary call's usage (empty when none was made).

        Below the threshold nothing changes. At the threshold the placeholder pass is applied up to a new tail; if
        that is enough, or summaries are paused after a failure, only the boundary is stored. Otherwise, and always
        after a context_overflow (the estimate was wrong, so it cannot say the pass was enough), one summary call
        condenses the middle."""
        budget = compress.tail_budget(window)
        limit = self.threshold * window
        items = compress.assemble(stored, context.bounds, budget)
        tokens = compress.estimate(items, compress.base_tokens(context.system, tools), context.anchor_tokens,
                                   context.anchor_id)
        if not overflow and tokens < limit:
            return items, Usage()
        if context.bounds.summary is not None:
            head = [item for item in stored if item.id <= context.bounds.head_id]
            rest = [item for item in stored if item.id >= context.bounds.tail_id]
        else:
            end = compress.head_end(stored)
            head, rest = list(stored[:end]), list(stored[end:])
        start = compress.tail_start(rest, budget)
        if start == 0:
            return items, Usage()  # nothing lies between the head and the tail
        tail_id = rest[start].id
        pruned = replace(context.bounds, pruned_id=max(tail_id, context.bounds.pruned_id or 0))
        pruned_items = compress.assemble(stored, pruned, budget)
        now = self._now()
        paused = context.failed_at is not None and now.timestamp() - context.failed_at < FAILURE_PAUSE_SECONDS
        enough = compress.estimate(pruned_items, compress.base_tokens(context.system, tools)) < limit
        if not overflow and (paused or enough):
            await self._prune(run.session_id, context, pruned)
            return pruned_items, Usage()
        middle = [item.message for item in rest[:start]]
        previous, files = compress.split_files(context.bounds.summary)
        system, prompt = compress.summary_prompt(previous, compress.summary_turns(previous, middle),
                                                 today=f"{now:%B} {now.day}, {now.year}")
        request = ModelRequest(provider=run.provider, model=run.model, system=system,
                               messages=(Message("user", (TextPart(prompt),)),),
                               reasoning_effort=run.reasoning_effort)
        try:
            text, usage = await self._summarize(provider, request)
        except Exception:
            log.warning("run %s: the summary call failed; continuing with placeholders", run.id, exc_info=True)
            text, usage = "", Usage()
        if not text:
            await self._prune(run.session_id, context, pruned, failed_at=now.timestamp())
            return pruned_items, usage
        summary = compress.join_files(text, compress.collect_files(middle, files))
        system_prompt = rebuild()
        bounds = compress.Boundaries(summary, head[-1].id, tail_id, tail_id)
        await self.store.compact_session(run.session_id, summary=summary, head_id=bounds.head_id, tail_id=tail_id,
                                         system_prompt=system_prompt)
        context.bounds, context.system = bounds, system_prompt
        context.anchor_tokens = context.anchor_id = context.failed_at = None
        compacted = compress.assemble(stored, bounds, budget)
        await self._emit(run.id, "context.compacted", {
            "messages_before": len(items), "messages_after": len(compacted), "tokens_before": tokens,
            "tokens_after": compress.estimate(compacted, compress.base_tokens(system_prompt, tools)),
            "usage": usage.to_dict()})
        return compacted, usage

    async def _prune(self, session_id: str, context: SessionContext, bounds: compress.Boundaries,
                     failed_at: float | None = None) -> None:
        await self.store.set_pruned(session_id, bounds.pruned_id, failed_at=failed_at)
        context.bounds, context.anchor_tokens, context.anchor_id = bounds, None, None
        if failed_at is not None:
            context.failed_at = failed_at

    async def _summarize(self, provider, request: ModelRequest) -> tuple[str, Usage]:
        text, usage = [], Usage()
        async for event in stream_with_retries(provider, request, sleep=self._sleep):
            if isinstance(event, TextDelta):
                text.append(event.text)
            elif isinstance(event, Completed):
                usage = usage + event.usage
        return "".join(text).strip(), usage
