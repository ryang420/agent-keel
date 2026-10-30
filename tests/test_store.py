import asyncio

import pytest

from keel.models import ImagePart, Message, ReasoningItem, TextPart, ToolCall, Usage
from keel.store.base import (WORKER_LOST_MESSAGE, SessionBusy, SessionNotFound, TerminalEventType, TitleInUse,
                             UnknownRun)

MODELS = [{"provider": "fake", "model": "m"}]


async def make_run(store, session_id="s1", profile_id="p1"):
    await store.ensure_session(profile_id, session_id)
    return await store.create_run(profile_id=profile_id, session_id=session_id, provider="fake", model="m",
                                  reasoning_effort="low", input_parts=[{"type": "text", "text": "hi"}],
                                  instructions="be brief")


async def test_profiles_upsert_and_read(store):
    first = await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="You help.", skills=["a"])
    assert first.models == MODELS and first.soul == "You help." and first.skills == ["a"]
    assert first.memory is False
    remembered = await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="You help.", skills=["a"],
                                            memory=True)
    assert remembered.memory is True
    second = await store.upsert_profile("p1", models=[], sandbox={"image": "x"}, soul="", skills=[])
    assert second.models == [] and second.sandbox == {"image": "x"} and second.memory is False
    assert await store.get_profile("missing") is None


async def test_a_session_belongs_to_one_profile(store):
    created = await store.ensure_session("p1", "s1")
    again = await store.ensure_session("p1", "s1")
    assert created.id == again.id == "s1" and created.message_count == 0
    with pytest.raises(SessionNotFound):
        await store.ensure_session("p2", "s1")
    assert await store.get_session("p2", "s1") is None


async def test_messages_keep_order_shape_and_ids(store, clock):
    await store.ensure_session("p1", "s1")
    call = ToolCall("c1", "terminal", "{}")
    item = ReasoningItem("fake", "m", "reasoning", {"encrypted_content": "e"})
    first = await store.append_message("s1", "run_1", Message("user", (TextPart("q"),)))
    clock.now += 1
    second = await store.append_message("s1", "run_1", Message("assistant", (TextPart("a"),),
                                                               tool_calls=(call,), reasoning=(item,)))
    third = await store.append_message("s1", "run_1", Message("tool", (TextPart("out"),),
                                                              tool_call_id="c1", tool_name="terminal"))
    assert first.id < second.id < third.id
    stored = await store.messages("s1")
    assert [m.message for m in stored] == [first.message, second.message, third.message]
    assert stored[1].message.tool_calls == (call,) and stored[1].message.reasoning == (item,)
    assert await store.last_message_id("s1") == third.id
    assert [m.id for m in await store.messages_after("s1", first.id)] == [second.id, third.id]
    page = await store.messages_page("s1", limit=2, offset=0)
    assert [m.id for m in page] == [second.id, third.id]
    assert [m.id for m in await store.messages_page("s1", limit=2, offset=2)] == [first.id]
    session = await store.get_session("p1", "s1")
    assert session.message_count == 3 and session.last_active_at == clock.now
    assert await store.last_message_id("empty") == 0


async def test_titles_follow_precedence_and_stay_unique(store):
    await store.ensure_session("p1", "s1")
    await store.ensure_session("p1", "s2")
    assert (await store.set_title("p1", "s1", "Draft", "derived")).title == "Draft"
    assert (await store.set_title("p1", "s1", "Model title", "llm")).title == "Model title"
    assert (await store.set_title("p1", "s1", "Mine", "user")).title_source == "user"
    assert (await store.set_title("p1", "s1", "Later model title", "llm")).title == "Mine"
    with pytest.raises(TitleInUse):
        await store.set_title("p1", "s2", "Mine", "user")
    await store.ensure_session("p2", "s3")
    assert (await store.set_title("p2", "s3", "Mine", "user")).title == "Mine"
    with pytest.raises(SessionNotFound):
        await store.set_title("p1", "nope", "x", "user")


