"""python -m keel.admin import-sessions: conversations as JSON lines in, one result line per conversation out."""

import json
from datetime import UTC, datetime

from keel.admin import import_lines
from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from tests.sandbox_support import ClaimedExecutor, local_coordinator


def line(**overrides):
    record = {
        "profile": "p1", "tenant_id": 7, "session_id": "console-a1", "title": "Row count", "title_source": "llm",
        "created_at": 100.0, "last_active_at": 103.0,
        "messages": [
            {"source_id": 11, "role": "user", "text": "How many rows?", "created_at": 100.0},
            {"source_id": 12, "role": "assistant", "text": "",
             "tool_calls": [{"id": "c1", "name": "terminal", "arguments": "{\"command\": \"wc -l f\"}"}],
             "created_at": 101.0},
            {"source_id": 13, "role": "tool", "text": "42 f", "tool_call_id": "c1", "tool_name": "terminal",
             "created_at": 102.0},
            {"source_id": 14, "role": "assistant", "text": "42 rows.", "created_at": 103.0},
        ],
    }
    record.update(overrides)
    return json.dumps(record)


async def test_a_conversation_is_imported_and_its_message_ids_are_mapped(store):
    [result] = await import_lines(store, [line()], dry_run=False)
    assert result["status"] == "imported" and result["profile_created"] and result["reason"] is None
    stored = await store.messages("console-a1")
    assert result["message_ids"] == {"11": stored[0].id, "12": stored[1].id, "13": stored[2].id, "14": stored[3].id}


async def test_a_second_run_reports_exists(store):
    await import_lines(store, [line()], dry_run=False)
    [again] = await import_lines(store, [line()], dry_run=False)
    assert (again["status"], again["message_ids"]) == ("exists", {})


async def test_a_dry_run_writes_nothing(store):
    [result] = await import_lines(store, [line()], dry_run=True)
    assert result["status"] == "valid"
    assert await store.get_session("p1", "console-a1") is None and await store.get_profile("p1") is None


async def test_each_bad_conversation_is_invalid_and_the_rest_still_import(store):
    bad = [
        "not json",
        line(session_id="../x"),
        line(session_id="console-b1", tenant_id=None),
        line(session_id="console-b2", title_source="robot"),
        line(session_id="console-b3", messages=[{"source_id": 1, "role": "system", "text": "x", "created_at": 1.0}]),
        line(session_id="console-b4", messages=[{"source_id": 1, "role": "tool", "text": "x", "created_at": 1.0}]),
    ]
    results = await import_lines(store, bad + [line(session_id="console-ok")], dry_run=False)
    assert [r["status"] for r in results] == ["invalid"] * 6 + ["imported"]
    assert all(r["reason"] for r in results[:6])
    assert await store.get_session("p1", "console-b4") is None


async def test_a_profile_of_another_tenant_is_invalid(store):
    await import_lines(store, [line()], dry_run=False)
    [result] = await import_lines(store, [line(session_id="console-b1", tenant_id=8)], dry_run=False)
    assert (result["status"], result["reason"]) == ("invalid", "profile belongs to another tenant")


async def test_an_imported_conversation_is_replayed_to_the_model_on_the_next_run(store, tmp_path, monkeypatch):
    """Continuable: the next turn's model call carries the imported turns, tool call and result paired."""
    await import_lines(store, [line()], dry_run=False)
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="SOUL", skills=[],
                               sandbox={"image": "local", "workdir": str(tmp_path)}, tenant_id=7)
    provider.script = [[TextDelta("Still 42."), Completed(Usage(1, 1, 0, 0))]]
    await store.create_run(profile_id="p1", session_id="console-a1", provider="fake", model="m",
                           reasoning_effort=None, input_parts=[{"type": "text", "text": "And now?"}],
                           instructions=None)

    async def no_sleep(_):
        pass

    coordinator, _ = await local_coordinator(store, tmp_path / "bin", monkeypatch)
    executor = ClaimedExecutor(store, InMemoryEventBus(), registry, coordinator=coordinator, kill_grace_s=0.5,
                               sleep=no_sleep, max_iterations=3, now=lambda: datetime(2026, 10, 16, tzinfo=UTC))
    run = await executor.claim_next()
    await executor.execute(run)
    sent = provider.requests[0].messages
    assert [m.role for m in sent] == ["user", "assistant", "tool", "assistant", "user"]
    assert sent[1].tool_calls[0].id == sent[2].tool_call_id == "c1"
    assert (await store.get_run("p1", run.id)).status == "completed"
