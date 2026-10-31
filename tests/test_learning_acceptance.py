"""Procedural learning acceptance (design §10), deterministic rows. Scripted model output proves the plumbing; it is
not evidence that learning helps.

| §10 row | Proved by |
|---|---|
| 1 "from now on" → foreground skill, next session's index | tests/test_runner_learning.py::test_a_from_now_on_request_becomes_a_skill_listed_in_the_next_session_only |
| 2 same correction in three sessions | test_the_same_correction_in_three_sessions_becomes_one_skill_and_never_a_duplicate |
| 3 two overlapping skills | test_two_overlapping_skills_are_merged_and_the_other_retired |
| 4 method only in a tool result | tests/test_review_observe.py::test_a_method_only_in_a_tool_result_cannot_be_cited |
| 5 thin evidence | tests/test_dream.py::test_thin_evidence_is_refused_to_the_model_and_nothing_is_consumed |
| 6 three down ratings | test_three_down_ratings_suspend_without_a_model_call_and_a_dream_revises |
| 7 consumer rollback | test_a_consumer_rollback_stands_against_older_observations |
| 8 foreground revise races a dream | test_a_foreground_revise_during_a_dream_discards_it_and_the_retry_uses_current_state |
| 9 dream killed mid-proposal / after commit | test_a_dream_killed_mid_proposal_writes_nothing_and_one_commit_stays_one |
| 10 learning disabled mid-dream | tests/test_dream.py::test_learning_turned_off_mid_dream_commits_nothing |
| 11 two tenants, two profiles | tests/test_learning_isolation_acceptance.py |
| 12 session/profile deletion and recreation | tests/test_learning_isolation_acceptance.py |
| 13 ordinary session with learning on | tests/test_runner_learning.py::test_a_from_now_on_request_becomes_a_skill_listed_in_the_next_session_only |
"""

import asyncio
import json

from keel.dream import Dreamer
from keel.providers.fake import FakeProvider
from keel.store.base import SkillSource

from tests.learning_support import (DONE, FORCE, OK, drain_dreams, drain_reviews, executor_for, learning_profile,
                                    no_sleep, observe, registry, seed_run, step, turn, user_message_ids)

CSV = {"action": "create", "name": "csv-export", "description": "Export a CSV after dropping empty rows",
       "body": "1. Drop empty rows.\n2. Export with a header row."}


async def corrected_sessions(store, clock, sessions, executor, reviewer_provider, providers):
    """Each session: the user's task and correction; its review records one `corrected` observation."""
    for session in sessions:
        await turn(store, executor, session, "Export the sales CSV. No: always drop empty rows first, it worked.")
        [message] = await user_message_ids(store, session)
        reviewer_provider.script += [step("observe", topic="csv export", situation="Exporting a CSV",
                                          approach="Drop empty rows before exporting", outcome="corrected",
                                          evidence=[message]), DONE]
        clock.now += 1
        await drain_reviews(store, providers)


async def test_the_same_correction_in_three_sessions_becomes_one_skill_and_never_a_duplicate(store, clock):
    store.coordination_clock = clock
    await learning_profile(store)
    model = FakeProvider(script=[])  # reviews and dreams; each review's steps are appended as its session ends
    providers = registry(model)
    executor = executor_for(store, FakeProvider(script=[OK] * 6))
    await corrected_sessions(store, clock, ("s1", "s2", "s3"), executor, model, providers)
    pending = await store.observations("p1", states=("pending",))
    assert len(pending) == 3 and {o.session_id for o in pending} == {"s1", "s2", "s3"}
    create = {k: v for k, v in CSV.items() if k != "action"}
    model.script += [step("create_skill", **create, observations=[o.id for o in pending], rationale="corrected thrice"),
                     DONE]
    await drain_dreams(store, providers)
    [skill] = await store.learned_skills("p1")
    [version] = await store.learned_skill_versions("p1", skill.id)
    assert (skill.origin, version.source_kind) == ("dream", "dream")
    assert set(version.observation_ids) == {o.id for o in pending}
    await corrected_sessions(store, clock, ("s4", "s5", "s6"), executor, model, providers)
    fresh = await store.observations("p1", states=("pending",))
    model.script += [step("create_skill", name="csv-export-steps", description="Export a CSV after dropping rows",
                          body="1. Drop empty rows.", observations=[o.id for o in fresh], rationale="again"),
                     DONE]
    await drain_dreams(store, providers)
    assert [s.name for s in await store.learned_skills("p1")] == ["csv-export"]
    refused = [r for r in model.requests if r.messages and r.messages[-1].role == "tool"]
    assert json.loads(refused[-1].messages[-1].text)["code"] == "duplicate"