async def test_sessions_list_newest_first_and_delete_removes_everything(store, clock):
    await store.ensure_session("p1", "old")
    clock.now += 10
    run = await make_run(store, "new")
    await store.append_message("new", run.id, Message("user", (TextPart("q"),)))
    await store.append_event(run.id, "run.started", {})
    assert [s.id for s in await store.list_sessions("p1")] == ["new", "old"]
    await store.finish_run(run.id, "completed")
    assert await store.delete_session("p1", "new") is True
    assert await store.delete_session("p1", "new") is False
    assert await store.messages("new") == [] and await store.get_run("p1", run.id) is None
    assert await store.events_after(run.id, 0) == []


async def test_a_run_is_claimed_once_leased_and_finished_once(store, clock):
    run = await make_run(store)
    assert run.status == "queued" and run.input == [{"type": "text", "text": "hi"}]
    assert await store.count_in_flight() == 1
    assert (await store.active_run_for_session("s1")).id == run.id
    claimed = await store.claim_run("w1", clock.now + 30)
    assert claimed.id == run.id and claimed.status == "running" and claimed.lease_owner == "w1"
    assert await store.claim_run("w2", clock.now + 30) is None
    assert await store.renew_lease(run.id, "w1", clock.now + 60) == "running"
    assert await store.renew_lease(run.id, "w2", clock.now + 60) is None
    stopping = await store.request_stop("p1", run.id)
    assert stopping.run.status == "stopping" and stopping.event is None
    assert await store.renew_lease(run.id, "w1", clock.now + 90) == "stopping"
    usage = Usage(10, 2, 8, 0)
    transition = await store.finish_run(run.id, "completed", output="done", usage=usage)
    assert transition is not None and transition.event is not None
    assert transition.event.type == "run.completed"
    again = await store.finish_run(run.id, "failed", error_code="x", error="late")
    assert again is not None and again.event is None and again.run.status == "completed"
    finished = await store.get_run("p1", run.id)
    assert (finished.status, finished.output, finished.usage) == ("completed", "done", usage)
    assert finished.lease_owner is None and finished.finished_at == clock.now
    assert await store.count_in_flight() == 0 and await store.active_run_for_session("s1") is None
    assert await store.get_run("p2", run.id) is None


async def test_stopping_a_queued_run_cancels_it(store):
    run = await make_run(store)
    first = await store.request_stop("p1", run.id)
    assert first.run.status == "cancelled" and first.event is not None
    assert first.event.type == "run.cancelled" and first.event.data == {}
    again = await store.request_stop("p1", run.id)
    assert again.run.status == "cancelled" and again.event is None
    assert await store.request_stop("p1", "run_missing") is None


async def test_expired_leases_fail_their_runs(store, clock):
    store.coordination_clock = clock  # lease deadlines on the fake clock, injected explicitly
    run = await make_run(store)
    await store.claim_run("w1", clock.now + 30)
    assert await store.expire_leases() == []
    clock.now += 31
    expired = await store.expire_leases()
    assert [t.run.id for t in expired] == [run.id]
    assert expired[0].run.status == "failed" and expired[0].run.error_code == "worker_lost"
    assert expired[0].event is not None and expired[0].event.type == "run.failed"


async def test_finish_run_commits_the_canonical_event_with_the_state(store, clock):
    usage = Usage(10, 2, 8, 0)
    completed = await make_run(store, "completed")
    await store.claim_run("w1", clock.now + 30)
    await store.set_current_exec(completed.id, "x1")
    transition = await store.finish_run(completed.id, "completed", output="done", usage=usage, incomplete=True)
    assert transition.run.finished_at == transition.event.at == clock.now
    assert transition.event.type == "run.completed"
    assert transition.event.data == {"output": "done", "usage": usage.to_dict(), "incomplete": True}
    saved = await store.get_run("p1", completed.id)
    assert saved.current_exec_id == "x1"  # a convenience pointer, kept as evidence
    assert saved.lease_owner is None and saved.lease_expires_at is None

    failed = await make_run(store, "failed")
    transition = await store.finish_run(failed.id, "failed", error_code="bad_request", error="nope")
    assert transition.event.type == "run.failed"
    assert transition.event.data == {"error_code": "bad_request", "message": "nope"}

    cancelled = await make_run(store, "cancelled")
    transition = await store.finish_run(cancelled.id, "cancelled")
    assert transition.event.type == "run.cancelled" and transition.event.data == {}
    assert [e.data for e in await store.events_after(cancelled.id, 0)] == [{}]


