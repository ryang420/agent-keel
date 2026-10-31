"""The dream's staged proposal: every change is gated as it is made (learning design §5)."""

import json

from keel.dream_stage import StagedDream
from keel.store.base import LearnedSkill, Observation


def obs(id, session="s1", outcome="corrected", at=100.0):
    return Observation(id, session, "weekly report", "When asked", "Do it this way", outcome, (), (), "pending", None,
                       None, None, at)


def learned(id, name, description, version=1, state="active", changed_at=None):
    return LearnedSkill(id, name, description, None, "1. Old step.", version, state, "dream", changed_at, 50.0, 50.0)


OBSERVATIONS = [obs("o1", "s1"), obs("o2", "s2"), obs("o3", "s3", "requested")]
SKILLS = [learned("k1", "weekly-report", "Write the weekly report", 3),
          learned("k2", "weekly-summary", "Summarise the week", 2, changed_at=150.0)]


def stage():
    return StagedDream(OBSERVATIONS, SKILLS, {"shipped-skill"})


def call(staged, tool, **args):
    content, error = staged.call(tool, json.dumps(args))
    return json.loads(content), error


def test_a_create_with_two_sessions_is_staged_and_consumes_its_observations():
    staged = stage()
    result, error = call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export",
                         body="1. Drop empty rows.", observations=["o1", "o2"], rationale="seen twice")
    assert not error and result["success"]
    [op] = staged.proposal.ops
    assert (op["action"], op["name"], op["cited"], op["rationale"]) == ("create", "csv-cleanup", ["o1", "o2"],
                                                                        "seen twice")
    assert staged.proposal.consumed == {"o1", "o2"}


def test_thin_evidence_unknown_observations_and_duplicates_are_refused():
    staged = stage()
    thin, error = call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.",
                       observations=["o1"])
    assert error and thin["code"] == "evidence"
    unknown, _ = call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.",
                      observations=["o9"])
    assert unknown["code"] == "invalid"
    dupe, _ = call(staged, "create_skill", name="weekly-report-steps", description="Steps", body="1.",
                   observations=["o3"])
    assert dupe["code"] == "duplicate"
    shipped, _ = call(staged, "create_skill", name="shipped-skill", description="Other", body="1.",
                      observations=["o3"])
    assert shipped["code"] == "invalid"
    assert staged.proposal.ops == [] and staged.proposal.consumed == set()


def test_a_revise_pins_the_read_version_and_respects_the_consumer():
    staged = stage()
    refused, _ = call(staged, "revise_skill", skill_id="k2", body="2.", observations=["o1", "o2"])
    assert refused["code"] == "consumer_precedence"  # the consumer changed k2 at 150, after both observations
    result, error = call(staged, "revise_skill", skill_id="k1", body="2. New step.", observations=["o3"])
    assert not error
    assert staged.proposal.ops[0] == {"action": "revise", "skill_id": "k1", "expected_version": 3,
                                      "body": "2. New step.", "rationale": None, "cited": ["o3"]}
    assert staged.proposal.read == {"k1": 3}
    again, _ = call(staged, "retire_skill", skill_id="k1", observations=["o1"])
    assert again["code"] == "invalid"  # one change per skill per dream


def test_a_merge_retires_the_sources_before_rewriting_the_target():
    staged = StagedDream([obs("o1", "s1", at=200.0), obs("o2", "s2", at=200.0)], SKILLS, set())
    thin, error = call(staged, "merge_skills", target_id="k1", source_ids=["k2"], body="1. Merged.",
                       observations=["o1"])
    assert error and thin["code"] == "evidence"  # the merged text needs what a create or revise needs
    assert staged.proposal.ops == []
    result, error = call(staged, "merge_skills", target_id="k1", source_ids=["k2"], body="1. Merged.",
                         observations=["o1", "o2"])
    assert not error, result
    retire, merge = staged.proposal.ops
    assert (retire["action"], retire["skill_id"], retire["expected_version"]) == ("retire", "k2", 2)
    assert retire["rationale"] == "merged into weekly-report"
    assert (merge["action"], merge["skill_id"], merge["body"]) == ("merge", "k1", "1. Merged.")
    assert staged.proposal.read == {"k1": 3, "k2": 2}


def test_dismissed_and_cited_observations_exclude_each_other():
    staged = stage()
    result, error = call(staged, "dismiss_observation", observation_id="o1", reason="a one-off choice")
    assert not error and staged.proposal.dismissed == {"o1": "a one-off choice"}
    cited, _ = call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.",
                    observations=["o1", "o2"])
    assert cited["code"] == "invalid"
    call(staged, "retire_skill", skill_id="k1", observations=["o2"])
    dismissed, _ = call(staged, "dismiss_observation", observation_id="o2", reason="x")
    assert dismissed["code"] == "invalid"


def test_a_list_argument_is_refused_not_raised():
    staged = stage()
    viewed, error = call(staged, "skill_view", name=["weekly-report"])
    assert error and viewed["code"] == "invalid"
    dismissed, error = call(staged, "dismiss_observation", observation_id=["o1"], reason="x")
    assert error and dismissed["code"] == "invalid"
    created, error = call(staged, "create_skill", name=["csv-cleanup"], description="Clean a CSV export", body="1.",
                          observations=["o1", "o2"])
    assert error and created["code"] == "invalid"
    assert staged.proposal.ops == [] and staged.proposal.dismissed == {}


def test_an_unknown_tool_name_is_refused():
    result, error = call(stage(), "delete_everything")
    assert error and result["success"] is False and "Unknown tool" in result["error"]


def test_skill_view_reads_the_snapshot_including_suspended_skills():
    staged = StagedDream([], [learned("k3", "old-way", "Old", state="suspended")], {"shipped-skill"})
    result, error = call(staged, "skill_view", name="old-way")
    assert not error and result["state"] == "suspended" and "1. Old step." in result["content"]
    shipped, error = call(staged, "skill_view", name="shipped-skill")
    assert error and "deployment skill" in shipped["error"]


def test_a_rationale_and_a_dismissal_reason_are_redacted_before_they_are_stored():
    key = "AKIA" + "ABCDEFGHIJKLMNOP"  # built at runtime, never a literal credential
    staged = stage()
    call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1. Drop empty rows.",
         observations=["o1", "o2"], rationale=f"the user pasted {key} twice")
    call(staged, "dismiss_observation", observation_id="o3", reason=f"one-off use of {key}")
    [op] = staged.proposal.ops
    assert key not in op["rationale"] and "[REDACTED]" in op["rationale"]
    assert key not in staged.proposal.dismissed["o3"] and "[REDACTED]" in staged.proposal.dismissed["o3"]
