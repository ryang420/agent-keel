"""History search text (cross-session memory design §3): what of a message is searchable, the lexical tokenizer
shared by indexing and queries, excerpts and timestamps. Lexical matching only, not semantic."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import unicodedata

from keel.models import ImagePart, Message, TextPart
from keel.tools.base import redact

MAX_QUERY_CHARS = 256
MAX_QUERY_TOKENS = 32
MAX_TOKEN_CHARS = 64
_HAN = "㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f"
# Two disjoint character-class runs: linear on any input.
_RUNS = re.compile(f"[{_HAN}]+|[^\\W_{_HAN}]+")
_HAN_CHAR = re.compile(f"[{_HAN}]")


class QueryError(ValueError):
    """The model's query or time bound is unusable; the message goes back to it."""


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def phrase(text: str) -> str:
    return " ".join(normalize(text).split())


def _runs(text: str):
    for match in _RUNS.finditer(normalize(text)):
        run = match.group()
        yield run, _HAN_CHAR.match(run) is not None


def index_tokens(text: str) -> set[str]:
    """Words, and for Han text every character and every adjacent pair, so one- and many-character queries match."""
    tokens: set[str] = set()
    for run, han in _runs(text):
        if han:
            tokens.update(run)
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
        else:
            tokens.add(run[:MAX_TOKEN_CHARS])
    return tokens


def query_tokens(query: str) -> list[str]:
    if len(query) > MAX_QUERY_CHARS:
        raise QueryError(f"query is longer than {MAX_QUERY_CHARS} characters.")
    tokens: list[str] = []
    for run, han in _runs(query):
        parts = ([run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]) if han \
            else [run[:MAX_TOKEN_CHARS]]
        tokens.extend(part for part in parts if part not in tokens)
    if not tokens:
        raise QueryError("query has no searchable words.")
    if len(tokens) > MAX_QUERY_TOKENS:
        raise QueryError(f"query has more than {MAX_QUERY_TOKENS} distinct words; use fewer, more specific ones.")
    return tokens


def searchable_text(message: Message) -> str | None:
    """The redacted text of a user or assistant message, NULs removed (PostgreSQL text refuses them); None for anything else or a message with no text."""
    if message.role not in ("user", "assistant"):
        return None
    text = "\n".join(part.text for part in message.parts if isinstance(part, TextPart)).replace("\x00", "").strip()
    return redact(text) if text else None


def render(message: Message) -> str:
    """A stored message as context text: its text, with what is not shown labelled, never claimed as done."""
    if message.role == "tool":
        return f"[result of tool {message.tool_name or 'unknown'} omitted]"
    pieces = [part.text if isinstance(part, TextPart) else "[image omitted]" for part in message.parts
              if isinstance(part, (TextPart, ImagePart))]
    pieces += [f"[called tool {call.name}; its arguments and result are omitted]" for call in message.tool_calls]
    return redact("\n".join(pieces))


def excerpt(text: str, tokens: list[str], width: int = 300) -> tuple[str, bool]:
    """A window of `text` around the first matching token, and whether text was cut off on either side."""
    if len(text) <= width:
        return text, False
    folded = text.casefold()
    found = [at for at in (folded.find(token) for token in tokens) if at >= 0]
    centre = min(found) if found else 0
    start = max(0, min(centre - width // 3, len(text) - width))
    return text[start:start + width], True


def parse_time(value, name: str) -> float:
    if not isinstance(value, str):
        raise QueryError(f"{name} must be an ISO 8601 timestamp with a timezone.")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise QueryError(f"{name} must be an ISO 8601 timestamp with a timezone.") from None
    if moment.tzinfo is None:
        raise QueryError(f"{name} needs a timezone, for example 2026-10-01T00:00:00+00:00.")
    return moment.timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