async def test_finish_run_validates_its_arguments_before_reading_the_run(store):
    run = await make_run(store)
    invalid = [
        dict(status="running"),
        dict(status="failed"),
        dict(status="failed", error_code="x"),
        dict(status="failed", error="x"),
        dict(status="cancelled", incomplete=True),
    ]
    for kwargs in invalid:
        with pytest.raises(ValueError):
            await store.finish_run(run.id, **kwargs)
    assert (await store.get_run("p1", run.id)).status == "queued"
    assert await store.events_after(run.id, 0) == []
    assert await store.finish_run("missing", "completed") is None
    # Validation does not depend on the run still being active: a retry with bad arguments is a bug, not a no-op.
    await store.finish_run(run.id, "completed")
    with pytest.raises(ValueError):
        await store.finish_run(run.id, "failed", error_code="x", error="")
    assert len(await store.events_after(run.id, 0)) == 1


async def test_expire_leases_handles_a_bounded_batch(store, clock):
    store.coordination_clock = clock  # lease deadlines on the fake clock, injected explicitly
    runs = []
    for index in range(5):
        run = await make_run(store, f"s{index}")
        await store.claim_run("dead", clock.now + 30)
        runs.append(run)
    clock.now += 31
    assert [len(await store.expire_leases(batch=2)) for _ in range(3)] == [2, 2, 1]
    assert await store.expire_leases(batch=2) == []
    for run in runs:
        events = await store.events_after(run.id, 0)
        assert [e.type for e in events] == ["run.failed"]
        assert events[0].data == {"error_code": "worker_lost", "message": WORKER_LOST_MESSAGE}
        assert events[0].at == clock.now
        saved = await store.get_run("p1", run.id)
        assert (saved.status, saved.finished_at, saved.lease_owner) == ("failed", clock.now, None)


async def test_a_failure_while_writing_events_rolls_the_whole_expiry_batch_back(store, clock):
    store.coordination_clock = clock  # lease deadlines on the fake clock, injected explicitly
    runs = []
    for index in range(3):
        runs.append(await make_run(store, f"s{index}"))
        await store.claim_run("dead", clock.now + 30)
    clock.now += 31
    real = store._insert_event_locked
    calls = 0

    async def flaky(run_id, type_, data, at):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("event write failed")
        return await real(run_id, type_, data, at)

    store._insert_event_locked = flaky
    try:
        with pytest.raises(RuntimeError):
            await store.expire_leases()
    finally:
        store._insert_event_locked = real
    for run in runs:
        saved = await store.get_run("p1", run.id)
        assert saved.status == "running" and saved.lease_owner == "dead"
        assert await store.events_after(run.id, 0) == []
    assert len(await store.expire_leases()) == 3


