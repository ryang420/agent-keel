import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from keel import prompts
from keel.events import InMemoryEventBus
from keel.models import Completed, ToolCall, ToolCallReady, TextDelta, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.sandbox.base import SandboxError
from keel.sandbox.local import LocalSandbox


async def no_sleep(seconds):
    pass


def call(name, call_id="c1", **args):
    return ToolCallReady(ToolCall(call_id, name, json.dumps(args)))


def answer(text="done"):
    return [TextDelta(text), Completed(Usage(10, 2, 0, 0))]


@pytest.fixture
async def loop_parts(store, tmp_path):
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    work = tmp_path / "work"
    skills = tmp_path / "skills" / "tools" / "helper"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: helper\ndescription: Helps.\n---\nUse it.\n")
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="SOUL",
                               skills=["helper"], sandbox={"image": "local", "workdir": str(work),
                                                           "skills_path": str(tmp_path / "skills")})
    executor = RunExecutor(store, InMemoryEventBus(), registry, sandbox=LocalSandbox(kill_grace_s=0.5),
                           sleep=no_sleep, max_iterations=3, now=lambda: datetime(2026, 10, 12, tzinfo=UTC))
    return store, provider, executor, work


async def start(store, text="hi", session="s1", instructions=None):
    await store.ensure_session("p1", session)
    await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": text}], instructions=instructions)
    return await store.claim_run("w1", 9_999_999)


async def test_a_tool_call_runs_and_its_result_feeds_the_next_call(loop_parts):
    store, provider, executor, work = loop_parts
    provider.script = [[call("terminal", command="echo keel-$((6*7))"), Completed(Usage(20, 5, 0, 0))],
                       answer("It printed keel-42")]
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.output) == ("completed", "It printed keel-42")
    assert done.usage == Usage(30, 7, 0, 0)
    messages = [m.message for m in await store.messages("s1")]
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls[0].name == "terminal"
    assert messages[2].tool_call_id == "c1" and messages[2].tool_name == "terminal"
    assert json.loads(messages[2].text)["output"] == "keel-42"
    second = provider.requests[1]
    assert [m.role for m in second.messages] == ["user", "assistant", "tool"]
    assert [t["name"] for t in second.tools] == list(prompts.TOOL_ORDER)
    events = [(e.type, e.data) for e in await store.events_after(run.id, 0)]
    types = [t for t, _ in events]
    assert types == ["run.started", "tool.started", "tool.completed", "message.delta", "run.completed"]
    assert events[1][1] == {"tool_call_id": "c1", "tool": "terminal", "preview": "echo keel-$((6*7))"}
    assert events[2][1]["error"] is False and events[2][1]["duration_s"] >= 0


async def test_the_system_prompt_is_built_once_and_reused(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [answer(), answer()]
    await executor.execute(await start(store, "first", instructions="A"))
    await executor.execute(await start(store, "second", instructions="B"))
    first, second = provider.requests
    assert first.system == second.system and first.tools == second.tools
    assert first.system.startswith("SOUL\n\n") and "- helper: Helps." in first.system
    assert (first.instructions, second.instructions) == ("A", "B")
    assert (await store.get_session("p1", "s1")).system_prompt == first.system


async def test_the_iteration_cap_ends_with_a_tool_less_call(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[call("terminal", f"c{i}", command="true"), Completed(Usage(1, 1, 0, 0))] for i in range(3)]
    provider.script.append(answer("summary"))
    run = await start(store)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).output == "summary"
    last = provider.requests[-1]
    assert len(provider.requests) == 4 and last.tools == ()
    assert last.messages[-1].role == "user" and last.messages[-1].text == prompts.MAX_ITERATIONS_REQUEST


async def test_bad_tool_calls_are_answered_and_the_run_continues(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[ToolCallReady(ToolCall("c1", "no_such_tool", "{}")),
                        ToolCallReady(ToolCall("c2", "read_file", "{oops")), Completed(Usage(1, 1, 0, 0))],
                       answer("recovered")]
    run = await start(store)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).status == "completed"
    tools = [m.message for m in await store.messages("s1") if m.message.role == "tool"]
    assert "Unknown tool" in tools[0].text and "Invalid JSON" in tools[1].text
    completed = [e.data for e in await store.events_after(run.id, 0) if e.type == "tool.completed"]
    assert [c["error"] for c in completed] == [True, True]


