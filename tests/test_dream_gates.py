"""The dream's pure commit gates (learning design §5)."""

import time

from keel.dream_gates import DreamRules, duplicates, evidence_refusal, precedence_refusal, tokens
from keel.store.base import LearnedSkill, Observation


def obs(id="o1", session="s1", outcome="corrected", situation="When asked", at=100.0):
    return Observation(id, session, "weekly report", situation, "Do it this way", outcome, (), (), "pending", None,
                       None, None, at)


def skill(changed_at=None):
    return LearnedSkill("k1", "weekly-report", "Write the weekly report", None, "1.", 1, "active", "dream",
                        changed_at, 50.0, 50.0)


def test_rules_default_to_the_spec_values():
    assert DreamRules() == DreamRules(8, 3600.0, 86_400.0, 4)


def test_tokens_ignore_case_punctuation_and_short_words():
    assert tokens("Weekly-Report of Q3, to ALL") == {"weekly", "report", "all"}


def test_tokens_are_linear_on_long_input():
    started = time.perf_counter()
    tokens("a-" * 200_000 + "b" * 200_000)
    assert time.perf_counter() - started < 1.0


def test_a_name_or_description_sharing_most_tokens_duplicates():
    assert duplicates("weekly-report-steps", "Anything else", "weekly-report", "Write it")
    assert duplicates("csv-cleanup", "Write the weekly report", "weekly-report", "Write the weekly report")
    assert not duplicates("csv-cleanup", "Clean a CSV export", "weekly-report", "Write the weekly report")


def test_create_and_revise_need_two_sessions_a_request_or_commented_praise():
    assert evidence_refusal("create", [obs()]) is not None
    assert evidence_refusal("create", [obs("o1", "s1"), obs("o2", "s1")]) is not None
    assert evidence_refusal("revise", [obs("o1", "s1"), obs("o2", "s2")]) is None
    assert evidence_refusal("create", [obs(outcome="requested")]) is None
    assert evidence_refusal("create", [obs(outcome="feedback_up")]) is None
    assert evidence_refusal("create", [obs(outcome="feedback_up", situation=" ")]) is not None


def test_retire_needs_one_cited_observation_and_merge_needs_what_new_text_needs():
    assert evidence_refusal("retire", []) is not None
    assert evidence_refusal("retire", [obs()]) is None
    assert evidence_refusal("merge", [obs()]) is not None  # a merge writes the target's text anew
    assert evidence_refusal("merge", [obs("o1", "s1"), obs("o2", "s2")]) is None
    assert evidence_refusal("merge", [obs(outcome="requested")]) is None


def test_a_consumer_change_stands_until_newer_evidence():
    assert precedence_refusal(skill(), [obs(at=10.0)]) is None
    assert precedence_refusal(skill(changed_at=200.0), [obs(at=100.0)]) is not None
    assert precedence_refusal(skill(changed_at=200.0), [obs(at=100.0), obs("o2", at=300.0)]) is None
    assert precedence_refusal(skill(changed_at=200.0), []) is not None
