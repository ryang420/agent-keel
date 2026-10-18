import asyncio
import json

import pytest

from keel import prompts
from keel.background import Background
from keel.events import InMemoryEventBus
from keel.models import Completed, ToolCall, ToolCallReady, ReasoningItem, ReasoningReady, TextDelta, TextPart, Usage
from keel.providers.base import ModelError, ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import DEFAULT_IDENTITY, RunExecutor
from keel.titles import derive_title
from keel.tools import ToolContext, run_tool
from tests.sandbox_support import ClaimedExecutor, local_coordinator

ITEM = ReasoningItem("fake", "m", "reasoning", {"encrypted_content": "e"})


async def no_sleep(seconds):
    pass


@pytest.fixture
async def setup(store):
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[])
    return store, provider, RunExecutor(store, InMemoryEventBus(), registry, sleep=no_sleep)


async def start(store, text="What is two plus two?", session="s1", instructions="Answer briefly.", claim=True):
    await store.ensure_session("p1", session)
    run = await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m",
                                 reasoning_effort="low", input_parts=[{"type": "text", "text": text}],
                                 instructions=instructions)
    if not claim:
        return run
    return await store.claim_run("w1", 9_999_999)


async def test_a_completed_turn_stores_both_messages_usage_and_events(setup):
    store, provider, executor = setup
    provider.script = [[TextDelta("Fo"), TextDelta("ur"), ReasoningReady(ITEM), Completed(Usage(50, 5, 40, 0))]]
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.output, done.usage) == ("completed", "Four", Usage(50, 5, 40, 0))
    messages = [m.message for m in await store.messages("s1")]
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].text == "What is two plus two?" and messages[1].text == "Four"
    assert messages[1].reasoning == (ITEM,)
    events = await store.events_after(run.id, 0)
    assert [e.type for e in events] == ["run.started", "message.delta", "message.delta", "run.completed"]
    assert events[-1].data == {"output": "Four", "usage": Usage(50, 5, 40, 0).to_dict()}
    assert (await store.get_session("p1", "s1")).title == "What is two plus two?"


async def test_completion_work_runs_only_when_the_terminal_transition_was_won(setup):
    store, provider, executor = setup
    run = await start(store)
    await store.finish_run(run.id, "cancelled")  # a stop lands while the model call is in flight
    called = []

    async def spy(*args):
        called.append(args)

    executor._after_complete = spy
    await executor.execute(run)
    assert called == []
    assert [e.type for e in await store.events_after(run.id, 0)] == ["run.cancelled"]
    assert (await store.get_run("p1", run.id)).status == "cancelled"


async def test_the_request_carries_identity_instructions_and_history(setup):
    store, provider, executor = setup
    await executor.execute(await start(store, "first"))
    await executor.execute(await start(store, "second", instructions="Be terse."))
    request = provider.requests[-1]
    assert request.system == DEFAULT_IDENTITY and request.instructions == "Be terse."
    assert [m.role for m in request.messages] == ["user", "assistant", "user"]
    assert request.messages[-1].parts == (TextPart("second"),)
    assert request.cache_scope == "s1" and request.reasoning_effort == "low" and request.tools == ()


async def test_a_profile_soul_replaces_the_default_identity(setup):
    store, provider, executor = setup
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={},
                               soul="You are a careful analyst.", skills=[])
    await executor.execute(await start(store))
    assert provider.requests[-1].system == "You are a careful analyst."


async def test_a_model_error_fails_the_run(setup):
    store, provider, executor = setup
    provider.script = [[ModelError("bad_request", "nope")]]
    run = await start(store)
    await executor.execute(run)
    failed = await store.get_run("p1", run.id)
    assert (failed.status, failed.error_code, failed.error) == ("failed", "bad_request", "nope")
    events = await store.events_after(run.id, 0)
    assert events[-1].type == "run.failed" and events[-1].data == {"error_code": "bad_request", "message": "nope"}


async def test_an_unknown_provider_fails_the_run(setup):
    store, provider, executor = setup
    await store.ensure_session("p1", "s9")
    await store.create_run(profile_id="p1", session_id="s9", provider="gone", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": "x"}], instructions=None)
    run = await store.claim_run("w1", 9_999_999)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).error_code == "bad_request"