async def test_the_working_directory_carries_across_runs(loop_parts):
    store, provider, executor, work = loop_parts
    (work / "sub").mkdir(parents=True)
    provider.script = [[call("terminal", command="cd sub"), Completed(Usage(1, 1, 0, 0))], answer(),
                       [call("terminal", command="pwd"), Completed(Usage(1, 1, 0, 0))], answer()]
    await executor.execute(await start(store, "one"))
    await executor.execute(await start(store, "two"))
    pwd = [m.message for m in await store.messages("s1") if m.message.role == "tool"][-1]
    assert Path(json.loads(pwd.text)["output"].strip()).resolve() == (work / "sub").resolve()


async def test_a_sandbox_that_cannot_start_fails_the_run(store):
    class Broken(LocalSandbox):
        async def ensure(self, profile_id, policy):
            raise SandboxError("Could not create the sandbox: no such image")

    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "missing", "workdir": "/w"})
    executor = RunExecutor(store, InMemoryEventBus(), registry, sandbox=Broken(), sleep=no_sleep)
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.error_code) == ("failed", "sandbox_unavailable")
    assert provider.requests == []


async def test_a_profile_without_a_sandbox_gets_no_tools(store):
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[], sandbox={})
    executor = RunExecutor(store, InMemoryEventBus(), registry, sandbox=LocalSandbox(), sleep=no_sleep)
    await executor.execute(await start(store))
    assert provider.requests[0].tools == () and provider.requests[0].system == "You are a helpful assistant."


async def wait_for_event(store, run_id, type_, seconds=5.0, count=1):
    for _ in range(int(seconds / 0.02)):
        if sum(e.type == type_ for e in await store.events_after(run_id, 0)) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {type_} event")


async def test_cancelling_during_a_command_kills_it_and_answers_the_calls(loop_parts):
    store, provider, executor, work = loop_parts
    provider.script = [[call("terminal", "c1", command="(sleep 1; touch late) & sleep 30"),
                        call("terminal", "c2", command="echo second"), Completed(Usage(1, 1, 0, 0))],
                       answer("after")]
    run = await start(store)
    task = asyncio.create_task(executor.execute(run))
    await wait_for_event(store, run.id, "tool.started")
    await asyncio.sleep(0.3)
    started = asyncio.get_running_loop().time()
    task.cancel()
    await task
    assert asyncio.get_running_loop().time() - started < 5
    assert (await store.get_run("p1", run.id)).status == "cancelled"
    await asyncio.sleep(1.5)
    assert not (work / "late").exists()
    tools = [m.message for m in await store.messages("s1") if m.message.role == "tool"]
    assert [(t.tool_call_id, t.text) for t in tools] == [("c1", "The command was cancelled."),
                                                         ("c2", "The command was cancelled.")]
    await executor.execute(await start(store, "again"))
    roles = [m.role for m in provider.requests[-1].messages]
    assert roles == ["user", "assistant", "tool", "tool", "user"]


async def tool_results(store):
    return [(m.message.tool_call_id, m.message.text) for m in await store.messages("s1")
            if m.message.role == "tool"]


async def test_an_internal_error_answers_the_unanswered_calls(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[call("terminal", "c1", command="true"), call("terminal", "c2", command="true"),
                        Completed(Usage(1, 1, 0, 0))], answer("next")]
    real = executor.emit

    async def flaky(run_id, type_, data):
        if type_ == "tool.completed":
            raise RuntimeError("boom")
        return await real(run_id, type_, data)

    executor.emit = flaky
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.error_code) == ("failed", "internal_error")
    results = await tool_results(store)
    assert [r[0] for r in results] == ["c1", "c2"]
    assert results[1][1] == "The command was cancelled."
    executor.emit = real
    await executor.execute(await start(store, "again"))
    assert [m.role for m in provider.requests[-1].messages] == ["user", "assistant", "tool", "tool", "user"]


