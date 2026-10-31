"""The dream (procedural learning design §5). A worker claims one profile's dream, reads its pending observations, the
learned skill index with outcome statistics, chosen skill texts and the deployment skill names, lets the model read
history with session_search and stage gated changes, and commits the whole proposal atomically. A proposal cut off by
the deadline, the budget or the iteration limit commits nothing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging

from keel import prompts
from keel.compress import message_tokens, text_tokens
from keel.dream_gates import DreamRules
from keel.dream_input import pick_bodies, render
from keel.dream_stage import STAGED, StagedDream
from keel.models import (Completed, Message, ModelRequest, ReasoningReady, TextDelta, TextPart, ToolCallReady)
from keel.providers.base import ModelError
from keel.providers.retry import RETRYABLE, stream_with_retries
from keel.review import AttemptUsage, JobFailed, keep_lease
from keel.tools.base import ToolArgumentError, ToolContext, tool_json
from keel.tools.history import session_search

log = logging.getLogger(__name__)
INPUT_OBSERVATIONS = 40
TOOLS = ("session_search", *STAGED)


@dataclass(frozen=True)
class DreamSettings:
    iterations: int = 12
    input_tokens: int = 60_000  # estimated, across one attempt's requests
    output_tokens: int = 4_000  # per request
    deadline_seconds: float = 300.0
    lease_seconds: float = 30.0
    rules: DreamRules = field(default_factory=DreamRules)


class Dreamer:
    def __init__(self, store, providers, settings: DreamSettings = DreamSettings(), *, sleep=asyncio.sleep) -> None:
        self.store = store
        self.providers = providers
        self.settings = settings
        self.sleep = sleep

    async def run_once(self, owner: str) -> bool:
        """Claim and run one due dream; False when none was due. A cancel (shutdown) gives the job back."""
        s = self.settings
        claim = await self.store.claim_dream(owner, s.lease_seconds, s.rules)
        if claim is None:
            return False
        attempt = asyncio.create_task(self._attempt(claim), name=f"keel-dream-{claim.job.id}")
        try:
            await keep_lease(attempt, lambda: self.store.renew_dream(claim, s.lease_seconds), s.lease_seconds,
                             f"dream {claim.job.id}")
        except asyncio.CancelledError:
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)
            try:
                await asyncio.shield(self.store.release_dream(claim))
            except Exception:
                log.warning("could not release dream %s", claim.job.id, exc_info=True)
            raise
        return True

    async def _attempt(self, claim) -> None:
        """Never raises except on cancellation: every outcome is recorded on the job."""
        state = AttemptUsage(claim.job.attempts)
        try:
            async with asyncio.timeout(self.settings.deadline_seconds):
                await self._dream(claim, state)
            return
        except JobFailed as failure:
            code, message, retry = failure.code, str(failure), failure.retry
        except TimeoutError:
            code, message, retry = "deadline", "The dream did not finish in time.", True
        except ModelError as error:
            code, message, retry = f"model_{error.code}", str(error), error.code in RETRYABLE
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("dream %s failed", claim.job.id)
            code, message, retry = "internal", "The dream failed with an internal error.", True
        try:
            await self.store.fail_dream(claim, code, message, retry=retry, usage=state.entry(code))
        except Exception:
            log.warning("could not record the failure of dream %s", claim.job.id, exc_info=True)

    async def _dream(self, claim, state: AttemptUsage) -> None:
        job, s = claim.job, self.settings
        gone = await self.store.dream_owner_gone(claim)
        if gone is not None:
            await self.store.discard_dream(claim, gone)
            return
        profile = await self.store.get_profile(job.profile_id)
        if profile is None:
            await self.store.discard_dream(claim, "profile_gone")
            return
        if job.provider is None:
            raise JobFailed("no_route", "The profile has no completed run to take a model from.", retry=False)
        if ({"provider": job.provider, "model": job.model} not in profile.models
                or job.provider not in self.providers.names()):
            raise JobFailed("model_unavailable", "The dream's model is no longer enabled for this profile.",
                            retry=False)
        observations = await self.store.observations(job.profile_id, states=("pending",), limit=INPUT_OBSERVATIONS)
        skills = await self.store.learned_skills(job.profile_id, states=("active", "suspended"))
        stage = StagedDream(observations, skills, set(profile.skills))
        if not observations:  # they expired or were deleted since the job was queued: nothing to do
            await self.store.commit_dream(claim, stage.proposal, usage=state.entry("empty"))
            return
        provider = self.providers.get(job.provider)
        window = provider.capabilities(job.model).context_window
        outcomes = await self.store.skill_text_outcomes(job.profile_id)
        text = render(observations, skills, outcomes, pick_bodies(observations, skills), set(profile.skills))
        messages = [Message("user", (TextPart(text),))]
        history = ToolContext(store=self.store, profile_id=job.profile_id,
                              profile_incarnation=claim.profile_incarnation, allowed=frozenset({"session_search"}))
        system = prompts.text("dream")
        tools = prompts.tool_definitions(TOOLS)
        base = text_tokens(system) + text_tokens(json.dumps(tools))
        for _ in range(s.iterations):
            estimate = base + sum(message_tokens(message) for message in messages)
            if state.reserved + estimate > s.input_tokens or estimate > window - s.output_tokens:
                raise JobFailed("budget", "The dream ran out of its input budget.", retry=True)
            state.reserved += estimate
            state.requests += 1
            request = ModelRequest(provider=job.provider, model=job.model, system=system, messages=tuple(messages),
                                   tools=tools, reasoning_effort="low", max_output_tokens=s.output_tokens,
                                   cache_scope="")
            calls, said, reasoning = [], [], []
            cut_off = False
            async for event in stream_with_retries(provider, request, sleep=self.sleep, context=f"dream {job.id}"):
                if isinstance(event, TextDelta):
                    said.append(event.text)
                elif isinstance(event, ToolCallReady):
                    calls.append(event.call)
                elif isinstance(event, ReasoningReady):
                    reasoning.append(event.item)
                elif isinstance(event, Completed):
                    state.usage = state.usage + event.usage
                    cut_off = cut_off or event.status == "incomplete"
            messages.append(Message("assistant", (TextPart("".join(said)),) if said else (), tool_calls=tuple(calls),
                                    reasoning=tuple(reasoning)))
            if cut_off:
                raise JobFailed("incomplete", "The dream's response was cut off.", retry=True)
            if not calls:
                outcome = await self.store.commit_dream(claim, stage.proposal, usage=state.entry("committed"))
                if outcome != "committed":
                    log.info("dream %s ended %s", job.id, outcome)
                return
            for call in calls:
                if call.name == "session_search":
                    content = await self._search(call.arguments, history)
                elif call.name in STAGED:
                    content, _ = stage.call(call.name, call.arguments)
                else:
                    content = json.dumps({"error": f"Unknown tool: {call.name}"})
                messages.append(Message("tool", (TextPart(content),), tool_call_id=call.id, tool_name=call.name))
        raise JobFailed("iteration_limit", "The dream did not finish within its iterations.", retry=True)

    async def _search(self, arguments: str, ctx: ToolContext) -> str:
        """session_search scoped by trusted context: this profile's history, redacted, as data."""
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return tool_json({"error": "Arguments must be a JSON object."})
        if not isinstance(args, dict):
            return tool_json({"error": "Arguments must be a JSON object."})
        try:
            return (await session_search(args, ctx)).content
        except ToolArgumentError as exc:
            return tool_json({"error": str(exc)})
