"""Execute one run: the agent loop (arch §4, M2 design §1). The model is called with the tools until it answers
in text; each tool call runs in the profile's sandbox and its result is committed before the next call."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import logging
import time
from typing import Callable

from keel import prompts
from keel import compress
from keel.compactor import Compactor, SessionContext
from keel.compress import Boundaries
from keel.models import (
    Completed, ImagePart, Message, ModelRequest, ReasoningReady, TextDelta, TextPart, ToolCall, ToolCallReady, Usage,
    part_from_dict,
)
from keel.prompt import DEFAULT_IDENTITY, build_system_prompt
from keel.providers.base import ModelError, ProviderRegistry
from keel.providers.retry import stream_with_retries
from keel.sandbox.base import PolicyError, SandboxError, parse_policy
from keel.skills import discover_skills
from keel.store.base import Event, Run, StoredMessage, TitleInUse
from keel.titles import derive_title, title_candidates
from keel.tools import ToolContext, ToolOutcome, run_tool
from keel.tools.base import preview_for

log = logging.getLogger(__name__)
DEFAULT_MAX_ITERATIONS = 60
CANCELLED_TOOL_RESULT = "The command was cancelled."
__all__ = ["CANCELLED_TOOL_RESULT", "DEFAULT_IDENTITY", "DEFAULT_MAX_ITERATIONS", "RunExecutor", "RunFailure"]


class RunFailure(Exception):
    """A run-ending failure that is not the provider's, with its error code (e.g. sandbox_unavailable)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _RunState:
    """What the failure and cancel paths need to know about a run in progress."""

    text: list[str] = field(default_factory=list)       # the current model message's text so far
    usage: Usage = field(default_factory=Usage)
    pending: list[ToolCall] = field(default_factory=list)  # committed tool calls that have no result yet
    sandbox: object = None
    exec_id: str | None = None
    open_tool: tuple | None = None  # (call, preview, started at) of a tool that has started and not completed
    streamed: bool = False  # the current model call has produced output, so it must not be retried


@dataclass
class _Turn:
    """What every model call of one run shares."""

    provider: object
    stored: list[StoredMessage]  # the session's messages, kept in step with what the run appends
    context: SessionContext
    tools: tuple[dict, ...]
    window: int
    rebuild: Callable[[], str]
    blobs: dict = field(default_factory=dict)  # image bytes loaded during this run, by blob id


def _unanswered(stored_messages, run_id: str | None = None) -> list[ToolCall]:
    """The tool calls (of one run, if given) that have no stored tool result, in call order. Results count
    whichever run stored them: a turn-start repair stores its results under the next run."""
    answered = {m.message.tool_call_id for m in stored_messages if m.message.role == "tool"}
    return [call for m in stored_messages if m.message.role == "assistant" and run_id in (None, m.run_id)
            for call in m.message.tool_calls if call.id not in answered]


