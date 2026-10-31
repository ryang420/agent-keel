"""Durable memory review (cross-session memory design §4). A worker claims one job, reads a bounded chunk of the
session's indexed text, lets the model edit a staged copy of memory with the memory tool, and commits the edits, the
job and the cursor together (and, when learning is on, stages lesson observations committed with it). It never
appends a message or an event, and it runs beside the run workers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging

from keel import prompts
from keel.compress import message_tokens, text_tokens
from keel.models import (Completed, Message, ModelRequest, ReasoningReady, TextDelta, TextPart, ToolCallReady,
                         Usage)
from keel.observations import MAX_PER_JOB
from keel.providers.base import ModelError
from keel.providers.retry import RETRYABLE, stream_with_retries
from keel.review_input import CHUNK_TOKENS, build_chunk, overlap, render
from keel.review_observe import StagedObservations
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


class JobFailed(Exception):
    def __init__(self, code: str, message: str, *, retry: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retry = retry


class AttemptUsage:
    def __init__(self, number: int) -> None:
        self.number = number
        self.requests = 0
        self.reserved = 0
        self.usage = Usage()

    def entry(self, outcome: str) -> dict:
        return {"attempt": self.number, "outcome": outcome, "requests": self.requests,
                "usage": self.usage.to_dict()}


async def keep_lease(attempt: asyncio.Task, renew, lease_seconds: float, what: str) -> None:
    """Renew every third of the lease until the attempt ends; a lost lease cancels it (its commit would be refused
    anyway). `renew` returns whether the lease is still held."""
    while not attempt.done():
        done, _ = await asyncio.wait({attempt}, timeout=lease_seconds / 3)
        if done:
            break
        try:
            renewed = await renew()
        except Exception:
            log.warning("renewing %s failed", what, exc_info=True)
            continue
        if not renewed:
            log.warning("%s lost its lease", what)
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)
            return
    await attempt


async def run_attempt(work, *, deadline: float, state: AttemptUsage, fail, what: str, noun: str) -> None:
    """Run one attempt's coroutine under its deadline. A failure becomes (code, message, retry), recorded through
    `fail(code, message, retry, usage)`; only a cancel propagates."""
    try:
        async with asyncio.timeout(deadline):
            await work
        return
    except JobFailed as failure:
        code, message, retry = failure.code, str(failure), failure.retry
    except TimeoutError:
        code, message, retry = "deadline", f"The {noun} did not finish in time.", True
    except ModelError as error:
        code, message, retry = f"model_{error.code}", str(error), error.code in RETRYABLE
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("%s failed", what)
        code, message, retry = "internal", f"The {noun} failed with an internal error.", True
    try:
        await fail(code, message, retry, state.entry(code))
    except Exception:
        log.warning("could not record the failure of %s", what, exc_info=True)


async def collect(provider, request, *, sleep, context: str, state: AttemptUsage) -> tuple[list, list, list, bool]:
    """Stream one request: its tool calls, text parts and reasoning items, and whether it was cut off. Usage is
    added to the attempt."""
    calls, text, reasoning = [], [], []
    cut_off = False
    async for event in stream_with_retries(provider, request, sleep=sleep, context=context):
        if isinstance(event, TextDelta):
            text.append(event.text)
        elif isinstance(event, ToolCallReady):
            calls.append(event.call)
        elif isinstance(event, ReasoningReady):
            reasoning.append(event.item)
        elif isinstance(event, Completed):
            state.usage = state.usage + event.usage
            cut_off = cut_off or event.status == "incomplete"
    return calls, text, reasoning, cut_off


def chunk_tokens(context_window: int) -> int:
    """New text per attempt: the default, smaller for a model whose window is small."""
    return min(CHUNK_TOKENS, max(500, (context_window - OUTPUT_TOKENS) // 4))


def attempt_tokens(window: int, attempts: int) -> int:
    """Each retry takes half the text of the one before, so a job that ran out of budget converges."""
    return max(250, chunk_tokens(window) // 2 ** max(0, attempts - 1))


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
            await keep_lease(attempt, lambda: self.store.renew_review(claim, s.lease_seconds), s.lease_seconds,
                             f"memory review {claim.job.id}")
        except asyncio.CancelledError:
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)
            try:
                await asyncio.shield(self.store.release_review(claim))
            except Exception:
                log.warning("could not release memory review %s", claim.job.id, exc_info=True)
            raise
        return True

    async def _attempt(self, claim) -> None:
        """Never raises except on cancellation: every outcome is recorded on the job."""
        state = AttemptUsage(claim.job.attempts)
        await run_attempt(self._review(claim, state), deadline=DEADLINE_S, state=state,
                          fail=lambda code, message, retry, usage: self.store.fail_review(
                              claim, code, message, retry=retry, usage=usage),
                          what=f"memory review {claim.job.id}", noun="review")

    async def _review(self, claim, state: AttemptUsage) -> None:
        job = claim.job
        gone = await self.store.review_owner_gone(claim)
        if gone is not None:
            await self.store.discard_review(claim, gone)
            return
        profile = await self.store.get_profile(job.profile_id)
        if profile is None:
            await self.store.discard_review(claim, "profile_gone")
            return
        if ({"provider": job.provider, "model": job.model} not in profile.models
                or job.provider not in self.providers.names()):
            raise JobFailed("model_unavailable", "The run's model is no longer enabled for this profile.", retry=False)
        provider = self.providers.get(job.provider)
        window = provider.capabilities(job.model).context_window
        rows, complete = await self.store.review_rows(claim, limit=SOURCE_ROWS)
        chunk = build_chunk(rows, cursor_message=job.cursor_message, cursor_offset=job.cursor_offset,
                            through=job.source_through, complete=complete, tokens=attempt_tokens(window, job.attempts))
        end = (chunk.end_message, chunk.end_offset)
        if not chunk.segments:
            await self.store.commit_review(claim, {}, end=end, done=chunk.done, evidence=None,
                                           usage=state.entry("empty"))
            return
        slots, revisions = await self.store.review_memory(job.profile_id)
        corrected = await self.store.corrected_texts(job.profile_id, after_message=chunk.last_message,
                                                     after_time=chunk.last_created_at)
        learning = bool(profile.memory and profile.learning)
        stage = StagedMemory(slots, revisions, self.limits, corrected)
        context = overlap(await self.store.review_context(claim))
        messages = [Message("user", (TextPart(render(context, chunk, stage.items(), self.limits)),))]
        observed = None
        if learning:
            reviewed = {segment.message_id for segment in chunk.segments}
            shown = {segment.message_id for segment in context}
            observed = StagedObservations(
                reviewed=reviewed, context=shown - reviewed,
                finished=await self.store.review_message_runs(job.profile_id, job.session_id, reviewed | shown),
                requested=await self.store.requested_message_ids(job.profile_id, job.session_id),
                remaining=MAX_PER_JOB - await self.store.review_observation_count(job.profile_id, job.id))
        system = prompts.text("memory_review")
        if observed is not None:
            system += "\n\n" + prompts.text("memory_review_learning")
        tools = prompts.tool_definitions(("memory", "observe") if observed is not None else ("memory",))
        base = text_tokens(system) + text_tokens(json.dumps(tools))
        for _ in range(ITERATIONS):
            estimate = base + sum(message_tokens(message) for message in messages)
            if state.reserved + estimate > INPUT_TOKENS or estimate > window - OUTPUT_TOKENS:
                raise JobFailed("budget", "The review ran out of its input budget.", retry=True)
            state.reserved += estimate
            state.requests += 1
            request = ModelRequest(provider=job.provider, model=job.model, system=system, messages=tuple(messages),
                                   tools=tools, reasoning_effort="low", max_output_tokens=OUTPUT_TOKENS,
                                   cache_scope="")
            calls, text, reasoning, cut_off = await collect(provider, request, sleep=self.sleep,
                                                            context=f"memory review {job.id}", state=state)
            messages.append(Message("assistant", (TextPart("".join(text)),) if text else (), tool_calls=tuple(calls),
                                    reasoning=tuple(reasoning)))
            if cut_off:
                raise JobFailed("incomplete", "The review's response was cut off.", retry=True)
            if not calls:
                outcome = await self.store.commit_review(claim, stage.proposals(), end=end, done=chunk.done,
                                                         evidence=chunk.last_message,
                                                         usage=state.entry("committed"),
                                                         observations=tuple(observed.items) if observed else ())
                if outcome != "committed":
                    log.info("memory review %s ended %s", job.id, outcome)
                return
            for call in calls:
                if call.name == "memory":
                    content, _ = stage.call(call.arguments)
                elif call.name == "observe" and observed is not None:
                    content, _ = observed.call(call.arguments)
                else:
                    content = json.dumps({"error": f"Unknown tool: {call.name}"})
                messages.append(Message("tool", (TextPart(content),), tool_call_id=call.id, tool_name=call.name))
        raise JobFailed("iteration_limit", "The review did not finish within its iterations.", retry=True)
