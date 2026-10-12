"""Session titles: cleaning, deriving from a first message, and making a derived one unique."""

from __future__ import annotations

from typing import Iterator
import unicodedata

TITLE_LIMIT = 48  # a derived title, ellipsis included
USER_TITLE_LIMIT = 100
_MAX_SUFFIX = 9


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


def title_candidates(title: str) -> Iterator[str]:
    """The title, then "title #2" … "title #9", each within the limit (the base is shortened for the suffix)."""
    yield title
    for number in range(2, _MAX_SUFFIX + 1):
        suffix = f" #{number}"
        yield title[:TITLE_LIMIT - len(suffix)].rstrip() + suffix