async def test_a_cancel_right_after_the_assistant_commit_answers_its_calls(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[call("terminal", "c1", command="true"), call("terminal", "c2", command="true"),
                        Completed(Usage(1, 1, 0, 0))]]
    real = store.append_message

    async def append(session_id, run_id, message):
        stored = await real(session_id, run_id, message)
        if message.role == "assistant" and message.tool_calls:
            raise asyncio.CancelledError()
        return stored

    store.append_message = append
    run = await start(store)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).status == "cancelled"
    assert [r[0] for r in await tool_results(store)] == ["c1", "c2"]


async def test_a_failed_assistant_commit_leaves_no_synthetic_results(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[call("terminal", "c1", command="true"), Completed(Usage(1, 1, 0, 0))]]
    real = store.append_message

    async def append(session_id, run_id, message):
        if message.role == "assistant":
            raise RuntimeError("db down")
        return await real(session_id, run_id, message)

    store.append_message = append
    run = await start(store)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).error_code == "internal_error"
    assert await tool_results(store) == []


async def test_tool_completed_is_emitted_after_the_result_is_committed(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[call("terminal", command="true"), Completed(Usage(1, 1, 0, 0))], answer()]
    seen = []
    real = executor.emit

    async def spy(run_id, type_, data):
        if type_ == "tool.completed":
            seen.append(len(await tool_results(store)))
        return await real(run_id, type_, data)

    executor.emit = spy
    await executor.execute(await start(store))
    assert seen == [1]


async def test_the_working_directory_is_saved_even_if_the_run_is_cancelled_in_the_same_iteration(loop_parts):
    store, provider, executor, work = loop_parts
    (work / "sub").mkdir(parents=True)
    provider.script = [[call("terminal", "c1", command="cd sub"), call("terminal", "c2", command="sleep 30"),
                        Completed(Usage(1, 1, 0, 0))]]
    run = await start(store)
    task = asyncio.create_task(executor.execute(run))
    await wait_for_event(store, run.id, "tool.started", count=2)
    task.cancel()
    await task
    cwd = (await store.get_session("p1", "s1")).cwd
    assert Path(cwd).resolve() == (work / "sub").resolve()


async def test_a_skill_listing_that_cannot_run_fails_the_run_as_sandbox_unavailable(store, tmp_path):
    class NoFind(LocalSandbox):
        async def exec(self, sandbox, argv, **kwargs):
            if "keel-discover" in argv:
                raise SandboxError("the sandbox went away")
            return await super().exec(sandbox, argv, **kwargs)

    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=["helper"],
                               sandbox={"image": "local", "workdir": str(tmp_path / "w"),
                                        "skills_path": str(tmp_path / "skills")})
    executor = RunExecutor(store, InMemoryEventBus(), registry, sandbox=NoFind(), sleep=no_sleep)
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.error_code) == ("failed", "sandbox_unavailable")


async def test_a_cancel_before_a_result_commits_answers_that_call_exactly_once(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[call("terminal", "c1", command="true"), call("terminal", "c2", command="true"),
                        Completed(Usage(1, 1, 0, 0))]]
    real = store.append_message
    fired = []

    async def append(session_id, run_id, message):
        if message.role == "tool" and message.tool_call_id == "c1" and not fired:
            fired.append(1)
            raise asyncio.CancelledError()
        return await real(session_id, run_id, message)

    store.append_message = append
    run = await start(store)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).status == "cancelled"
    assert [r[0] for r in await tool_results(store)] == ["c1", "c2"]


async def test_a_result_that_committed_before_an_exception_is_not_duplicated(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[call("terminal", "c1", command="true"), call("terminal", "c2", command="true"),
                        Completed(Usage(1, 1, 0, 0))]]
    real = store.append_message

    async def append(session_id, run_id, message):
        stored = await real(session_id, run_id, message)
        if message.role == "tool" and message.tool_call_id == "c1" and message.text != "The command was cancelled.":
            raise RuntimeError("connection lost after commit")
        return stored

    store.append_message = append
    run = await start(store)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).error_code == "internal_error"
    assert [r[0] for r in await tool_results(store)] == ["c1", "c2"]


