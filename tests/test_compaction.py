import json
from datetime import UTC, datetime, timedelta

import pytest

from keel import prompts
from keel.events import InMemoryEventBus
from keel.models import Completed, Message, TextDelta, TextPart, ToolCall, ToolCallReady, Usage
from keel.providers.base import Capabilities, ModelError, ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from tests.sandbox_support import ClaimedExecutor, local_coordinator

BIG = "a" * 16_000  # 4,000 tokens
SUMMARY_USAGE = Usage(100, 20, 0, 0)


async def no_sleep(seconds):
    pass


class Model(FakeProvider):
    """Main calls follow `script`; summary calls, recognised by their system prompt, follow `summary_script`."""

    def __init__(self, window: int) -> None:
        super().__init__(capabilities=Capabilities(window, True, False, True, "none"))
        self.summaries: list = []
        self.summary_script: list = []

    async def stream(self, request):
        if request.system != prompts.text("summary_system"):
            async for event in super().stream(request):
                yield event
            return
        self.summaries.append(request)
        step = self.summary_script.pop(0) if self.summary_script else [TextDelta("SUMMARY"), Completed(SUMMARY_USAGE)]
        for item in step:
            if isinstance(item, BaseException):
                raise item
            yield item


class Now:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class Ticking:
    """A clock that moves a day forward every time it is read."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return datetime(2026, 10, 12, tzinfo=UTC) + timedelta(days=self.reads)


@pytest.fixture
async def parts(store):
    model = Model(40_000)
    registry = ProviderRegistry()
    registry.register(model)
    now = Now()
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[])
    return store, model, RunExecutor(store, InMemoryEventBus(), registry, sleep=no_sleep, now=now), now


async def seed(store, pairs, text=BIG, session="s1"):
    await store.ensure_session("p1", session)
    for _ in range(pairs):
        await store.append_message(session, None, Message("user", (TextPart(text),)))
        await store.append_message(session, None, Message("assistant", (TextPart(text),)))


async def start(store, text="next", session="s1", executor=None):
    await store.ensure_session("p1", session)
    await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": text}], instructions=None)
    if executor is not None:
        return await executor.claim_next()  # a sandboxed run is claimed with its usage
    return await store.claim_run("w1", 9_999_999)


async def sandboxed(store, tmp_path, monkeypatch, registry, **options):
    coordinator, _ = await local_coordinator(store, tmp_path / "bin", monkeypatch)
    return ClaimedExecutor(store, InMemoryEventBus(), registry, coordinator=coordinator, **options)


def answer(text="done", usage=Usage(None, 5, None, None)):
    return [TextDelta(text), Completed(usage)]


async def test_a_session_over_the_threshold_is_summarized_before_the_call(parts):
    store, model, executor, _ = parts
    await seed(store, 8)
    model.script = [answer()]
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert done.status == "completed" and done.usage == Usage(100, 25, 0, 0)
    stored = await store.messages("s1")
    assert len(stored) == 18
    session = await store.get_session("p1", "s1")
    assert session.summary == "SUMMARY"
    assert (session.summary_head_id, session.summary_tail_id, session.pruned_id) == (
        stored[2].id, stored[14].id, stored[14].id)
    request = model.requests[-1]
    assert [m.text for m in request.messages[:3]] == [BIG, BIG, BIG]
    assert request.messages[3].text.startswith(prompts.text("summary_prefix"))
    assert "\n\nSUMMARY\n\n" in request.messages[3].text
    assert [m.text for m in request.messages[4:]] == [BIG, BIG, "next"]
    prompt = model.summaries[0].messages[0].text
    assert "TURNS TO SUMMARIZE:" in prompt and prompt.count("[USER]: ") == 5 and prompt.count("[ASSISTANT]: ") == 6
    compacted = [e.data for e in await store.events_after(run.id, 0) if e.type == "context.compacted"]
    assert len(compacted) == 1
    data = compacted[0]
    assert (data["messages_before"], data["messages_after"], data["usage"]) == (17, 7, SUMMARY_USAGE.to_dict())
    assert data["tokens_before"] >= 30_000 > data["tokens_after"]


async def test_long_tool_results_are_replaced_without_a_summary_when_that_is_enough(parts):
    store, model, executor, _ = parts
    await seed(store, 1, text="hi")
    await store.append_message("s1", None, Message("user", (TextPart("read three files"),)))
    for n in range(3):
        call = ToolCall(f"c{n}", "read_file", json.dumps({"path": f"/w/{n}.txt"}))
        await store.append_message("s1", None, Message("assistant", tool_calls=(call,)))
        await store.append_message("s1", None, Message("tool", (TextPart("r" * 48_000),), tool_call_id=f"c{n}",
                                                       tool_name="read_file"))
    await store.append_message("s1", None, Message("assistant", (TextPart("read them"),)))
    model.script = [answer()]
    run = await start(store)
    await executor.execute(run)
    assert model.summaries == []
    stored = await store.messages("s1")
    session = await store.get_session("p1", "s1")
    assert session.summary is None and session.pruned_id == stored[9].id
    sent = [m.text for m in model.requests[-1].messages if m.role == "tool"]
    assert sent == ["[read_file result omitted from context: 48,000 characters]"] * 3
    assert [len(m.message.text) for m in stored if m.message.role == "tool"] == [48_000] * 3
    assert not [e for e in await store.events_after(run.id, 0) if e.type == "context.compacted"]


async def test_a_run_compacts_between_two_tool_calls(store, tmp_path, monkeypatch):
    model = Model(200_000)
    registry = ProviderRegistry()
    registry.register(model)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(tmp_path / "work")})
    executor = await sandboxed(store, tmp_path, monkeypatch, registry, sleep=no_sleep, now=Ticking())
    await seed(store, 1, text="earlier")
    await store.set_session_prompt("s1", "AN EARLIER PROMPT", "fake", "m")
    model.script = [
        [TextDelta("x" * 400_000), ToolCallReady(ToolCall("c1", "terminal", '{"command": "echo one"}')),
         Completed(Usage())],
        [TextDelta("y" * 200_000), ToolCallReady(ToolCall("c2", "terminal", '{"command": "echo two"}')),
         Completed(Usage())],
        answer("finished"),
    ]
    run = await start(store, "go", executor=executor)
    await executor.execute(run)
    assert (await store.get_run("p1", run.id)).status == "completed"
    assert len(model.requests) == 3 and len(model.summaries) == 1
    first, second, third = model.requests
    assert first.system == second.system == "AN EARLIER PROMPT" and first.tools == second.tools == third.tools
    assert third.system != first.system
    assert third.system == (await store.get_session("p1", "s1")).system_prompt
    assert [m.role for m in third.messages] == ["user", "assistant", "user", "user", "assistant", "tool"]
    assert third.messages[-1].tool_call_id == "c2"
    stored = [m.message for m in await store.messages("s1")]
    calls = {c.id for m in stored for c in m.tool_calls}
    assert calls == {m.tool_call_id for m in stored if m.role == "tool"} == {"c1", "c2"}


async def test_an_overflow_before_any_output_compacts_and_retries_once(parts):
    store, model, executor, _ = parts
    await seed(store, 8, text="b" * 4_000)
    model.script = [[ModelError("context_overflow", "too long")], answer("after")]
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.output) == ("completed", "after")
    assert len(model.summaries) == 1 and len(model.requests) == 2
    assert "\n\nSUMMARY\n\n" in model.requests[1].messages[3].text


async def test_an_overflow_that_persists_fails_the_run(parts):
    store, model, executor, _ = parts
    await seed(store, 8, text="b" * 4_000)
    model.script = [[ModelError("context_overflow", "too long")], [ModelError("context_overflow", "still")]]
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.error_code) == ("failed", "context_overflow")
    assert len(model.summaries) == 1


async def test_a_failed_summary_pauses_summaries_for_ten_minutes(parts):
    store, model, executor, now = parts
    await seed(store, 8)
    model.summary_script = [[ModelError("bad_request", "no")]]
    model.script = [answer(), answer(), answer()]
    first = await start(store, "one")
    await executor.execute(first)
    session = await store.get_session("p1", "s1")
    assert (await store.get_run("p1", first.id)).status == "completed"
    assert len(model.summaries) == 1 and session.summary is None and session.pruned_id is not None
    assert session.compaction_failed_at == now.value.timestamp()
    now.value += timedelta(minutes=5)
    await executor.execute(await start(store, "two"))
    assert len(model.summaries) == 1
    now.value += timedelta(minutes=6)
    await executor.execute(await start(store, "three"))
    session = await store.get_session("p1", "s1")
    assert len(model.summaries) == 2 and session.summary == "SUMMARY" and session.compaction_failed_at is None


async def test_a_call_whose_newest_round_was_cut_records_no_anchor(store, tmp_path, monkeypatch):
    model = Model(40_000)
    registry = ProviderRegistry()
    registry.register(model)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(tmp_path / "work")})
    executor = await sandboxed(store, tmp_path, monkeypatch, registry, sleep=no_sleep)
    anchors = []
    record = store.set_context_anchor

    async def spy(session_id, tokens, message_id):
        anchors.append(tokens)
        await record(session_id, tokens, message_id)

    store.set_context_anchor = spy
    await seed(store, 2, text="earlier")
    await store.set_pruned("s1", (await store.messages("s1"))[-1].id)
    huge = ToolCall("c1", "terminal", json.dumps({"command": "python3 -c \"print('z' * 60000)\""}))
    model.script = [
        [ToolCallReady(huge), Completed(Usage(18_500, 5, 0, 0))],
        [ToolCallReady(ToolCall("c2", "terminal", '{"command": "echo two"}')), Completed(Usage(28_600, 5, 0, 0))],
        answer("finished", usage=Usage(2_000, 5, 0, 0)),
    ]
    await executor.execute(await start(store, "go", executor=executor))
    assert len(model.requests) == 3 and model.summaries == []
    assert anchors == [18_500, 2_000]
    sent = {m.tool_call_id: m.text for m in model.requests[2].messages if m.role == "tool"}
    assert sent["c1"].startswith("[terminal result omitted from context:")


async def test_the_final_call_without_tools_records_no_anchor(store, tmp_path, monkeypatch):
    model = Model(40_000)
    registry = ProviderRegistry()
    registry.register(model)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(tmp_path / "work")})
    executor = await sandboxed(store, tmp_path, monkeypatch, registry, sleep=no_sleep, max_iterations=1)
    model.script = [
        [ToolCallReady(ToolCall("c1", "terminal", '{"command": "echo one"}')), Completed(Usage(3_000, 5, 0, 0))],
        answer("summing up", usage=Usage(1_000, 5, 0, 0)),
    ]
    await executor.execute(await start(store, "go", executor=executor))
    assert model.requests[1].tools == ()
    session = await store.get_session("p1", "s1")
    assert (session.context_tokens, session.context_message_id) == (3_000, (await store.messages("s1"))[0].id)


async def test_the_run_instructions_count_toward_the_threshold(parts):
    store, model, executor, _ = parts
    await seed(store, 3)
    model.script = [answer()]
    await store.ensure_session("p1", "s1")
    await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": "next"}], instructions="i" * 32_000)
    await executor.execute(await store.claim_run("w1", 9_999_999))
    assert len(model.summaries) == 1


async def test_a_summary_cut_at_the_output_limit_is_not_stored(parts):
    store, model, executor, now = parts
    await seed(store, 8)
    model.summary_script = [[TextDelta("PART"), Completed(SUMMARY_USAGE, "incomplete")]]
    model.script = [answer()]
    run = await start(store)
    await executor.execute(run)
    session = await store.get_session("p1", "s1")
    assert (await store.get_run("p1", run.id)).status == "completed"
    assert session.summary is None and session.pruned_id is not None
    assert session.compaction_failed_at == now.value.timestamp()


async def test_an_overflow_after_output_is_not_retried(parts):
    store, model, executor, _ = parts
    await seed(store, 8, text="b" * 4_000)
    model.script = [[TextDelta("partial"), ModelError("context_overflow", "too long")], answer("after")]
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.error_code) == ("failed", "context_overflow")
    assert len(model.requests) == 1 and model.summaries == []


async def test_an_overflow_whose_summary_fails_retries_with_placeholders(parts):
    store, model, executor, now = parts
    await seed(store, 1, text="hi")
    await store.append_message("s1", None, Message("user", (TextPart("read five files"),)))
    for n in range(5):
        call = ToolCall(f"c{n}", "read_file", json.dumps({"path": f"/w/{n}.txt"}))
        await store.append_message("s1", None, Message("assistant", tool_calls=(call,)))
        await store.append_message("s1", None, Message("tool", (TextPart("r" * 16_000),), tool_call_id=f"c{n}",
                                                       tool_name="read_file"))
    await store.append_message("s1", None, Message("assistant", (TextPart("read them"),)))
    model.summary_script = [[ModelError("bad_request", "no")]]
    model.script = [[ModelError("context_overflow", "too long")], answer("after")]
    run = await start(store)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.output) == ("completed", "after")
    assert len(model.summaries) == 1 and len(model.requests) == 2
    sent = [m.text for m in model.requests[1].messages if m.role == "tool"]
    assert sent[:3] == ["[read_file result omitted from context: 16,000 characters]"] * 3
    assert sent[3:] == ["r" * 16_000] * 2
    assert (await store.get_session("p1", "s1")).compaction_failed_at == now.value.timestamp()


async def test_the_reported_input_tokens_anchor_the_next_estimate(parts):
    store, model, executor, _ = parts
    model.script = [answer(usage=Usage(1_234, 5, 0, 0))]
    await executor.execute(await start(store, "hello"))
    stored = await store.messages("s1")
    session = await store.get_session("p1", "s1")
    assert (session.context_tokens, session.context_message_id) == (1_234, stored[0].id)