async def test_two_overlapping_skills_are_merged_and_the_other_retired(store, clock):
    store.coordination_clock = clock
    await learning_profile(store)
    await seed_run(store)
    first = (await store.change_learned_skill("p1", CSV, source=SkillSource("consumer")))["skill"]
    second = (await store.change_learned_skill("p1", {"action": "create", "name": "csv-cleanup",
                                                      "description": "Clean a CSV export",
                                                      "body": "1. Remove blank lines."},
                                               source=SkillSource("consumer")))["skill"]
    clock.now += 10
    ids = [await observe(store, "s1"), await observe(store, "s2")]
    model = FakeProvider(script=[step("merge_skills", target_id=first["id"], source_ids=[second["id"]],
                                      body="1. Drop empty and blank rows.\n2. Export with a header row.",
                                      observations=ids, rationale="the same method"), DONE])
    await drain_dreams(store, registry(model))
    merged, retired = await store.learned_skill("p1", first["id"]), await store.learned_skill("p1", second["id"])
    assert (merged.state, retired.state) == ("active", "retired")
    *_, merge = await store.learned_skill_versions("p1", first["id"])
    *_, retire = await store.learned_skill_versions("p1", second["id"])
    assert (merge.change, merge.source_kind, set(merge.observation_ids)) == ("merge", "dream", set(ids))
    assert (retire.change, retire.rationale, retire.job_id) == ("retire", "merged into csv-export", merge.job_id)


async def test_three_down_ratings_suspend_without_a_model_call_and_a_dream_revises(store, clock):
    store.coordination_clock = clock
    await learning_profile(store)
    skill = (await store.change_learned_skill("p1", CSV, source=SkillSource("consumer")))["skill"]
    clock.now += 10
    executor_model = FakeProvider(script=[OK] * 3)
    executor = executor_for(store, executor_model)
    for index in range(3):
        run = await turn(store, executor, f"s{index}", "Export the CSV.")
        await store.record_skill_load("p1", f"s{index}", run.id, skill["id"], 1)
        await store.put_feedback("p1", run.id, "down", None)
    suspended = await store.learned_skill("p1", skill["id"])
    assert suspended.state == "suspended"  # suspended by the feedback alone: no Dreamer exists yet
    executor_model.script.append(OK)
    await turn(store, executor, "fresh", "Export the CSV again.")
    assert skill["name"] not in executor_model.requests[-1].system  # a suspended skill leaves the skills index
    feedback = await store.observations("p1", states=("pending",))
    assert [o.outcome for o in feedback] == ["feedback_down"] * 3
    model = FakeProvider(script=[step("revise_skill", skill_id=skill["id"], body="1. Drop empty rows.\n"
                                      "2. Check the delimiter.\n3. Export.", observations=[o.id for o in feedback],
                                      rationale="down-rated"), DONE])
    clock.now += 1
    await drain_dreams(store, registry(model))
    revised = await store.learned_skill("p1", skill["id"])
    assert (revised.state, revised.version) == ("active", 3)
    [job] = await store.list_dreams("p1")
    assert job.trigger == "feedback" and job.status == "done"