async def test_cancelling_the_execution_cancels_the_run(setup):
    store, provider, executor = setup
    provider.script = [[TextDelta("par"), "hang"]]
    run = await start(store)
    task = asyncio.create_task(executor.execute(run))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(await store.events_after(run.id, 0)) >= 2:
            break
    task.cancel()
    await task
    cancelled = await store.get_run("p1", run.id)
    assert (cancelled.status, cancelled.output) == ("cancelled", "par")
    assert (await store.events_after(run.id, 0))[-1].type == "run.cancelled"


def test_derived_titles_use_the_first_line_and_cut_at_a_word():
    assert derive_title("  \n Hello world \nmore") == "Hello world"
    long = "word " * 20
    title = derive_title(long)
    assert title.endswith("…") and len(title) <= 48 and " " not in title[-2:]
    assert derive_title("   ") is None


def test_derived_titles_never_exceed_the_limit_with_the_ellipsis():
    for text in ("x" * 200, "ab " * 40, "a" * 47 + " " + "b" * 30, "a" * 48, "a" * 49):
        title = derive_title(text)
        assert len(title) <= 48, (text, title)


def test_derived_titles_drop_control_zero_width_and_bidi_characters():
    assert derive_title("\u200b\n\x07He\u202ello\u200b\x00 wor\ufeffld") == "Hello world"
    assert derive_title("\u200b\u202e") is None


async def test_a_taken_derived_title_gets_a_numbered_suffix(setup):
    store, provider, executor = setup
    await executor.execute(await start(store, "hi", session="s1"))
    await executor.execute(await start(store, "hi", session="s2"))
    await executor.execute(await start(store, "hi", session="s3"))
    titles = [(await store.get_session("p1", s)).title for s in ("s1", "s2", "s3")]
    assert titles == ["hi", "hi #2", "hi #3"]


async def test_a_suffixed_title_still_fits_the_limit(setup):
    store, provider, executor = setup
    long = "w" * 60
    await executor.execute(await start(store, long, session="s1"))
    await executor.execute(await start(store, long, session="s2"))
    titles = [(await store.get_session("p1", s)).title for s in ("s1", "s2")]
    assert all(len(t) <= 48 for t in titles) and titles[1].endswith(" #2") and titles[0] != titles[1]


async def test_an_unexpected_provider_error_fails_the_run_without_leaking(setup):
    store, provider, executor = setup
    provider.script = [[RuntimeError("secret detail")]]
    run = await start(store)
    await executor.execute(run)
    failed = await store.get_run("p1", run.id)
    assert (failed.status, failed.error_code) == ("failed", "internal_error")
    assert "secret" not in failed.error
    last = (await store.events_after(run.id, 0))[-1]
    assert last.type == "run.failed" and last.data["error_code"] == "internal_error"


async def test_a_malformed_input_part_fails_the_run(setup):
    store, provider, executor = setup
    await store.ensure_session("p1", "s7")
    await store.create_run(profile_id="p1", session_id="s7", provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "audio"}], instructions=None)
    run = await store.claim_run("w1", 9_999_999)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).error_code == "internal_error"


async def test_a_double_cancel_still_ends_the_run_cancelled(setup):
    store, provider, executor = setup
    provider.script = [[TextDelta("par"), "hang"]]
    run = await start(store)
    task = asyncio.create_task(executor.execute(run))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(await store.events_after(run.id, 0)) >= 2:
            break
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await task
    assert (await store.get_run("p1", run.id)).status == "cancelled"
    assert (await store.events_after(run.id, 0))[-1].type == "run.cancelled"


async def test_a_tool_call_without_tools_is_ignored_with_a_warning(setup, caplog):
    store, provider, executor = setup
    provider.script = [[TextDelta("ok"), ToolCallReady(ToolCall("c1", "lookup", "{}")), Completed(Usage(1, 1, 0, 0))]]
    run = await start(store)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).status == "completed"
    assert "lookup" in caplog.text


async def test_an_incomplete_response_still_completes_but_says_so(setup, caplog):
    store, provider, executor = setup
    provider.script = [[TextDelta("par"), Completed(Usage(5, 2, 0, 0), "incomplete")]]
    with caplog.at_level("WARNING"):
        await executor.execute(await start(store))
    run = (await store.list_runs("p1", ("completed",)))[0]
    assert run.status == "completed" and run.output == "par"
    event = (await store.events_after(run.id, 0))[-1]
    assert event.type == "run.completed" and event.data["incomplete"] is True
    assert "incomplete" in caplog.text
    # An ordinary completion carries no flag.
    provider.script = [[TextDelta("x"), Completed(Usage(1, 1, 0, 0))]]
    await executor.execute(await start(store, session="s2"))
    other = [r for r in await store.list_runs("p1", ("completed",)) if r.session_id == "s2"][0]
    assert "incomplete" not in (await store.events_after(other.id, 0))[-1].data