async def test_cancelling_a_finish_before_commit_rolls_the_transition_back(store, clock):
    run = await make_run(store)
    await store.claim_run("w1", clock.now + 30)
    entered, release = asyncio.Event(), asyncio.Event()
    real = store._insert_event_locked

    async def blocking(*args):
        entered.set()
        await release.wait()
        return await real(*args)

    store._insert_event_locked = blocking
    task = asyncio.create_task(store.finish_run(run.id, "completed", output="x"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store._insert_event_locked = real
    saved = await store.get_run("p1", run.id)
    assert saved.status == "running" and saved.finished_at is None
    assert await store.events_after(run.id, 0) == []
    assert (await store.finish_run(run.id, "completed", output="x")).event is not None


async def test_a_lost_acknowledgement_can_be_retried_without_a_second_event(store):
    run = await make_run(store)
    real = store.finish_run

    async def commit_then_lose(*args, **kwargs):
        await real(*args, **kwargs)
        raise RuntimeError("the connection dropped after commit")

    store.finish_run = commit_then_lose
    try:
        with pytest.raises(RuntimeError):
            await store.finish_run(run.id, "completed", output="first")
    finally:
        store.finish_run = real
    retry = await store.finish_run(run.id, "completed", output="second")
    assert retry.event is None
    assert (retry.run.output, retry.run.status) == ("first", "completed")
    assert [e.data["output"] for e in await store.events_after(run.id, 0)] == ["first"]


async def test_append_event_rejects_terminal_types_and_unknown_runs(store):
    run = await make_run(store)
    for type_ in ("run.completed", "run.failed", "run.cancelled"):
        with pytest.raises(TerminalEventType):
            await store.append_event(run.id, type_, {})
    assert await store.events_after(run.id, 0) == []
    with pytest.raises(UnknownRun):
        await store.append_event("run_missing", "run.started", {})
    rows = await store.db.fetch("SELECT COUNT(*) AS n FROM run_events WHERE run_id = ?", "run_missing")
    assert rows[0]["n"] == 0


async def test_append_event_stops_at_the_terminal_event(store, clock):
    run = await make_run(store)
    assert (await store.append_event(run.id, "run.started", {})).seq == 1
    await store.claim_run("w1", clock.now + 30)
    await store.request_stop("p1", run.id)  # stopping: still accepts ordinary events
    assert (await store.append_event(run.id, "message.delta", {"text": "x"})).seq == 2
    await store.finish_run(run.id, "cancelled")
    assert await store.append_event(run.id, "message.delta", {"text": "late"}) is None
    assert [e.type for e in await store.events_after(run.id, 0)] == [
        "run.started", "message.delta", "run.cancelled"]


async def test_list_runs_by_status(store, clock):
    first = await make_run(store, "s1")
    clock.now += 1
    await make_run(store, "s2")
    await store.claim_run("w1", 9_999)
    running = await store.list_runs("p1", ("running",))
    assert [r.id for r in running] == [first.id]
    assert len(await store.list_runs("p1", ("queued", "running"))) == 2


async def test_events_are_numbered_per_run(store):
    run = await make_run(store)
    other_run = await make_run(store, "s2")
    first = await store.append_event(run.id, "run.started", {"model": "m"})
    second = await store.append_event(run.id, "message.delta", {"text": "hi"})
    other = await store.append_event(other_run.id, "run.started", {})
    assert (first.seq, second.seq, other.seq) == (1, 2, 1)
    assert [e.type for e in await store.events_after(run.id, 0)] == ["run.started", "message.delta"]
    assert [e.data for e in await store.events_after(run.id, 1)] == [{"text": "hi"}]


async def test_ping(store):
    assert await store.ping() is True


async def test_stopping_a_finished_run_leaves_it_finished(store):
    run = await make_run(store)
    await store.claim_run("w1", 9_999)
    await store.finish_run(run.id, "completed", output="done")
    transition = await store.request_stop("p1", run.id)
    assert transition.run.status == "completed" and transition.event is None
    assert await store.request_stop("p2", run.id) is None


async def test_title_precedence_is_enforced_and_unknown_source_rejected(store):
    await store.ensure_session("p1", "s1")
    await store.set_title("p1", "s1", "Mine", "user")
    assert (await store.set_title("p1", "s1", "Other", "derived")).title == "Mine"
    with pytest.raises(ValueError):
        await store.set_title("p1", "s1", "x", "bogus")


async def test_a_session_with_an_active_run_cannot_be_deleted(store):
    run = await make_run(store)
    with pytest.raises(SessionBusy):
        await store.delete_session("p1", "s1")
    await store.finish_run(run.id, "completed")
    assert await store.delete_session("p1", "s1") is True


async def test_one_in_flight_run_per_session(store):
    run = await make_run(store)
    with pytest.raises(SessionBusy):
        await make_run(store)
    await store.finish_run(run.id, "completed")
    assert (await make_run(store)).status == "queued"


async def test_list_runs_with_no_statuses_is_empty(store):
    await make_run(store)
    assert await store.list_runs("p1", ()) == []


async def test_a_session_prompt_is_set_once(store):
    await store.ensure_session("p1", "s1")
    first = await store.set_session_prompt("s1", "PROMPT A", "fake", "m")
    second = await store.set_session_prompt("s1", "PROMPT B", "fake", "m2")
    assert first.system_prompt == second.system_prompt == "PROMPT A"
    assert (second.provider, second.model) == ("fake", "m")


async def test_the_session_cwd_is_kept(store):
    await store.ensure_session("p1", "s1")
    assert (await store.get_session("p1", "s1")).cwd is None
    await store.set_session_cwd("s1", "/work/sub")
    assert (await store.get_session("p1", "s1")).cwd == "/work/sub"


async def test_blobs_round_trip_and_leave_with_their_session(store):
    await store.ensure_session("p1", "s1")
    blob = await store.put_blob("p1", "s1", "image/png", "aGVsbG8=")
    assert blob.startswith("blob_")
    assert await store.get_blobs([blob], profile_id="p1", session_id="s1") == {
        blob: ("image/png", "aGVsbG8=")}
    assert await store.get_blobs([blob], profile_id="p2", session_id="s1") == {}
    assert await store.get_blobs([blob], profile_id="p1", session_id="s2") == {}
    assert await store.get_blobs([], profile_id="p1", session_id="s1") == {}
    await store.delete_session("p1", "s1")
    assert await store.get_blobs([blob], profile_id="p1", session_id="s1") == {}


async def test_delete_blobs(store):
    await store.ensure_session("p1", "s1")
    blob = await store.put_blob("p1", "s1", "image/png", "aGVsbG8=")
    await store.delete_blobs([blob])
    assert await store.get_blobs([blob], profile_id="p1", session_id="s1") == {}


async def test_image_parts_are_stored_as_references(store):
    await store.ensure_session("p1", "s1")
    blob = await store.put_blob("p1", "s1", "image/png", "aGVsbG8=")
    await store.append_message("s1", None, Message("user", (TextPart("look"), ImagePart("image/png", blob_id=blob))))
    stored = (await store.messages("s1"))[0].message
    assert stored.parts == (TextPart("look"), ImagePart("image/png", "", blob))


async def test_the_current_exec_is_recorded(store):
    await store.ensure_session("p1", "s1")
    run = await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}], instructions=None)
    assert run.current_exec_id is None
    await store.set_current_exec(run.id, "x1")
    assert (await store.get_run("p1", run.id)).current_exec_id == "x1"
    await store.set_current_exec(run.id, None)
    assert (await store.get_run("p1", run.id)).current_exec_id is None