async def test_a_consumer_rollback_stands_against_older_observations(store, clock):
    store.coordination_clock = clock
    await learning_profile(store)
    await seed_run(store)
    skill = (await store.change_learned_skill("p1", CSV, source=SkillSource("consumer")))["skill"]
    clock.now += 10
    used = [await observe(store, "s1"), await observe(store, "s2")]
    older = [await observe(store, "s3"), await observe(store, "s4")]
    model = FakeProvider(script=[step("revise_skill", skill_id=skill["id"], body="1. Dream text.",
                                      observations=used, rationale="first"), DONE])
    await drain_dreams(store, registry(model))
    clock.now += 10
    rolled = await store.change_learned_skill("p1", {"action": "rollback", "skill_id": skill["id"],
                                                     "expected_version": 2, "version": 1},
                                              source=SkillSource("consumer"))
    assert rolled["success"]
    clock.now += 10
    await observe(store, "s5")  # new evidence makes the profile due; the cited ones stay older than the rollback
    model.script += [step("revise_skill", skill_id=skill["id"], body="1. Dream text.", observations=older,
                          rationale="again"), DONE]
    await drain_dreams(store, registry(model))
    current = await store.learned_skill("p1", skill["id"])
    assert (current.version, current.body) == (3, CSV["body"])
    assert json.loads(model.requests[-1].messages[-1].text)["code"] == "consumer_precedence"


async def test_a_foreground_revise_during_a_dream_discards_it_and_the_retry_uses_current_state(store, clock):
    store.coordination_clock = clock
    await learning_profile(store)
    await seed_run(store)
    skill = (await store.change_learned_skill("p1", CSV, source=SkillSource("dream", job_id="old")))["skill"]
    clock.now += 10
    ids = [await observe(store, "s1"), await observe(store, "s2")]

    class Racing(FakeProvider):
        async def stream(self, request):
            if len(self.requests) == 1:  # after the dream staged its revise, the user revises in the foreground
                await store.change_learned_skill("p1", {"action": "revise", "skill_id": skill["id"],
                                                        "expected_version": 1, "body": "1. Foreground text."},
                                                 source=SkillSource("foreground"))
            async for event in super().stream(request):
                yield event

    revise = step("revise_skill", skill_id=skill["id"], body="1. Dream text.", observations=ids, rationale="r")
    model = Racing(script=[revise, DONE, revise, DONE])
    await drain_dreams(store, registry(model))
    [job] = await store.list_dreams("p1")
    assert (job.status, job.error_code) == ("pending", "conflict")
    assert {o.state for o in await store.observations("p1")} == {"pending"}
    assert (await store.learned_skill("p1", skill["id"])).body == "1. Foreground text."
    clock.now += 60  # past the retry backoff
    await drain_dreams(store, registry(model))
    current = await store.learned_skill("p1", skill["id"])
    assert (current.version, current.body) == (3, "1. Dream text.")
    assert (await store.list_dreams("p1"))[0].status == "done"


async def test_a_dream_killed_mid_proposal_writes_nothing_and_one_commit_stays_one(store, clock):
    store.coordination_clock = clock
    await learning_profile(store)
    await seed_run(store)
    ids = [await observe(store, "s1"), await observe(store, "s2")]
    fields = {k: v for k, v in CSV.items() if k != "action"}
    create = step("create_skill", **fields, observations=ids, rationale="twice")
    hanging = FakeProvider(script=[create, ["hang"]])
    task = asyncio.create_task(Dreamer(store, registry(hanging), FORCE, sleep=no_sleep).run_once("w1"))
    for _ in range(200):
        if len(hanging.requests) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert await store.learned_skills("p1") == []
    [job] = await store.list_dreams("p1")
    assert (job.status, job.attempts) == ("pending", 0)
    model = FakeProvider(script=[create, DONE])
    await drain_dreams(store, registry(model))
    claim_rows = await store.db.fetch("SELECT id FROM dream_jobs WHERE status = 'done'")
    assert len(claim_rows) == 1 and len(await store.learned_skills("p1")) == 1
    await drain_dreams(store, registry(FakeProvider(script=[create, DONE])))  # nothing is due: nothing reapplied
    assert len(await store.learned_skills("p1")) == 1
    [version] = await store.learned_skill_versions("p1", (await store.learned_skills("p1"))[0].id)
    assert version.version == 1