async def test_a_new_turn_answers_calls_a_dead_run_left_open(loop_parts):
    from keel.models import Message, TextPart

    store, provider, executor, _ = loop_parts
    await store.ensure_session("p1", "s1")
    await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": "old"}], instructions=None)
    old = await store.claim_run("w0", 9_999_999)
    await store.append_message("s1", old.id, Message("user", (TextPart("old"),)))
    await store.append_message("s1", old.id, Message("assistant", (), tool_calls=(ToolCall("x1", "terminal", "{}"),)))
    await store.finish_run(old.id, "failed", output="", usage=Usage(), error_code="lost", error="swept")
    provider.script = [answer()]
    await executor.execute(await start(store, "new"))
    roles = [m.role for m in provider.requests[0].messages]
    assert roles == ["user", "assistant", "tool", "user"]
    assert await tool_results(store) == [("x1", "The command was cancelled.")]


async def test_a_cancel_during_internal_error_cleanup_still_finishes_the_run(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[call("terminal", "c1", command="true"), call("terminal", "c2", command="true"),
                        Completed(Usage(1, 1, 0, 0))]]
    real = executor.emit
    reached = asyncio.Event()

    async def flaky(run_id, type_, data):
        if type_ == "tool.completed":
            raise RuntimeError("boom")
        return await real(run_id, type_, data)

    executor.emit = flaky
    original = executor._answer_pending

    async def slow_answer(run, state):
        reached.set()
        await asyncio.sleep(0.3)
        await original(run, state)

    executor._answer_pending = slow_answer
    run = await start(store)
    task = asyncio.create_task(executor.execute(run))
    await reached.wait()
    task.cancel()
    await task
    done = await store.get_run("p1", run.id)
    assert (done.status, done.error_code) == ("failed", "internal_error")
    assert [r[0] for r in await tool_results(store)] == ["c1", "c2"]
    assert not task.cancelling()


async def test_a_forced_termination_suppresses_late_events_and_stays_the_last_event(loop_parts):
    store, provider, executor, _ = loop_parts
    tool_started = asyncio.Event()
    real_emit = executor.emit

    async def emit(run_id, type_, data):
        event = await real_emit(run_id, type_, data)
        if type_ == "tool.started":
            tool_started.set()
        return event

    executor.emit = emit
    provider.script = [[call("terminal", command="sleep 0.3"), Completed(Usage(1, 1, 0, 0))],
                       answer("after")]
    run = await start(store)
    task = asyncio.create_task(executor.execute(run))
    await tool_started.wait()
    forced = await store.finish_run(run.id, "failed", error_code="worker_lost", error="the worker was lost")
    assert forced.event is not None
    assert await executor.emit(run.id, "message.delta", {"text": "late"}) is None
    await asyncio.wait_for(task, 5)
    assert [e.type for e in await store.events_after(run.id, 0)] == [
        "run.started", "tool.started", "run.failed"]
    messages = [m.message for m in await store.messages("s1")]
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[2].tool_call_id == "c1"  # the tool's result was still stored; only events are suppressed


async def test_the_internal_error_path_kills_a_running_command(loop_parts):
    store, provider, executor, work = loop_parts
    provider.script = [[call("terminal", "c1", command="(sleep 1; touch late) & sleep 30"),
                        Completed(Usage(1, 1, 0, 0))]]
    # Fail the run from outside the command while it is still running (after tool.started plus 0.3 s).
    run = await start(store)
    real_emit = executor.emit

    async def emit(run_id, type_, data):
        event = await real_emit(run_id, type_, data)
        if type_ == "tool.started":
            asyncio.get_running_loop().call_later(0.3, boom.cancel)
        return event

    boom = asyncio.get_running_loop().create_future()
    executor.emit = emit
    original = executor._call_tool

    async def failing(run_, call_, ctx):
        runner = asyncio.ensure_future(original(run_, call_, ctx))
        done, _ = await asyncio.wait({runner, boom}, return_when=asyncio.FIRST_COMPLETED)
        if runner not in done:
            raise RuntimeError("internal failure while the command runs")
        return runner.result()

    executor._call_tool = failing
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).error_code == "internal_error"
    await asyncio.sleep(1.5)
    assert not (work / "late").exists()


