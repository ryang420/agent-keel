"""Command runs: one command in the profile's sandbox, without a model (docs/design/2026-10-31-command-runs.md)."""

import pytest

from keel.store.base import SessionBusy

SANDBOX = {"image": "img", "workdir": "/w"}
COMMAND = {"argv": ["python3", "job.py"], "cwd": "/w/job", "timeout_s": 60, "env": {"KEY": "value"}}


async def test_a_command_run_is_stored_without_a_model(store):
    await store.upsert_profile("p1", models=[], sandbox=SANDBOX, soul="", skills=[])
    await store.ensure_session("p1", "s1")
    run = await store.create_command_run(profile_id="p1", session_id="s1", command=COMMAND)
    assert (run.kind, run.command, run.status) == ("command", COMMAND, "queued")
    assert (run.provider, run.model, run.reasoning_effort, run.input, run.instructions) == (None, None, None, [], None)
    assert (await store.get_run("p1", run.id)).command == COMMAND
    with pytest.raises(SessionBusy):
        await store.create_command_run(profile_id="p1", session_id="s1", command=COMMAND)


async def test_a_model_run_reads_as_kind_model(store):
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[])
    await store.ensure_session("p1", "s1")
    run = await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort=None,
                                 input_parts=[{"type": "text", "text": "hi"}], instructions=None)
    assert (run.kind, run.command) == ("model", None)
    assert (await store.get_run("p1", run.id)).kind == "model"


async def test_a_coordinated_claim_carries_the_command(store):
    await store.upsert_profile("p1", models=[], sandbox=SANDBOX, soul="", skills=[])
    await store.ensure_session("p1", "s1")
    run = await store.create_command_run(profile_id="p1", session_id="s1", command=COMMAND)
    claim = await store.claim_run_with_usage("w1", 30, deployment="dep", daemon_id="daemon-1")
    assert claim.run.id == run.id and claim.use is not None
    assert (claim.run.kind, claim.run.command) == ("command", COMMAND)


async def test_the_schema_refuses_a_model_run_without_a_model_and_a_command_run_without_a_command(store):
    await store.upsert_profile("p1", models=[], sandbox=SANDBOX, soul="", skills=[])
    for run_id, kind in (("run_a", "model"), ("run_b", "command")):
        with pytest.raises(Exception, match="runs_kind_fields"):
            await store.db.execute(
                "INSERT INTO runs (id, profile_id, session_id, status, input, created_at, kind) "
                "VALUES (?, 'p1', ?, 'queued', '[]', 1, ?)", run_id, run_id, kind)
