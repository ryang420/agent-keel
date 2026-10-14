"""Long sessions (M3 design §6): many turns through the HTTP API with a small context window, and the edges of
compaction, against a provider that counts tokens the way keel estimates them and overflows above its window."""

import asyncio
import json

import httpx
import pytest

from keel import compress, prompts
from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.models import Completed, ImagePart, Message, TextDelta, TextPart, ToolCall, Usage
from keel.providers.base import Capabilities, ModelError, ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.sandbox.local import LocalSandbox
from keel.worker import WorkerPool

AUTH = {"Authorization": "Bearer test-key"}
PROFILE = {"models": [{"provider": "fake", "model": "m"}], "sandbox": {}, "soul": "", "skills": []}


class Counting(FakeProvider):
    """Counts a request with keel's heuristic times `factor`, fails with context_overflow above its window, reports
    the count as input tokens, and answers summary calls with a numbered summary."""

    def __init__(self, window: int, reply: str = "r" * 8_000, factor: float = 1.0) -> None:
        super().__init__(capabilities=Capabilities(window, True, False, True, "none"))
        self.window, self.reply, self.factor = window, reply, factor
        self.summaries: list = []
        self.main: list = []
        self.calls: list = []  # every request that was answered, in order
        self.overflows = 0

    async def stream(self, request):
        size = int(self.factor * compress.estimate([compress.Item(None, m) for m in request.messages],
                                                   compress.base_tokens(request.system, request.tools,
                                                                        request.instructions)))
        if size > self.window:
            self.overflows += 1
            raise ModelError("context_overflow", f"{size} tokens is over the window")
        self.calls.append(request)
        if request.system == prompts.text("summary_system"):
            self.summaries.append(request)
            yield TextDelta(f"SUMMARY {len(self.summaries)}")
        else:
            self.main.append(request)
            yield TextDelta(self.reply)
        yield Completed(Usage(size, 10, 0, 0))


def between_summaries(model: Counting) -> list[list]:
    """The main requests grouped by the summary calls that separate them."""
    groups: list[list] = [[]]
    for request in model.calls:
        if request.system == prompts.text("summary_system"):
            groups.append([])
        else:
            groups[-1].append(request)
    return [group for group in groups if group]


async def executor_for(store, model, **options):
    registry = ProviderRegistry()
    registry.register(model)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[])
    return RunExecutor(store, InMemoryEventBus(), registry, **options)


async def turn(store, executor, text, session="s1"):
    await store.ensure_session("p1", session)
    await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": text}], instructions=None)
    run = await store.claim_run("w1", 9_999_999)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert done.status == "completed", (done.error_code, done.error)
    return done


async def append(store, *messages, session="s1"):
    await store.ensure_session("p1", session)
    for message in messages:
        await store.append_message(session, None, message)


def user(text):
    return Message("user", (TextPart(text),))


def assistant(text):
    return Message("assistant", (TextPart(text),))


@pytest.fixture
async def service(store, tmp_path):
    model = Counting(40_000)
    registry = ProviderRegistry()
    registry.register(model)
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, registry, sandbox=LocalSandbox(kill_grace_s=0.5))
    pool = WorkerPool(store, bus, executor, owner="w1", workers=1, poll_seconds=0.01)
    keel = Keel(store=store, bus=bus, providers=registry, executor=executor,
                credentials=(ServiceCredential("t1", "test-key"),), max_running=2)
    await pool.start()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(keel)), base_url="http://keel",
                               headers=AUTH)
    profile = {**PROFILE, "sandbox": {"image": "local", "workdir": str(tmp_path / "work")}}
    assert (await client.put("/v1/profiles/p1", json=profile)).status_code == 200
    yield client, model, store
    await client.aclose()
    await pool.close(drain_seconds=0.1)


async def settle(client, run_id, seconds=10.0):
    for _ in range(int(seconds / 0.02)):
        body = (await client.get(f"/v1/profiles/p1/runs/{run_id}")).json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(body)


async def test_a_session_many_times_the_window_keeps_working(service):
    client, model, store = service
    run_ids = []
    for n in range(30):
        started = await client.post("/v1/profiles/p1/runs", json={
            "input": f"turn {n} " + "u" * 4_000, "session_id": "long", "provider": "fake", "model": "m"})
        assert started.status_code == 202, started.text
        body = await settle(client, started.json()["run_id"])
        assert body["status"] == "completed", body
        run_ids.append(started.json()["run_id"])
    compacted = [e for run_id in run_ids for e in await store.events_after(run_id, 0) if e.type == "context.compacted"]
    assert len(compacted) >= 2 and len(model.summaries) == len(compacted) and model.overflows == 0
    groups = between_summaries(model)
    assert len(groups) == len(compacted) + 1
    assert all(len({request.system for request in group}) == 1 for group in groups)
    assert model.main[0].tools and len({json.dumps(request.tools) for request in model.main}) == 1
    page = (await client.get("/v1/profiles/p1/sessions/long/messages", params={"limit": 500})).json()
    assert [m["role"] for m in page["data"]] == ["user", "assistant"] * 30
    assert page["data"][0]["content"] == "turn 0 " + "u" * 4_000