async def seed_open_call(store, status="failed"):
    from keel.models import Message, TextPart

    await store.ensure_session("p1", "s1")
    await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": "old"}], instructions=None)
    old = await store.claim_run("w0", 9_999_999)
    await store.append_message("s1", old.id, Message("user", (TextPart("old"),)))
    await store.append_message("s1", old.id, Message("assistant", (), tool_calls=(ToolCall("x1", "terminal", "{}"),)))
    await store.finish_run(old.id, status, output="", usage=Usage(), error_code="worker_lost", error="swept")
    return old


async def test_a_stale_runs_late_cleanup_does_not_duplicate_the_repaired_result(loop_parts):
    from keel.runner import _RunState

    store, provider, executor, _ = loop_parts
    old = await seed_open_call(store)
    provider.script = [answer()]
    await executor.execute(await start(store, "new"))
    state = _RunState()
    await executor._answer_pending(old, state)
    assert await tool_results(store) == [("x1", "The command was cancelled.")]


async def test_a_failed_repair_write_fails_the_turn_before_the_user_message(loop_parts):
    store, provider, executor, _ = loop_parts
    await seed_open_call(store)
    real = store.append_message

    async def append(session_id, run_id, message):
        if message.role == "tool":
            raise RuntimeError("db down")
        return await real(session_id, run_id, message)

    store.append_message = append
    run = await start(store, "new")
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).error_code == "internal_error"
    assert provider.requests == []
    assert [m.message.text for m in await store.messages("s1") if m.message.role == "user"] == ["old"]


async def test_a_stored_policy_outside_the_mount_root_fails_the_run(store, tmp_path):
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": "/w",
                                        "mounts": [{"source": "/etc", "target": "/x"}]})
    executor = RunExecutor(store, InMemoryEventBus(), registry, sandbox=LocalSandbox(), sleep=no_sleep,
                           mount_root="/srv/data")
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.error_code) == ("failed", "sandbox_unavailable")
    assert provider.requests == []


# --- final fix wave ---

class CountingSandbox(LocalSandbox):
    def __init__(self):
        super().__init__(kill_grace_s=0.5)
        self.execs = []

    async def exec(self, sandbox, argv, **kwargs):
        self.execs.append(argv)
        return await super().exec(sandbox, argv, **kwargs)


async def test_skill_discovery_is_cached_for_the_ttl(loop_parts):
    store, provider, executor, _ = loop_parts
    counting = CountingSandbox()
    now = [100.0]
    executor.sandbox, executor._clock, executor.skills_ttl_s = counting, lambda: now[0], 60.0
    provider.script = [answer(), answer(), answer()]

    def discoveries():
        return sum(1 for argv in counting.execs if "keel-discover" in argv)

    await executor.execute(await start(store, "one"))
    assert discoveries() == 1
    now[0] = 130.0
    await executor.execute(await start(store, "two"))
    assert discoveries() == 1
    now[0] = 161.0
    await executor.execute(await start(store, "three"))
    assert discoveries() == 2


async def test_a_vanished_session_cwd_is_reset_in_the_store(loop_parts):
    import shutil
    store, provider, executor, work = loop_parts
    (work / "sub").mkdir(parents=True)
    provider.script = [[call("terminal", command="cd sub"), Completed(Usage(1, 1, 0, 0))], answer(),
                       [call("terminal", command="pwd"), call("execute_code", "c2", code="print(1)"),
                        Completed(Usage(1, 1, 0, 0))], answer()]
    await executor.execute(await start(store, "one"))
    assert (await store.get_session("p1", "s1")).cwd.endswith("/sub")
    shutil.rmtree(work / "sub")
    await executor.execute(await start(store, "two"))
    tools = [json.loads(m.message.text) for m in await store.messages("s1") if m.message.role == "tool"][-2:]
    assert all(t["exit_code"] == 0 for t in tools) and "no longer exists" in tools[0]["output"]
    assert (await store.get_session("p1", "s1")).cwd == str(work)