def with_background(setup):
    """`setup` builds the executor; these tests pass the background owner the fixture does not."""
    store, provider, executor = setup
    background = Background()
    titled = RunExecutor(store, executor.bus, executor.providers, sleep=no_sleep, background=background)
    return store, provider, titled, background


async def wait_for_background(background):
    """`asyncio.wait` rejects an empty set, and a task is removed from `_tasks` once it finishes."""
    tasks = [task for task in background._tasks]
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=2)
    assert not pending


OPENING = "The pool exhausts under load"
DONE = [TextDelta("done"), Completed(Usage(3, 1, 0, 0))]
TITLE_OK = [TextDelta('{"title": "Pool exhausts under load"}'), Completed(Usage(3, 1, 0, 0))]


async def test_the_first_completed_run_is_renamed_in_the_background(setup):
    store, provider, executor, background = with_background(setup)
    # The title step sleeps so the derived title can be read: any await after execute also runs the task.
    provider.script = [DONE, [0.05, *TITLE_OK]]
    run = await start(store, OPENING)
    await executor.execute(run)
    session = await store.get_session("p1", "s1")
    assert (session.title, session.title_source) == (OPENING, "derived")
    events = [event.type for event in await store.events_after(run.id, 0)]
    await wait_for_background(background)
    session = await store.get_session("p1", "s1")
    assert (session.title, session.title_source) == ("Pool exhausts under load", "llm")
    assert len(provider.requests) == 2
    title_request = provider.requests[1]
    assert title_request.tools == () and title_request.reasoning_effort == "low"
    assert title_request.max_output_tokens == 512 and title_request.cache_scope == ""
    assert OPENING in title_request.messages[0].text
    assert [event.type for event in await store.events_after(run.id, 0)] == events
    assert [message.message.role for message in await store.messages("s1")] == ["user", "assistant"]


async def test_a_second_run_does_not_call_the_model_for_a_title(setup):
    store, provider, executor, background = with_background(setup)
    provider.script = [DONE, TITLE_OK, [TextDelta("again"), Completed(Usage(3, 1, 0, 0))]]
    await executor.execute(await start(store, OPENING))
    await wait_for_background(background)
    titled = (await store.get_session("p1", "s1")).title
    before = len(provider.requests)
    await executor.execute(await start(store, "more detail"))
    await wait_for_background(background)
    assert len(provider.requests) == before + 1
    assert (await store.get_session("p1", "s1")).title == titled


async def test_a_bad_title_keeps_the_derived_one(setup):
    store, provider, executor, background = with_background(setup)
    provider.script = [DONE, [TextDelta("I cannot tell")]]
    await executor.execute(await start(store, OPENING))
    await wait_for_background(background)
    session = await store.get_session("p1", "s1")
    assert (session.title, session.title_source) == (OPENING, "derived")


async def test_a_user_rename_during_the_call_wins(setup):
    store, provider, executor, background = with_background(setup)
    provider.script = [DONE, [0.05, *TITLE_OK]]
    await executor.execute(await start(store, OPENING))
    session = await store.get_session("p1", "s1")
    assert (session.title, session.title_source) == (OPENING, "derived")

    async def rename_once_the_title_call_starts():
        for _ in range(100):
            if len(provider.requests) >= 2:
                await store.set_title("p1", "s1", "Chosen", "user")
                return
            await asyncio.sleep(0.001)
        raise AssertionError("the title call never started")

    renaming = asyncio.create_task(rename_once_the_title_call_starts())
    await wait_for_background(background)
    await renaming
    session = await store.get_session("p1", "s1")
    assert (session.title, session.title_source) == ("Chosen", "user")


