"""Session titles: cleaning, deriving from a first message, parsing a model title, and keeping one unique."""

from __future__ import annotations

import json
import re
from typing import Iterator
import unicodedata

TITLE_LIMIT = 48  # a derived title, ellipsis included
USER_TITLE_LIMIT = 100
MODEL_TITLE_LIMIT = 100
TITLE_INPUT_CHARS = 1000
_TITLE_OBJECT = re.compile(r"\{[^{}]{0,500}\}")
_REJECTED = frozenset({
    "fix login button on mobile",
    "postgres connection pool exhaustion",
    "code changes",
    "friendly greeting",
})


def sanitize_title(title: str, limit: int = USER_TITLE_LIMIT) -> str:
    """Drop control, format (zero-width, bidi) and surrogate characters, collapse whitespace, cut to `limit`."""
    cleaned = "".join(ch for ch in title if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    return " ".join(cleaned.split())[:limit].strip()


def derive_title(text: str) -> str | None:
    """The first line that has anything visible, cleaned and cut at a word boundary to 48 characters."""
    line = next((clean for clean in (sanitize_title(raw, limit=10_000) for raw in text.splitlines()) if clean), "")
    if not line:
        return None
    if len(line) <= TITLE_LIMIT:
        return line
    room = TITLE_LIMIT - 1  # for the ellipsis
    cut = line[:room] if line[room] == " " else line[:room].rsplit(" ", 1)[0]
    return (cut.rstrip() or line[:room]) + "…"


def title_candidates(title: str, *, limit: int = TITLE_LIMIT) -> Iterator[str]:
    """The title, then "title #2", "title #3", …, each cut to `limit`. Unbounded: a session is never left untitled
    because every numbered suffix was taken."""
    yield title[:limit].strip() or title[:1]
    number = 2
    while True:
        suffix = f" #{number}"
        base = title[:max(1, limit - len(suffix))].rstrip()
        yield base + suffix
        number += 1


def parse_model_title(text: str) -> str | None:
    """A short specific title from a JSON object, or None when the reply is unusable."""
    found = _TITLE_OBJECT.search(text)
    if found is None:
        return None
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return None
    raw = data.get("title") if isinstance(data, dict) else None
    if not isinstance(raw, str):
        return None
    title = sanitize_title(raw, limit=MODEL_TITLE_LIMIT)
    if not title or len(title.split()) > 12 or title.casefold() in _REJECTED:
        return None
    return title