async def test_compaction_state_is_stored_and_cleared(store):
    await store.ensure_session("p1", "s1")
    await store.set_session_prompt("s1", "OLD", "fake", "m")
    await store.set_context_anchor("s1", 1_234, 7)
    session = await store.get_session("p1", "s1")
    assert (session.context_tokens, session.context_message_id, session.pruned_id) == (1_234, 7, None)
    await store.set_pruned("s1", 9, failed_at=55.0)
    session = await store.get_session("p1", "s1")
    assert (session.pruned_id, session.context_tokens, session.context_message_id) == (9, None, None)
    assert session.compaction_failed_at == 55.0
    await store.set_context_anchor("s1", 99, 10)
    await store.set_pruned("s1", 11)
    session = await store.get_session("p1", "s1")
    assert (session.pruned_id, session.context_tokens, session.compaction_failed_at) == (11, None, 55.0)
    await store.compact_session("s1", summary="SUM", head_id=3, tail_id=12, system_prompt="NEW")
    session = await store.get_session("p1", "s1")
    assert (session.summary, session.summary_head_id, session.summary_tail_id, session.pruned_id) == ("SUM", 3, 12, 12)
    assert (session.system_prompt, session.context_tokens, session.compaction_failed_at) == ("NEW", None, None)


async def test_pruning_deletes_only_events_of_runs_finished_before_the_cutoff(store, clock):
    from keel.models import Message, TextPart

    await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=[])
    runs = []
    for session in ("s1", "s2", "s3"):
        await store.ensure_session("p1", session)
        runs.append(await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m",
                                           reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                           instructions=None))
        await store.append_message(session, runs[-1].id, Message("user", (TextPart("hi"),)))
    for run in runs:
        for _ in range(5):
            await store.append_event(run.id, "message.delta", {"text": "x"})
    await store.finish_run(runs[0].id, "completed", output="old")  # finished at 1000
    clock.now += 100
    await store.finish_run(runs[1].id, "completed", output="new")  # finished at 1100; runs[2] is still queued
    assert await store.prune_events(1050, batch=2) == 6  # five deltas and the terminal event
    assert await store.events_after(runs[0].id, 0) == []
    assert len(await store.events_after(runs[1].id, 0)) == 6
    assert len(await store.events_after(runs[2].id, 0)) == 5
    old = await store.get_run("p1", runs[0].id)
    assert (old.status, old.output) == ("completed", "old")
    assert len(await store.messages("s1")) == 1
    assert await store.prune_events(1050) == 0


