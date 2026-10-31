"""skill_manage, and learned skills through skills_list and skill_view without a sandbox (learning design §2, §3)."""

import json

from keel.skills import SkillInfo
from keel.store.base import SkillSource
from keel.tools import ToolContext, run_tool


async def setup(store, *, learning=True, skills=()):
    await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=list(skills), memory=True,
                               learning=learning)
    await store.ensure_session("p1", "s1")
    return ToolContext(store=store, profile_id="p1", session_id="s1", run_id="r1", tool_call_id="c1")


async def call(name, args, ctx):
    outcome = await run_tool(name, json.dumps(args), ctx)
    return outcome, json.loads(outcome.content)


CREATE = {"action": "create", "name": "csv-report", "description": "Weekly CSV report", "body": "1. Load."}


async def test_create_list_revise_retire_from_the_foreground(store):
    ctx = await setup(store)
    _, made = await call("skill_manage", CREATE, ctx)
    assert made["success"] and made["skill"]["version"] == 1
    skill = await store.learned_skill("p1", made["skill"]["id"])
    assert skill.origin == "foreground"
    _, listing = await call("skill_manage", {"action": "list"}, ctx)
    assert [s["name"] for s in listing["skills"]] == ["csv-report"]
    ctx.tool_call_id = "c2"
    _, revised = await call("skill_manage", {"action": "revise", "skill_id": skill.id, "expected_version": 1,
                                              "body": "1. Load. 2. Group."}, ctx)
    assert revised["skill"]["version"] == 2
    ctx.tool_call_id = "c3"
    outcome, stale = await call("skill_manage", {"action": "retire", "skill_id": skill.id, "expected_version": 1},
                                ctx)
    assert outcome.error and stale["code"] == "version_conflict" and stale["skill"]["version"] == 2


async def test_a_replayed_tool_call_returns_the_first_result(store):
    ctx = await setup(store)
    _, first = await call("skill_manage", CREATE, ctx)
    _, again = await call("skill_manage", CREATE, ctx)
    assert again["replayed"] is True and again["skill"] == first["skill"]


async def test_the_model_cannot_rollback_suspend_or_name_an_owner(store):
    ctx = await setup(store)
    outcome, body = await call("skill_manage", {"action": "suspend", "skill_id": "x", "expected_version": 1}, ctx)
    assert outcome.error
    _, made = await call("skill_manage", {**CREATE, "profile_id": "p2"}, ctx)
    assert made["success"] and await store.learned_skills("p2") == []


async def test_skill_manage_refuses_when_learning_is_off(store):
    ctx = await setup(store, learning=False)
    outcome, body = await call("skill_manage", CREATE, ctx)
    assert outcome.error and "not available" in body["error"]


async def test_skill_view_serves_a_learned_skill_without_a_sandbox_and_records_the_load(store):
    ctx = await setup(store)
    made = await store.change_learned_skill("p1", CREATE, source=SkillSource("consumer"))
    outcome, body = await call("skill_view", {"name": "csv-report"}, ctx)
    assert not outcome.error and body["source"] == "learned" and body["version"] == 1
    assert body["content"].startswith("---\nname: csv-report\n") and body["content"].endswith("1. Load.")
    assert await store.skill_load_counts("p1", since=0) == {(made["skill"]["id"], 1): 1}
    outcome, body = await call("skill_view", {"name": "csv-report", "file_path": "x.md"}, ctx)
    assert outcome.error and "no linked files" in body["error"]


async def test_skill_view_says_a_suspended_or_retired_skill_is_no_longer_available(store):
    ctx = await setup(store)
    skill_id = (await store.change_learned_skill("p1", CREATE, source=SkillSource("consumer")))["skill"]["id"]
    await store.change_learned_skill("p1", {"action": "suspend", "skill_id": skill_id, "expected_version": 1},
                                     source=SkillSource("consumer"))
    _, body = await call("skill_view", {"name": "csv-report"}, ctx)
    assert "suspended" in body["error"] and "no longer available" in body["error"]
    await store.change_learned_skill("p1", {"action": "retire", "skill_id": skill_id, "expected_version": 2},
                                     source=SkillSource("consumer"))
    _, body = await call("skill_view", {"name": "csv-report"}, ctx)
    assert "retired" in body["error"] and "no longer available" in body["error"]


async def test_skills_list_shows_active_learned_skills_under_learned_and_hides_them_when_off(store):
    ctx = await setup(store)
    await store.change_learned_skill("p1", CREATE, source=SkillSource("consumer"))
    _, body = await call("skills_list", {}, ctx)
    assert body["skills"] == [{"name": "csv-report", "description": "Weekly CSV report", "category": "learned"}]
    await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=[], memory=True, learning=False)
    _, body = await call("skills_list", {}, ctx)
    assert body["skills"] == []
    _, body = await call("skill_view", {"name": "csv-report"}, ctx)
    assert "not found" in body["error"]


async def test_a_deployment_skill_wins_a_name_clash(store):
    ctx = await setup(store)
    await store.change_learned_skill("p1", CREATE, source=SkillSource("consumer"))
    ctx.skills = (SkillInfo("csv-report", "Shipped", None, "/skills/csv-report"),)
    _, body = await call("skills_list", {}, ctx)
    assert [(s["name"], s["category"]) for s in body["skills"]] == [("csv-report", None)]
