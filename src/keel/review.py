"""Durable memory review (cross-session memory design §4). A worker claims one job, reads a bounded chunk of the
session's indexed text, lets the model edit a staged copy of memory with the memory tool, and commits the edits, the
job and the cursor together. It never appends a message or an event, and it runs beside the run workers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging

from keel import prompts
from keel.compress import message_tokens, text_tokens
from keel.models import (Completed, Message, ModelRequest, ReasoningReady, TextDelta, TextPart, ToolCallReady,
                         Usage)
from keel.providers.base import ModelError
from keel.providers.retry import RETRYABLE, stream_with_retries
from keel.review_input import CHUNK_TOKENS, build_chunk, overlap, render
from keel.review_stage import StagedMemory

log = logging.getLogger(__name__)
ITERATIONS = 4
INPUT_TOKENS = 16_000  # estimated, across one attempt's requests
OUTPUT_TOKENS = 2_000  # per request
DEADLINE_S = 120.0
SOURCE_ROWS = 200


@dataclass(frozen=True)
class ReviewSettings:
    turns: int = 10
    idle_seconds: float = 60.0
    max_age_seconds: float = 600.0
    lease_seconds: float = 30.0
    memory_chars: int = 2200
    user_chars: int = 1375


class _Failed(Exception):
    def __init__(self, code: str, message: str, *, retry: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retry = retry


class _Attempt:
    def __init__(self, number: int) -> None:
        self.number = number
        self.requests = 0
        self.reserved = 0
        self.usage = Usage()

    def entry(self, outcome: str) -> dict:
        return {"attempt": self.number, "outcome": outcome, "requests": self.requests,
                "usage": self.usage.to_dict()}


def chunk_tokens(context_window: int) -> int:
    """New text per attempt: the default, smaller for a model whose window is small."""
    return min(CHUNK_TOKENS, max(500, (context_window - OUTPUT_TOKENS) // 4))


class MemoryReviewer:
    def __init__(self, store, providers, settings: ReviewSettings = ReviewSettings(), *,
                 sleep=asyncio.sleep) -> None:
        self.store = store
        self.providers = providers
        self.settings = settings
        self.sleep = sleep
        self.limits = {"memory": settings.memory_chars, "user": settings.user_chars}

    async def run_once(self, owner: str) -> bool:
        """Claim and run one due job; False when none was due. A cancel (shutdown) gives the job back."""
        s = self.settings
        claim = await self.store.claim_review(owner, s.lease_seconds, turns=s.turns, idle_seconds=s.idle_seconds,
                                              max_age_seconds=s.max_age_seconds)
        if claim is None:
            return False
        attempt = asyncio.create_task(self._attempt(claim), name=f"keel-review-{claim.job.id}")
        try:
            await self._keep_lease(claim, attempt)
        except asyncio.CancelledError:
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)
            try:
                await asyncio.shield(self.store.release_review(claim))
            except Exception:
                log.warning("could not release memory review %s", claim.job.id, exc_info=True)
            raise
        return True

    async def _keep_lease(self, claim, attempt: asyncio.Task) -> None:
        """Renew every third of the lease until the attempt ends; a lost lease cancels it (its commit would be
        refused anyway)."""
        while not attempt.done():
            done, _ = await asyncio.wait({attempt}, timeout=self.settings.lease_seconds / 3)
            if done:
                break
            try:
                renewed = await self.store.renew_review(claim, self.settings.lease_seconds)
            except Exception:
                log.warning("renewing memory review %s failed", claim.job.id, exc_info=True)
                continue
            if not renewed:
                log.warning("memory review %s lost its lease", claim.job.id)
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
                return
        await attempt

    async def _attempt(self, claim) -> None:
        """Never raises except on cancellation: every outcome is recorded on the job."""
        state = _Attempt(claim.job.attempts)
        try:
            async with asyncio.timeout(DEADLINE_S):
                await self._review(claim, state)
            return
        except _Failed as failure:
            code, message, retry = failure.code, str(failure), failure.retry
        except TimeoutError:
            code, message, retry = "deadline", "The review did not finish in time.", True
        except ModelError as error:
            code, message, retry = f"model_{error.code}", str(error), error.code in RETRYABLE
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("memory review %s failed", claim.job.id)
            code, message, retry = "internal", "The review failed with an internal error.", True
        try:
            await self.store.fail_review(claim, code, message, retry=retry, usage=state.entry(code))
        except Exception:
            log.warning("could not record the failure of memory review %s", claim.job.id, exc_info=True)

    async def _review(self, claim, state: _Attempt) -> None:
        job = claim.job
        gone = await self.store.review_owner_gone(claim)
        if gone is not None:
            await self.store.discard_review(claim, gone)
            return
        profile = await self.store.get_profile(job.profile_id)
        if ({"provider": job.provider, "model": job.model} not in profile.models
                or job.provider not in self.providers.names()):
            raise _Failed("model_unavailable", "The run's model is no longer enabled for this profile.", retry=False)
        provider = self.providers.get(job.provider)
        window = provider.capabilities(job.model).context_window
        rows, complete = await self.store.review_rows(claim, limit=SOURCE_ROWS)
        chunk = build_chunk(rows, cursor_message=job.cursor_message, cursor_offset=job.cursor_offset,
                            through=job.source_through, complete=complete, tokens=chunk_tokens(window))
        end = (chunk.end_message, chunk.end_offset)
        if not chunk.segments:
            await self.store.commit_review(claim, {}, end=end, done=chunk.done, evidence=None,
                                           usage=state.entry("empty"))
            return
        slots, revisions = await self.store.review_memory(job.profile_id)
        corrected = await self.store.corrected_texts(job.profile_id, after_message=chunk.last_message,
                                                     after_time=chunk.last_created_at)
        stage = StagedMemory(slots, revisions, self.limits, corrected)
        context = overlap(await self.store.review_context(claim))
        messages = [Message("user", (TextPart(render(context, chunk, stage.items(), self.limits)),))]
        system = prompts.text("memory_review")
        tools = prompts.tool_definitions(("memory",))
        base = text_tokens(system) + text_tokens(json.dumps(tools))
        for _ in range(ITERATIONS):
            estimate = base + sum(message_tokens(message) for message in messages)
            if state.reserved + estimate > INPUT_TOKENS or estimate > window - OUTPUT_TOKENS:
                raise _Failed("budget", "The review ran out of its input budget.", retry=False)
            state.reserved += estimate
            state.requests += 1
            request = ModelRequest(provider=job.provider, model=job.model, system=system, messages=tuple(messages),
                                   tools=tools, reasoning_effort="low", max_output_tokens=OUTPUT_TOKENS,
                                   cache_scope="")
            calls, text, reasoning = [], [], []
            async for event in stream_with_retries(provider, request, sleep=self.sleep,
                                                   context=f"memory review {job.id}"):
                if isinstance(event, TextDelta):
                    text.append(event.text)
                elif isinstance(event, ToolCallReady):
                    calls.append(event.call)
                elif isinstance(event, ReasoningReady):
                    reasoning.append(event.item)
                elif isinstance(event, Completed):
                    state.usage = state.usage + event.usage
            messages.append(Message("assistant", (TextPart("".join(text)),) if text else (), tool_calls=tuple(calls),
                                    reasoning=tuple(reasoning)))
            if not calls:
                outcome = await self.store.commit_review(claim, stage.proposals(), end=end, done=chunk.done,
                                                         evidence=chunk.last_message,
                                                         usage=state.entry("committed"))
                if outcome != "committed":
                    log.info("memory review %s ended %s", job.id, outcome)
                return
            for call in calls:
                if call.name == "memory":
                    content, _ = stage.call(call.arguments)
                else:
                    content = json.dumps({"error": f"Unknown tool: {call.name}"})
                messages.append(Message("tool", (TextPart(content),), tool_call_id=call.id, tool_name=call.name))
        raise _Failed("iteration_limit", "The review did not finish within its iterations.", retry=True)
