"""The dream's gates that need no database (procedural learning design §5): evidence, duplicates and consumer
precedence, checked on every staged change and returned to the model with the reason; and the scheduling rules.
Pure."""

from __future__ import annotations

from dataclasses import dataclass
import re

SESSIONS = 2  # without a request or commented praise, a create or revise needs observations from this many sessions
SIMILAR = 0.6  # token overlap at which a new skill's name or description duplicates a live one
_TOKEN = re.compile(r"[a-z0-9]+")  # one character class, no nesting: linear in the input
_SHORT = 2  # tokens this short carry no meaning ("a", "of", "q3")


@dataclass(frozen=True)
class DreamRules:
    """When a profile is due for a dream, and how many attempts it may start a day (§5)."""
    min_observations: int = 8
    debounce_seconds: float = 3600.0
    interval_seconds: float = 86_400.0
    daily_attempts: int = 4


def tokens(text: str) -> frozenset[str]:
    return frozenset(token for token in _TOKEN.findall(text.lower()) if len(token) > _SHORT)


def overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity; 0 when either side is empty."""
    return len(a & b) / len(a | b) if a and b else 0.0


def duplicates(name: str, description: str, other_name: str, other_description: str) -> bool:
    """A new skill reads like an existing one: its name or its description shares most of their tokens."""
    return (overlap(tokens(name), tokens(other_name)) >= SIMILAR
            or overlap(tokens(description), tokens(other_description)) >= SIMILAR)


def evidence_refusal(action: str, cited: list) -> str | None:
    """Why the cited observations (already checked to be this attempt's input) cannot support the change, or None."""
    if action in ("create", "revise"):
        if any(item.outcome == "requested" for item in cited):
            return None
        if any(item.outcome == "feedback_up" and item.situation.strip() for item in cited):
            return None
        if len({item.session_id for item in cited}) >= SESSIONS:
            return None
        return ("Not enough evidence: cite observations from at least two different conversations, one requested "
                "observation, or one positive feedback with a comment.")
    if not cited:
        return "Cite the observation that shows this change is needed."
    return None


def precedence_refusal(skill, cited: list) -> str | None:
    """The consumer's last change of the skill stands unless a cited observation is newer (both on the content
    clock)."""
    if skill.consumer_changed_at is None:
        return None
    if cited and max(item.created_at for item in cited) > skill.consumer_changed_at:
        return None
    return (f"The user changed skill {skill.name} after the cited observations; leave it unless newer evidence shows "
            "it should change.")