async def test_memory_is_offered_without_a_sandbox(setup):
    store, provider, executor = setup
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True)
    provider.script = [[ToolCallReady(ToolCall("c1", "memory", json.dumps(
        {"action": "add", "target": "user", "content": "The user likes tea"}))), Completed(Usage(1, 1, 0, 0))],
                       [TextDelta("ok"), Completed(Usage(1, 1, 0, 0))],
                       [TextDelta("ok"), Completed(Usage(1, 1, 0, 0))]]
    run = await start(store)
    await store.db.execute("UPDATE sessions SET turns_since_memory = 4 WHERE id = ?", "s1")
    await executor.execute(run)
    assert tuple(tool["name"] for tool in provider.requests[0].tools) == ("memory",)
    assert (await store.memory_entries("p1"))["user"] == ["The user likes tea"]
    session = await store.get_session("p1", "s1")
    assert session.turns_since_memory == 0
    # "tea" occurs inside "instead" in the guidance text, so the frozen snapshot is checked by the whole fact.
    assert "The user likes tea" not in session.system_prompt and prompts.text("memory") in session.system_prompt
    assert "USER PROFILE (who the user is)\n(empty)" in session.system_prompt
    await executor.execute(await start(store, "later", session="s2"))
    later = await store.get_session("p1", "s2")
    assert "The user likes tea" in later.system_prompt
    assert (await store.get_session("p1", "s1")).system_prompt == session.system_prompt


async def test_a_sandbox_profile_with_memory_gets_the_sandbox_tools_and_memory(store, tmp_path, monkeypatch):
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    work = tmp_path / "work"
    work.mkdir()
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[], memory=True,
                               sandbox={"image": "local", "workdir": str(work)})
    coordinator, _ = await local_coordinator(store, tmp_path / "bin", monkeypatch)
    executor = ClaimedExecutor(store, InMemoryEventBus(), registry, coordinator=coordinator, sleep=no_sleep)
    await start(store, claim=False)
    await executor.execute(await executor.claim_next())
    assert [tool["name"] for tool in provider.requests[0].tools] == [*prompts.TOOL_ORDER, "memory"]


async def test_a_tool_other_than_memory_needs_a_sandbox():
    missing = await run_tool("terminal", '{"command": "true"}', ToolContext())
    assert missing.error and "sandbox" in missing.content
    unavailable = await run_tool("memory", '{"target": "user", "action": "add", "content": "x"}', ToolContext())
    assert unavailable.error and "Memory is not available." in unavailable.content


async def test_memory_refuses_a_write_when_the_profile_flag_is_off(store, local_sandbox):
    """A sandboxed run always puts the store on the context, and the model can call memory even when it was not
    offered. The handler re-reads the profile and writes only when memory is on."""
    backend, sandbox, _work = local_sandbox
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}],
                               sandbox={"image": "local", "workdir": str(_work)}, soul="", skills=[], memory=False)
    await store.ensure_session("p1", "s1")
    ctx = ToolContext(backend, sandbox, cwd=str(_work), store=store, profile_id="p1", session_id="s1")
    refused = await run_tool("memory", '{"action":"add","target":"memory","content":"secret fact"}', ctx)
    assert refused.error
    assert (await store.memory_entries("p1"))["memory"] == []

    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}],
                               sandbox={"image": "local", "workdir": str(_work)}, soul="", skills=[], memory=True)
    saved = await run_tool("memory", '{"action":"add","target":"memory","content":"secret fact"}', ctx)
    assert saved.error is False
    assert (await store.memory_entries("p1"))["memory"] == ["secret fact"]


async def test_a_profile_with_neither_still_gets_no_tools(setup):
    store, provider, executor = setup
    await executor.execute(await start(store))
    assert provider.requests[0].tools == ()



async def test_the_title_covers_the_conversation_when_its_first_run_did_not_complete(setup):
    # 2026-10-16: a session whose first turn was stopped was titled from its second turn, a bare "hello"
    # ("友好问候"), because the title read only the first completed run's input.
    from keel.providers.base import ModelError

    store, provider, executor, background = with_background(setup)
    provider.script = [[ModelError("bad_request", "refused")], DONE, TITLE_OK]
    first = await start(store, OPENING)
    await executor.execute(first)
    assert (await store.get_run("p1", first.id)).status == "failed"
    await executor.execute(await start(store, "hello"))
    await wait_for_background(background)
    title_request = provider.requests[-1]
    assert OPENING in title_request.messages[0].text and "hello" in title_request.messages[0].text
    assert (await store.get_session("p1", "s1")).title == "Pool exhausts under load"