class RunExecutor:
    def __init__(self, store, bus, providers: ProviderRegistry, *, sandbox=None, sleep=asyncio.sleep,
                 max_iterations: int = DEFAULT_MAX_ITERATIONS, mount_root: str | None = None, kill_timeout_s: float = 10.0,
                 skills_ttl_s: float = 60.0, clock=time.monotonic, now=lambda: datetime.now(UTC),
                 compression_threshold: float = 0.75) -> None:
        self.store = store
        self.bus = bus
        self.providers = providers
        self.sandbox = sandbox
        self.max_iterations = max_iterations
        self.mount_root = mount_root
        self.kill_timeout_s = kill_timeout_s
        self.skills_ttl_s = skills_ttl_s
        self._sleep = sleep
        self._now = now
        self._clock = clock
        # (profile, policy digest, enabled skills) -> (expires at, discovered skills): later turns skip discovery
        self._skills_cache: dict[tuple, tuple[float, list]] = {}
        self.compactor = Compactor(store, threshold=compression_threshold, sleep=sleep, now=now, emit=self.emit)

    async def emit(self, run_id: str, type_: str, data: dict) -> Event:
        event = await self.store.append_event(run_id, type_, data)
        await self.bus.notify(run_id)
        return event

    async def _finish(self, run: Run, status: str, *, output: str, usage: Usage, event: str, data: dict,
                      error_code: str | None = None, error: str | None = None) -> None:
        if await self.store.finish_run(run.id, status, output=output, usage=usage, error_code=error_code,
                                       error=error):
            await self.emit(run.id, event, data)

    async def execute(self, run: Run) -> None:
        state = _RunState()
        try:
            await self.emit(run.id, "run.started", {"provider": run.provider, "model": run.model,
                                                    "reasoning_effort": run.reasoning_effort})
            await self._loop(run, state)
        except (ModelError, RunFailure) as error:
            await self._finish(run, "failed", output="".join(state.text), usage=state.usage, event="run.failed",
                               data={"error_code": error.code, "message": str(error)},
                               error_code=error.code, error=str(error))
        except asyncio.CancelledError:
            # Swallowed on purpose: the run ends cancelled and execute returns normally.
            current = asyncio.current_task()
            if current is not None:
                while current.uncancel() > 0:
                    pass
            await self._shielded(run, self._cancel(run, state), "record the cancellation")
        except Exception:
            log.exception("run %s failed with an internal error", run.id)
            await self._shielded(run, self._fail_internal(run, state), "record the failure")

    async def _shielded(self, run: Run, work, what: str) -> None:
        """Run cleanup to completion even if the task is cancelled again meanwhile; those late cancellations
        are absorbed (uncancelled) because execute ends the run itself and returns normally."""
        cleanup = asyncio.ensure_future(work)
        received = 0
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                received += 1
            except Exception:
                log.exception("run %s: could not %s", run.id, what)
        current = asyncio.current_task()
        if current is not None:
            for _ in range(received):
                current.uncancel()

    async def _fail_internal(self, run: Run, state: _RunState) -> None:
        message = "The run failed because of an internal error."
        await self._kill_command(run, state)
        await self._close_tool(run, state)
        await self._answer_pending(run, state)
        await self._finish(run, "failed", output="".join(state.text), usage=state.usage, event="run.failed",
                           data={"error_code": "internal_error", "message": message},
                           error_code="internal_error", error=message)

    async def _kill_command(self, run: Run, state: _RunState) -> None:
        """Kill the command the run is executing; cancelling the docker client alone would leave it running."""
        if not (state.exec_id and state.sandbox is not None and self.sandbox is not None):
            return
        for timeout in (self.kill_timeout_s, 2.0):  # the second try is the last best-effort hard kill
            try:
                await asyncio.wait_for(self.sandbox.kill_tree(state.sandbox, state.exec_id), timeout)
                return
            except Exception:
                log.warning("run %s: could not kill its command", run.id, exc_info=True)

    async def _close_tool(self, run: Run, state: _RunState) -> None:
        """A tool that started but was cut short gets its tool.completed (an error), best effort, so a client that
        follows the events never waits on it."""
        if state.open_tool is None:
            return
        call, preview, started = state.open_tool
        state.open_tool = None
        try:
            await self.emit(run.id, "tool.completed", {"tool_call_id": call.id, "tool": call.name,
                                                       "duration_s": round(time.monotonic() - started, 3),
                                                       "error": True, "preview": preview})
        except Exception:
            log.warning("run %s: could not close tool call %s", run.id, call.id, exc_info=True)

    async def _answer_pending(self, run: Run, state: _RunState) -> None:
        """Answer every committed tool call of this run that has no stored result, so the next turn's request
        stays valid. It reconciles against the stored messages (a cancelled or failed append may or may not have
        committed); state.pending is only the fallback when the store cannot be read."""
        try:
            missing = _unanswered(await self.store.messages(run.session_id), run.id)
        except Exception:
            log.warning("run %s: could not read the transcript; answering from memory", run.id, exc_info=True)
            missing = list(state.pending)
        await self._append_synthetic(run, missing)
        state.pending = []

    async def _append_synthetic(self, run: Run, calls: list[ToolCall], *, strict: bool = False) -> None:
        """Store a cancelled result for each call; best-effort in cleanup, raising when strict."""
        for call in calls:
            try:
                await self.store.append_message(run.session_id, run.id, Message(
                    "tool", (TextPart(CANCELLED_TOOL_RESULT),), tool_call_id=call.id, tool_name=call.name))
            except Exception:
                if strict:
                    raise
                log.warning("run %s: could not answer tool call %s", run.id, call.id, exc_info=True)

    async def _cancel(self, run: Run, state: _RunState) -> None:
        """End a stopped run: kill its running command, answer its unanswered tool calls, finish it cancelled."""
        await self._kill_command(run, state)
        await self._close_tool(run, state)
        await self._answer_pending(run, state)
        await self._finish(run, "cancelled", output="".join(state.text), usage=state.usage, event="run.cancelled",
                           data={})

    async def _loop(self, run: Run, state: _RunState) -> None:
        try:
            provider = self.providers.get(run.provider)
        except KeyError:
            raise ModelError("bad_request", f"The provider {run.provider!r} is not available.")
        profile = await self.store.get_profile(run.profile_id)
        session = await self.store.get_session(run.profile_id, run.session_id)
        policy = None
        if self.sandbox is not None and profile is not None and profile.sandbox:
            try:
                policy = parse_policy(profile.sandbox, self.mount_root)
            except PolicyError as exc:
                raise RunFailure("sandbox_unavailable", f"The sandbox policy is invalid: {exc}") from exc
        ctx, skills = None, []
        if policy is not None:
            try:
                sandbox = await self.sandbox.ensure(run.profile_id, policy)
            except SandboxError as exc:
                raise RunFailure("sandbox_unavailable", str(exc)) from exc
            state.sandbox = sandbox
            if policy.skills_path:
                try:
                    skills = await self._skills(run.profile_id, sandbox, policy, profile.skills)
                except SandboxError as exc:
                    raise RunFailure("sandbox_unavailable", str(exc)) from exc
            ctx = ToolContext(self.sandbox, sandbox, cwd=(session.cwd if session and session.cwd else policy.workdir),
                              skills=tuple(skills), on_exec=self._exec_recorder(run, state))
        saved_cwd = ctx.cwd if ctx else None

        def rebuild() -> str:
            return build_system_prompt(soul=profile.soul if profile else "", model=run.model, provider=run.provider,
                                       now=self._now(), tools=ctx is not None, skills=skills,
                                       workdir=policy.workdir if policy else None)

        system = session.system_prompt if session else None
        if system is None:
            system = (await self.store.set_session_prompt(run.session_id, rebuild(), run.provider,
                                                          run.model)).system_prompt
        stored = await self.store.messages(run.session_id)
        # A run that was swept, crashed or cut short may have left tool calls without results; answer them
        # before this turn so the request pairs every call.
        orphans = _unanswered(stored)
        if orphans:
            await self._append_synthetic(run, orphans, strict=True)  # an unpaired call must not be sent
            stored = await self.store.messages(run.session_id)
        user = Message("user", tuple(part_from_dict(part) for part in run.input))
        stored.append(await self.store.append_message(run.session_id, run.id, user))
        await self._title_from(run, user)
        context = SessionContext(
            bounds=Boundaries.of(session), system=system,
            anchor_tokens=session.context_tokens if session else None,
            anchor_id=session.context_message_id if session else None,
            failed_at=session.compaction_failed_at if session else None)
        turn = _Turn(provider=provider, stored=stored, context=context,
                     tools=prompts.tool_definitions() if ctx is not None else (),
                     window=provider.capabilities(run.model).context_window, rebuild=rebuild)
        for iteration in range(self.max_iterations + 1):
            final = iteration == self.max_iterations and bool(turn.tools)
            calls, reasoning, incomplete = await self._model_call(run, turn, state, final=final)
            output = "".join(state.text)
            assistant = Message("assistant", (TextPart(output),) if output else (), tool_calls=tuple(calls),
                                reasoning=tuple(reasoning))
            # Set before the commit: if the commit lands but the await is cancelled, the calls must still be
            # answered. A failed commit (not a cancel) means nothing was committed, so nothing is pending.
            state.pending = list(calls)
            try:
                stored.append(await self.store.append_message(run.session_id, run.id, assistant))
            except Exception:
                state.pending = []
                raise
            if not calls:
                await self._finish(run, "completed", output=output, usage=state.usage, event="run.completed",
                                   data={"output": output, "usage": state.usage.to_dict(),
                                         **({"incomplete": True} if incomplete else {})})
                return
            for call in calls:
                outcome, duration, preview = await self._call_tool(run, call, ctx, state)
                result = Message("tool", (TextPart(outcome.content),), tool_call_id=call.id, tool_name=call.name)
                # `pending` is only the fallback for cleanup; cleanup reconciles against the stored messages,
                # so whether this append committed before an exception or cancel does not matter.
                state.pending.pop(0)
                try:
                    stored.append(await self.store.append_message(run.session_id, run.id, result))
                except Exception:
                    state.pending.insert(0, call)
                    raise
                state.open_tool = None
                await self.emit(run.id, "tool.completed", {"tool_call_id": call.id, "tool": call.name,
                                                           "duration_s": duration, "error": outcome.error,
                                                           "preview": preview})
                if ctx.cwd != saved_cwd:
                    await self.store.set_session_cwd(run.session_id, ctx.cwd)
                    saved_cwd = ctx.cwd
        raise AssertionError("unreachable: the final iteration offers no tools")

    async def _model_call(self, run: Run, turn: _Turn, state: _RunState, *,
                          final: bool) -> tuple[list[ToolCall], list, bool]:
        """One model call over the assembled history. The compactor runs first, and once more when the call fails
        with context_overflow before any output; the call is then retried once."""
        items, spent = await self.compactor.prepare(run, turn.provider, turn.stored, turn.context, tools=turn.tools,
                                                    window=turn.window, rebuild=turn.rebuild)
        state.usage = state.usage + spent
        retried = False
        while True:
            messages = await self._with_images([item.message for item in items], turn.blobs)
            if final:
                messages.append(Message("user", (TextPart(prompts.MAX_ITERATIONS_REQUEST),)))
            request = ModelRequest(
                provider=run.provider, model=run.model, system=turn.context.system, messages=tuple(messages),
                instructions=run.instructions, tools=() if final else turn.tools,
                reasoning_effort=run.reasoning_effort, cache_scope=run.session_id,
            )
            try:
                calls, reasoning, incomplete, usage = await self._stream(run, turn.provider, request, state)
            except ModelError as error:
                if error.code != "context_overflow" or retried or state.streamed:
                    raise
                retried, before = True, items
                items, spent = await self.compactor.prepare(run, turn.provider, turn.stored, turn.context,
                                                            tools=turn.tools, window=turn.window,
                                                            rebuild=turn.rebuild, overflow=True)
                state.usage = state.usage + spent
                if items == before:
                    raise
                continue
            # No anchor when the newest round was cut: later calls send that round whole, so the count would be low.
            stored = {item.id: item.message for item in turn.stored}
            cut = any(stored.get(item.id) is not item.message for item in items[compress.round_start(items):])
            newest = max((item.id for item in items if item.id is not None), default=None)
            if usage.input_tokens is not None and newest is not None and not cut:
                await self.store.set_context_anchor(run.session_id, usage.input_tokens, newest)
                turn.context.anchor_tokens, turn.context.anchor_id = usage.input_tokens, newest
            return calls, reasoning, incomplete

    async def _stream(self, run: Run, provider, request: ModelRequest,
                      state: _RunState) -> tuple[list[ToolCall], list, bool, Usage]:
        state.text, state.streamed = [], False
        calls, reasoning, incomplete, usage = [], [], False, Usage()
        async for event in stream_with_retries(provider, request, sleep=self._sleep):
            state.streamed = True
            if isinstance(event, TextDelta):
                state.text.append(event.text)
                await self.emit(run.id, "message.delta", {"text": event.text})
            elif isinstance(event, ReasoningReady):
                reasoning.append(event.item)
            elif isinstance(event, ToolCallReady):
                if request.tools:
                    calls.append(event.call)
                else:
                    log.warning("run %s: the model called %r but no tools were offered; ignoring it",
                                run.id, event.call.name)
            elif isinstance(event, Completed):
                usage = usage + event.usage
                state.usage = state.usage + event.usage
                if event.status == "incomplete":
                    incomplete = True
                    log.warning("run %s: the model response was incomplete", run.id)
        return calls, reasoning, incomplete, usage

    async def _skills(self, profile_id: str, sandbox, policy, enabled: list[str]) -> list:
        key = (profile_id, policy.digest(), tuple(enabled))
        cached = self._skills_cache.get(key)
        if cached is not None and self._clock() < cached[0]:
            return cached[1]
        skills = await discover_skills(self.sandbox, sandbox, policy.skills_path, enabled)
        if skills:  # an empty result may be a failed listing; look again next turn
            self._skills_cache[key] = (self._clock() + self.skills_ttl_s, skills)
        else:
            self._skills_cache.pop(key, None)
        return skills

    async def _call_tool(self, run: Run, call: ToolCall, ctx: ToolContext,
                         state: _RunState) -> tuple[ToolOutcome, float, str]:
        preview = preview_for(call.name, call.arguments)
        started = time.monotonic()
        state.open_tool = (call, preview, started)
        await self.emit(run.id, "tool.started", {"tool_call_id": call.id, "tool": call.name, "preview": preview})
        ctx.tool_call_id = call.id
        outcome = await run_tool(call.name, call.arguments, ctx)
        return outcome, round(time.monotonic() - started, 3), preview

    def _exec_recorder(self, run: Run, state: _RunState):
        async def record(exec_id: str | None) -> None:
            state.exec_id = exec_id
            try:
                await self.store.set_current_exec(run.id, exec_id)
            except Exception:
                log.warning("run %s: could not record the current command", run.id, exc_info=True)
        return record

    async def _with_images(self, messages: list[Message], cache: dict) -> list[Message]:
        """Load the bytes of stored image references, each once per run; an image whose blob is gone becomes a
        text note."""
        wanted = {part.blob_id for message in messages for part in message.parts
                  if isinstance(part, ImagePart) and part.blob_id and not part.data_b64}
        if not wanted:
            return list(messages)
        missing = [blob_id for blob_id in wanted if blob_id not in cache]
        if missing:
            found = await self.store.get_blobs(missing)
            cache.update({blob_id: found.get(blob_id) for blob_id in missing})

        def load(part):
            if isinstance(part, ImagePart) and part.blob_id and not part.data_b64:
                found = cache.get(part.blob_id)
                return ImagePart(found[0], found[1], part.blob_id) if found else TextPart("[image no longer available]")
            return part

        return [replace(message, parts=tuple(load(part) for part in message.parts)) for message in messages]

    async def _title_from(self, run: Run, user: Message) -> None:
        session = await self.store.get_session(run.profile_id, run.session_id)
        if session is None or session.title is not None:
            return
        title = derive_title(user.text)
        if not title:
            return
        for candidate in title_candidates(title):
            try:
                await self.store.set_title(run.profile_id, run.session_id, candidate, "derived")
                return
            except TitleInUse:
                continue
        # Ten sessions already share this title; the model title (M3) will name this one.
