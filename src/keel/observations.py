"""Lesson observations (procedural learning design §4, §6): their limits and the checks every writer shares. Pure."""

from __future__ import annotations

from keel.learned import MAX_DETAIL, MAX_TOPIC
from keel.memory import rejected

OUTCOMES = ("worked", "failed", "corrected", "requested", "feedback_up", "feedback_down")
REVIEW_OUTCOMES = ("worked", "failed", "corrected", "requested")  # feedback outcomes come only from feedback
MAX_PER_JOB = 5
EXPIRY_SECONDS = 30 * 86_400  # a pending observation not consumed by then expires
STATS_SECONDS = 30 * 86_400  # the window for per-version outcome statistics
SUSPEND_DOWN = 3  # down ratings that suspend a skill version when they outnumber the up ratings
MAX_COMMENT = 2_000
FEEDBACK_TOPIC = "Run feedback"


def check_observation(topic, situation, approach) -> str | None:
    """Why this observation text must not be stored, or None. The topic is required; situation and approach may be
    empty here (a review requires an approach itself)."""
    if not isinstance(topic, str) or not topic.strip() or len(topic) > MAX_TOPIC:
        return f"topic must be non-empty text of at most {MAX_TOPIC} characters."
    for name, value in (("situation", situation), ("approach", approach)):
        if not isinstance(value, str) or len(value) > MAX_DETAIL:
            return f"{name} must be text of at most {MAX_DETAIL} characters."
    for text in (topic, situation, approach):
        if reason := rejected(text):
            return reason
    return None


def check_comment(comment) -> str | None:
    """Why a feedback comment must not be stored, or None. A comment is user-supplied data, never instructions."""
    if comment is None:
        return None
    if not isinstance(comment, str) or len(comment) > MAX_COMMENT:
        return f"comment must be text of at most {MAX_COMMENT} characters."
    return rejected(comment)