async def test_pruning_with_a_lower_bound_keeps_the_events_of_runs_finished_before_it(store, clock):
    await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=[])
    runs = []
    for session in ("s1", "s2"):
        await store.ensure_session("p1", session)
        runs.append(await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m",
                                           reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                           instructions=None))
        for _ in range(3):
            await store.append_event(runs[-1].id, "message.delta", {"text": "x"})
        await store.finish_run(runs[-1].id, "completed")  # at 1000, then at 1100
        clock.now += 100
    assert await store.prune_events(1200, finished_after=1050, batch=2) == 4  # three deltas and the terminal event
    assert len(await store.events_after(runs[0].id, 0)) == 4
    assert await store.events_after(runs[1].id, 0) == []
    assert await store.prune_events(1200, finished_after=1000) == 4


async def test_memory_writes_are_a_transaction(store):
    await store.apply_memory("p1", "memory", 100, [{"action": "add", "content": "one"}])
    await store.apply_memory("p1", "memory", 100, [{"action": "add", "content": "two"}])
    assert await store.memory_entries("p1") == {"memory": ["one", "two"], "user": []}

    await asyncio.gather(
        store.apply_memory("p2", "memory", 100, [{"action": "add", "content": "left"}]),
        store.apply_memory("p2", "memory", 100, [{"action": "add", "content": "right"}]))
    assert sorted((await store.memory_entries("p2"))["memory"]) == ["left", "right"]

    # The row already exists, and the read pauses, so the two adds overlap. A lost update keeps only one of them.
    await store.apply_memory("p3", "memory", 100, [{"action": "add", "content": "base"}])
    original = store.db.fetch

    async def pausing(sql, *args):
        rows = await original(sql, *args)
        if "FROM memory_entries" in sql:
            await asyncio.sleep(0.1)
        return rows

    store.db.fetch = pausing
    try:
        await asyncio.gather(
            store.apply_memory("p3", "memory", 100, [{"action": "add", "content": "left"}]),
            store.apply_memory("p3", "memory", 100, [{"action": "add", "content": "right"}]))
    finally:
        store.db.fetch = original
    assert sorted((await store.memory_entries("p3"))["memory"]) == ["base", "left", "right"]


async def test_a_failed_batch_stores_nothing(store):
    result = await store.apply_memory("p1", "memory", 1, [
        {"action": "add", "content": "a"}, {"action": "add", "content": "b"}])
    assert result["success"] is False and result["entries"] == []
    assert await store.memory_entries("p1") == {"memory": [], "user": []}


async def test_reset_memory_turns_zeroes_the_counter(store):
    await store.ensure_session("p1", "s1")
    await store.db.execute("UPDATE sessions SET turns_since_memory = 7 WHERE id = ?", "s1")
    await store.reset_memory_turns("s1")
    assert (await store.get_session("p1", "s1")).turns_since_memory == 0
