"""Learned skills (procedural learning design §2, §3): the limits, the validation every writer shares (foreground,
dream, consumer) and the document skill_view serves. Pure: the store applies changes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from keel.memory import rejected

CATEGORY = "learned"  # the skills index lists every learned skill under this category
MAX_SKILLS = 50  # active or suspended, per profile
MAX_NAME, MAX_DESCRIPTION, MAX_CATEGORY, MAX_BODY = 64, 200, 32, 8_000
MAX_TOPIC, MAX_DETAIL = 120, 600  # observation topic; situation and approach
# Unambiguous (a hyphen always separates the runs), so a full match is linear in the input.
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


@dataclass(frozen=True)
class IndexEntry:
    """A learned skill as the skills index shows it."""

    name: str
    description: str
    category: str = CATEGORY


def _slug(value, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit and _SLUG.fullmatch(value) is not None


def check(name, description, body, category) -> str | None:
    """Why this skill text must not be stored, or None: the §2 limits, then memory's secret and injection refusal on
    every text field."""
    if not _slug(name, MAX_NAME):
        return (f"name must be a lowercase slug (letters, digits and single hyphens) of at most {MAX_NAME} "
                "characters.")
    if (not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION
            or "\n" in description):
        return f"description must be one non-empty line of at most {MAX_DESCRIPTION} characters."
    if category is not None and not _slug(category, MAX_CATEGORY):
        return f"category must be a lowercase slug of at most {MAX_CATEGORY} characters."
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
        return f"body must be non-empty markdown of at most {MAX_BODY} characters."
    if body.lstrip().startswith("---"):
        return "body must not start with front matter; keel adds it."
    for text in (name, description, body):
        if reason := rejected(text):
            return reason
    return None


def document(skill) -> str:
    """The SKILL.md text of a learned skill: front matter rendered from the stored fields, then the body."""
    description = json.dumps(skill.description, ensure_ascii=False)
    return f"---\nname: {skill.name}\ndescription: {description}\n---\n\n{skill.body}"