async def test_paths_survive_two_compactions_verbatim(store):
    model = Counting(40_000)
    executor = await executor_for(store, model)
    write = ToolCall("c1", "write_file", json.dumps({"path": "/work/reports/q3-summary.md", "content": "x"}))
    await append(store, user("hi"), assistant("hello"), user("start"), assistant("ok"),
                 user("Please analyse /data/uploads/sales-2026-q3.csv for me."),
                 Message("assistant", tool_calls=(write,)),
                 Message("tool", (TextPart('{"bytes_written": 1}'),), tool_call_id="c1", tool_name="write_file"),
                 assistant("done"))
    for n in range(12):
        await turn(store, executor, f"filler {n} " + "f" * 16_000)
    assert len(model.summaries) >= 2
    _, files = compress.split_files((await store.get_session("p1", "s1")).summary)
    assert files[:2] == ["/data/uploads/sales-2026-q3.csv", "/work/reports/q3-summary.md"]
    assert "## Files referenced" not in model.summaries[1].messages[0].text
    assert "PREVIOUS SUMMARY:\nSUMMARY 1" in model.summaries[1].messages[0].text


async def test_a_first_message_over_the_head_limit_is_cut_in_the_request_only(store):
    model = Counting(40_000)
    executor = await executor_for(store, model)
    first = "q" * 40_000
    await turn(store, executor, first)
    for n in range(8):
        await turn(store, executor, f"filler {n} " + "f" * 16_000)
    assert model.summaries
    sent = model.main[-1].messages[0].text
    assert "OUTPUT TRUNCATED" in sent and compress.text_tokens(sent) <= compress.HEAD_MESSAGE_TOKENS + 50
    assert (await store.messages("s1"))[0].message.text == first


async def test_mostly_cjk_text_is_compacted_before_the_provider_overflows(store):
    model = Counting(40_000, reply="数" * 2_000)
    executor = await executor_for(store, model)
    for n in range(20):
        await turn(store, executor, f"第{n}轮 " + "问" * 1_000)
    assert model.summaries and model.overflows == 0


async def test_images_outside_the_tail_are_sent_as_a_note(store):
    model = Counting(40_000)
    executor = await executor_for(store, model)
    await store.ensure_session("p1", "s1")
    blob = await store.put_blob("p1", "s1", "image/png", "aGVsbG8=")
    await append(store, user("hi"), assistant("hello"), user("start"), assistant("ok"),
                 Message("user", (TextPart("look"), ImagePart("image/png", blob_id=blob))), assistant("seen"))
    for n in range(3):
        call = ToolCall(f"c{n}", "read_file", json.dumps({"path": f"/w/{n}.txt"}))
        await append(store, Message("assistant", tool_calls=(call,)),
                     Message("tool", (TextPart("r" * 48_000),), tool_call_id=f"c{n}", tool_name="read_file"))
    await append(store, assistant("read them"))
    await turn(store, executor, "next")
    assert model.summaries == []
    sent = model.main[-1].messages
    assert not any(isinstance(part, ImagePart) for message in sent for part in message.parts)
    assert [m.parts for m in sent if m.role == "user" and m.text.startswith("look")] == [
        (TextPart("look"), TextPart(compress.IMAGE_OMITTED))]


async def test_a_provider_that_counts_more_than_the_heuristic_gets_summaries_not_repeated_pruning(store):
    model = Counting(40_000, reply="r" * 600, factor=1.3)
    executor = await executor_for(store, model)
    await append(store, *[message for _ in range(20) for message in (user("t" * 4_000), assistant("a" * 4_000))])
    writes = []
    set_pruned, compact_session = store.set_pruned, store.compact_session

    async def pruned(session_id, pruned_id, **options):
        writes.append("pruned")
        await set_pruned(session_id, pruned_id, **options)

    async def compacted(session_id, **options):
        writes.append("summary")
        await compact_session(session_id, **options)

    store.set_pruned, store.compact_session = pruned, compacted
    for n in range(40):
        await turn(store, executor, f"turn {n} " + "u" * 600)
    assert model.summaries and model.overflows == 0
    assert "pruned" * 3 not in "".join(writes), writes


async def test_a_summary_call_fits_a_small_window(store):
    model = Counting(40_000, reply="ok")
    executor = await executor_for(store, model)
    await append(store, *[message for _ in range(40) for message in (user("t" * 6_000), assistant("a" * 6_000))])
    await turn(store, executor, "next")
    assert len(model.summaries) == 1 and model.overflows == 0
    assert compress.split_files((await store.get_session("p1", "s1")).summary)[0] == "SUMMARY 1"


async def test_a_middle_larger_than_the_summary_input_limit_is_cut_to_fit(store):
    model = Counting(400_000, reply="ok")
    executor = await executor_for(store, model)
    await append(store, *[message for _ in range(120) for message in (user("t" * 6_000), assistant("a" * 6_000))])
    await turn(store, executor, "next")
    assert len(model.summaries) == 1
    prompt = model.summaries[0].messages[0].text
    assert "OUTPUT TRUNCATED" in prompt
    assert compress.text_tokens(prompt) <= compress.SUMMARY_INPUT_TOKENS + 5_000


async def test_a_compaction_refreshes_the_memory_snapshot(store):
    model = Counting(40_000)
    executor = await executor_for(store, model)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True)
    await turn(store, executor, "turn 0 " + "u" * 4_000)
    frozen = (await store.get_session("p1", "s1")).system_prompt
    assert frozen and "FACT-ONE" not in frozen
    await store.apply_memory("p1", "memory", 2200, [{"action": "add", "content": "FACT-ONE"}])
    for n in range(1, 30):
        await turn(store, executor, f"turn {n} " + "u" * 4_000)
    assert model.summaries
    refreshed = (await store.get_session("p1", "s1")).system_prompt
    assert "FACT-ONE" in refreshed
    before, after = between_summaries(model)[:2]
    assert len(before) > 1 and len(after) > 1
    assert len({request.system for request in before}) == 1
    assert len({request.system for request in after}) == 1
    assert before[0].system == frozen != after[0].system
    assert after[0].system == refreshed