async def test_a_cancelled_tool_still_gets_its_tool_completed(loop_parts):
    store, provider, executor, _ = loop_parts
    provider.script = [[call("terminal", "c1", command="sleep 30"), call("terminal", "c2", command="echo second"),
                        Completed(Usage(1, 1, 0, 0))]]
    run = await start(store)
    task = asyncio.create_task(executor.execute(run))
    await wait_for_event(store, run.id, "tool.started")
    await asyncio.sleep(0.3)
    task.cancel()
    await task
    events = [(e.type, e.data) for e in await store.events_after(run.id, 0)]
    assert [t for t, _ in events] == ["run.started", "tool.started", "tool.completed", "run.cancelled"]
    completed = events[2][1]
    assert completed["tool_call_id"] == "c1" and completed["error"] is True and completed["preview"] == "sleep 30"
    assert completed["tool"] == "terminal" and completed["duration_s"] >= 0


async def test_every_request_of_a_multi_turn_session_renders_as_valid_responses_input(loop_parts):
    import httpx
    from keel.models import ReasoningItem, ReasoningReady
    from keel.providers.responses import ResponsesTransport
    store, provider, executor, _ = loop_parts
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    reasoning = ReasoningItem("fake", "m", "reasoning", {"encrypted_content": "ENC", "summary": []})
    provider.script = [
        [ReasoningReady(reasoning), call("terminal", "a1", command="echo one"),
         call("terminal", "a2", command="echo two"), Completed(Usage(1, 1, 0, 0))],
        answer("first done"),
        [call("terminal", "b1", command="sleep 30"), Completed(Usage(1, 1, 0, 0))],
        answer("saw the picture"),
    ]
    await executor.execute(await start(store, "turn one"))
    cancelled = await start(store, "turn two")
    task = asyncio.create_task(executor.execute(cancelled))
    await wait_for_event(store, cancelled.id, "tool.started")
    await asyncio.sleep(0.3)
    task.cancel()
    await task
    await store.ensure_session("p1", "s1")
    await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": "turn three"},
                                        {"type": "image", "media_type": "image/png", "data_b64": png}],
                           instructions=None)
    await executor.execute(await store.claim_run("w1", 9_999_999))
    assert (await store.get_run("p1", cancelled.id)).status == "cancelled"
    transport = ResponsesTransport("fake", "http://unused", httpx.AsyncClient())  # build_body makes no request
    assert len(provider.requests) == 4
    for request in provider.requests:
        items = transport.build_body(request)["input"]
        open_calls: list[str] = []
        outputs: dict[str, int] = {}
        for index, item in enumerate(items):
            if item.get("role") == "user":
                assert not open_calls, f"calls without an output before a user item: {open_calls}"
            if item.get("type") == "function_call":
                open_calls.append(item["call_id"])
                outputs.setdefault(item["call_id"], 0)
            elif item.get("type") == "function_call_output":
                assert item["call_id"] in open_calls, "an output without a call before it"
                open_calls.remove(item["call_id"])
                outputs[item["call_id"]] += 1
            elif item.get("type") == "reasoning":
                assert index + 1 < len(items) and (items[index + 1].get("type") in ("reasoning", "function_call")
                                                   or items[index + 1].get("role") == "assistant")
        assert not open_calls and all(count == 1 for count in outputs.values())
    second = transport.build_body(provider.requests[1])["input"]
    kinds = [item.get("type") or item.get("role") for item in second]
    assert kinds == ["user", "reasoning", "function_call", "function_call", "function_call_output",
                     "function_call_output"]
    last = transport.build_body(provider.requests[3])["input"]
    assert [item.get("type") or item.get("role") for item in last].count("function_call_output") == 3
    picture = [part for part in last[-1]["content"] if part["type"] == "input_image"]
    assert picture == [{"type": "input_image", "image_url": f"data:image/png;base64,{png}"}]
