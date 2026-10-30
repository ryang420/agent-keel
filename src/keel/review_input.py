"""What one memory review reads (cross-session memory design §4): a bounded chunk of a session's indexed user and
assistant text after the job's cursor, a little earlier text as context only, and the live memory it may edit.
Pure: the store supplies the rows. Offsets count code points of the indexed (redacted) text."""

from __future__ import annotations

from dataclasses import dataclass

from keel.compress import text_tokens
from keel.memory import joined

CHUNK_TOKENS = 2_000  # new text per attempt
OVERLAP_TOKENS = 500  # earlier text to interpret it by, never saved from alone
OVERLAP_TURNS = 2


@dataclass(frozen=True)
class SourceRow:
    id: int
    role: str  # "user" or "assistant"
    text: str
    created_at: float


@dataclass(frozen=True)
class Segment:
    message_id: int
    role: str
    text: str
    start: int
    end: int
    total: int
    overlap: bool


@dataclass(frozen=True)
class Chunk:
    segments: tuple[Segment, ...]
    end_message: int  # every message up to this id has been examined
    end_offset: int  # code points already examined of the first message after end_message
    done: bool  # nothing in the job's range is left after this chunk
    last_message: int | None  # the newest message this chunk read text from
    last_created_at: float | None


def fits(text: str, tokens: int) -> int:
    """How many leading code points of `text` fit in `tokens` by text_tokens's estimate."""
    if text_tokens(text) <= tokens:
        return len(text)
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if text_tokens(text[:mid]) <= tokens:
            low = mid
        else:
            high = mid - 1
    return low


def build_chunk(rows: list[SourceRow], *, cursor_message: int, cursor_offset: int, through: int, complete: bool,
                tokens: int = CHUNK_TOKENS) -> Chunk:
    """The next chunk after the cursor. `rows` are the messages after `cursor_message` in ascending order;
    `complete` says they are all that remain up to `through`. A message that would fit a fresh chunk waits for it;
    one longer than a whole chunk is split, and the offset says where the next chunk resumes."""
    segments: list[Segment] = []
    left = tokens
    end_message, end_offset = cursor_message, cursor_offset
    last: SourceRow | None = None
    for index, row in enumerate(rows):
        start = cursor_offset if index == 0 else 0
        rest = row.text[start:]
        if not rest:
            end_message, end_offset = row.id, 0
            continue
        size = fits(rest, left)
        if size == len(rest):
            segments.append(Segment(row.id, row.role, rest, start, len(row.text), len(row.text), False))
            left -= text_tokens(rest)
            end_message, end_offset, last = row.id, 0, row
            continue
        if size and not (segments and text_tokens(rest) <= tokens):
            segments.append(Segment(row.id, row.role, rest[:size], start, start + size, len(row.text), False))
            end_offset, last = start + size, row
        break
    else:
        if complete:
            end_message, end_offset = through, 0
        return Chunk(tuple(segments), end_message, end_offset, complete, last.id if last else None,
                     last.created_at if last else None)
    return Chunk(tuple(segments), end_message, end_offset, False, last.id if last else None,
                 last.created_at if last else None)


def overlap(rows: list[SourceRow], tokens: int = OVERLAP_TOKENS, turns: int = OVERLAP_TURNS) -> list[Segment]:
    """Up to `turns` user turns before the cursor (`rows` newest first), each message cut to its end, within
    `tokens`; returned oldest first."""
    chosen: list[Segment] = []
    left, users = tokens, 0
    for row in rows:
        if users >= turns or left <= 0:
            break
        keep = fits(row.text, left)
        if keep == 0:
            break
        start = len(row.text) - keep
        chosen.append(Segment(row.id, row.role, row.text[start:], start, len(row.text), len(row.text), True))
        left -= text_tokens(row.text[start:])
        if row.role == "user":
            users += 1
    return chosen[::-1]


def neutralise(text: str) -> str:
    """Keep conversation text from closing or opening a message tag."""
    return text.replace("</message", "</ message").replace("<message", "< message")


def render(context: list[Segment], chunk: Chunk, items: dict[str, list[dict]], limits: dict[str, int]) -> str:
    lines = ["Live memory (edit entries by id):"]
    for target in ("user", "memory"):
        mine = items.get(target, [])
        lines.append(f"[{target}] usage {len(joined([item['text'] for item in mine]))}/{limits[target]}")
        lines.extend(f"- id={item['id']} version={item['version']}: {item['text']}" for item in mine)
        if not mine:
            lines.append("(empty)")
    lines += ["", "Conversation to review, oldest first. Messages marked context-only were reviewed before: use them "
                  "to understand the rest, never as the only evidence for a new entry."]
    for segment in [*context, *chunk.segments]:
        attrs = f"id={segment.message_id} role={segment.role}"
        if segment.overlap:
            attrs += " context-only"
        elif segment.start or segment.end < segment.total:
            attrs += f" chars={segment.start}-{segment.end}/{segment.total}"
        lines += [f"<message {attrs}>", neutralise(segment.text), "</message>"]
    return "\n".join(lines)
