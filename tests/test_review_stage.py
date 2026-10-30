"""The reviewer's memory calls edit a staged copy; nothing reaches the store until the queue commits it."""

import json

from keel.memory import Slot
from keel.review_stage import CORRECTED, StagedMemory

LIMITS = {"memory": 2200, "user": 1375}


def stage(user=(), corrected=None):
    slots = {"user": [Slot(f"e{i}", text, 1) for i, text in enumerate(user)], "memory": []}
    return StagedMemory(slots, {"user": 3, "memory": 0}, LIMITS, corrected or {})


def test_edits_change_only_the_stage_and_are_proposed_per_touched_target():
    staged = stage(user=["likes tea"])
    out, error = staged.call(json.dumps({"target": "user", "operations": [
        {"action": "replace", "entry_id": "e0", "content": "likes green tea"},
        {"action": "add", "content": "works in UTC"}]}))
    assert not error and json.loads(out)["success"]
    [(target, (revision, before, after, changes))] = staged.proposals().items()
    assert target == "user" and revision == 3
    assert [slot.text for slot in before] == ["likes tea"]
    assert [slot.text for slot in after] == ["likes green tea", "works in UTC"]
    assert [change.action for change in changes] == ["replace", "add"]
    assert [item["text"] for item in staged.items()["user"]] == ["likes green tea", "works in UTC"]


def test_list_shows_the_stage():
    staged = stage(user=["likes tea"])
    staged.call(json.dumps({"target": "user", "action": "add", "content": "prefers short answers"}))
    listing = json.loads(staged.call(json.dumps({"action": "list"}))[0])
    assert [item["text"] for item in listing["user"]["items"]] == ["likes tea", "prefers short answers"]


def test_a_corrected_fact_is_refused_and_the_model_can_continue():
    staged = stage(corrected={"user": {"likes tea"}})
    out, error = staged.call(json.dumps({"target": "user", "action": "add", "content": " likes tea "}))
    assert error and json.loads(out) == {"success": False, "code": "corrected", "error": CORRECTED}
    out, error = staged.call(json.dumps({"target": "user", "action": "add", "content": "likes coffee"}))
    assert not error and staged.proposals()["user"][2][0].text == "likes coffee"


def test_refusals_leave_the_stage_unchanged():
    staged = stage()
    out, error = staged.call(json.dumps({"target": "user", "action": "add", "content": "x" * 2000}))
    assert error and json.loads(out)["code"] == "over_limit" and staged.proposals() == {}
    assert staged.call("not json")[1] and staged.call(json.dumps({"target": "nope", "action": "add"}))[1]
    assert staged.call(json.dumps({"target": "user", "operations": "x"}))[1]


def test_no_op_calls_propose_nothing():
    staged = stage(user=["likes tea"])
    staged.call(json.dumps({"target": "user", "action": "add", "content": "likes tea"}))
    assert staged.proposals() == {}
