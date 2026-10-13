"""Context compression, the pure part (M3 design §5): estimate a request's size, choose the head, tail and
placeholder boundaries, and assemble what is sent. Nothing here changes what is stored; keel.compactor decides when
to compact and stores the result."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from typing import Sequence

from keel import prompts
from keel.models import ImagePart, Message, TextPart
from keel.store.base import Session, StoredMessage
from keel.tools.base import cap_json_result, redact, truncate_middle

MESSAGE_OVERHEAD = 4
IMAGE_TOKENS = 1_600
HEAD_MESSAGES = 3
HEAD_MESSAGE_TOKENS = 4_000
TAIL_SHARE, TAIL_MIN_TOKENS, TAIL_MAX_TOKENS = 0.025, 10_000, 25_000
PLACEHOLDER_OVER_CHARS = 200
IMAGE_OMITTED = "[image omitted from context]"
SUMMARY_END = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
SUMMARY_INPUT_TOKENS = 40_000
FILES_LIMIT = 50
FILES_HEADING = "## Files referenced"
_CONTENT_MAX, _CONTENT_HEAD, _CONTENT_TAIL = 6_000, 4_000, 1_500
_ARGS_MAX, _ARGS_HEAD = 1_500, 1_200
_PATH_ARGUMENT_TOOLS = ("read_file", "write_file", "patch")
_PATH_TEXT_ARGUMENTS = {"terminal": "command", "execute_code": "code"}
# A fixed prefix and one bounded character class keep the search linear on model-controlled text. The look-behind
# keeps URLs (`https://host/path`) and relative paths (`a/b`) out.
_ABSOLUTE_PATH = re.compile(r"(?<![\w/.:~-])/[\w.@+~-][\w./@+~-]{0,1023}")


def text_tokens(text: str) -> int:
    """A quarter of a token per ASCII character and one per other character: CJK text is close to one token per
    character, and dividing all text by 4 would undercount it several times."""
    ascii_chars = len(text.encode("ascii", "ignore"))
    return (ascii_chars + 3) // 4 + len(text) - ascii_chars


def message_tokens(message: Message) -> int:
    total = MESSAGE_OVERHEAD
    for part in message.parts:
        total += text_tokens(part.text) if isinstance(part, TextPart) else IMAGE_TOKENS
    for call in message.tool_calls:
        total += text_tokens(call.name) + text_tokens(call.arguments)
    for item in message.reasoning:
        total += text_tokens(json.dumps(item.payload))
    return total


def base_tokens(system: str, tools: Sequence[dict]) -> int:
    """The part of every request that is not messages: the system prompt and the tool definitions."""
    return text_tokens(system) + sum(text_tokens(json.dumps(tool)) for tool in tools)


def tail_budget(window: int) -> int:
    return max(TAIL_MIN_TOKENS, min(TAIL_MAX_TOKENS, int(window * TAIL_SHARE)))


@dataclass(frozen=True)
class Boundaries:
    """A session's compaction state: the summary with the last head message and the first tail message it sits
    between, and the placeholder boundary (messages below it, other than the head, get the placeholder pass)."""

    summary: str | None = None
    head_id: int | None = None
    tail_id: int | None = None
    pruned_id: int | None = None

    @classmethod
    def of(cls, session: Session | None) -> "Boundaries":
        if session is None:
            return cls()
        return cls(session.summary, session.summary_head_id, session.summary_tail_id, session.pruned_id)

    @property
    def active(self) -> bool:
        return self.summary is not None or self.pruned_id is not None


@dataclass(frozen=True)
class Item:
    id: int | None  # the stored message's id; None for the summary message, which is not stored
    message: Message


def head_end(stored: Sequence[StoredMessage]) -> int:
    """How many leading messages form the head: the first three, extended until every tool call among them has
    its result."""
    calls: set[str] = set()
    answered: set[str | None] = set()
    for index, item in enumerate(stored):
        if index >= HEAD_MESSAGES and calls <= answered:
            return index
        calls.update(call.id for call in item.message.tool_calls)
        if item.message.role == "tool":
            answered.add(item.message.tool_call_id)
    return len(stored)


def tail_start(candidates: Sequence[StoredMessage], budget: int) -> int:
    """Where the tail starts in `candidates`: the earliest user or assistant message from which everything fits
    `budget`, but never later than the newest user or assistant message, so the tail holds at least the newest
    assistant message with all its results. Starting at a user or assistant message keeps every tool call with its
    results. 0 when the tail is everything."""
    start, used = None, 0
    for index in range(len(candidates) - 1, -1, -1):
        used += message_tokens(candidates[index].message)
        if candidates[index].message.role == "tool":
            continue
        if start is not None and used > budget:
            break
        start = index
    return start or 0


def placeholder(message: Message) -> Message:
    """The placeholder pass for one message: a tool result over 200 characters becomes one line naming the tool and
    its size, every image becomes a note, and reasoning is dropped."""
    if message.role == "tool" and len(message.text) > PLACEHOLDER_OVER_CHARS:
        note = f"[{message.tool_name or 'tool'} result omitted from context: {len(message.text):,} characters]"
        return replace(message, parts=(TextPart(note),), reasoning=())
    parts = tuple(TextPart(IMAGE_OMITTED) if isinstance(part, ImagePart) else part for part in message.parts)
    return replace(message, parts=parts, reasoning=())


def _cut_text(text: str, max_tokens: int) -> str:
    """Cut head-and-tail with the tool truncation marker so the text is about `max_tokens`."""
    tokens = text_tokens(text)
    if tokens <= max_tokens:
        return text
    cut = truncate_middle(text, max(1, len(text) * max_tokens // tokens))
    return cut if len(cut) < len(text) else text


def cut_message(message: Message, max_tokens: int) -> Message:
    """Cut the message's text parts, each in proportion to its size, so their text fits about `max_tokens`."""
    total = sum(text_tokens(part.text) for part in message.parts if isinstance(part, TextPart))
    if total <= max_tokens:
        return message
    parts = tuple(TextPart(_cut_text(part.text, max_tokens * text_tokens(part.text) // total))
                  if isinstance(part, TextPart) and part.text else part for part in message.parts)
    return replace(message, parts=parts)


def cut_head(message: Message) -> Message:
    """A head message once there is a summary: text cut to about HEAD_MESSAGE_TOKENS, each call's arguments brought
    under the same size and kept valid JSON, images replaced by a note, and reasoning dropped."""
    cut = cut_message(message, HEAD_MESSAGE_TOKENS)
    parts = tuple(TextPart(IMAGE_OMITTED) if isinstance(part, ImagePart) else part for part in cut.parts)
    calls = tuple(replace(call, arguments=cap_json_result(call.arguments, HEAD_MESSAGE_TOKENS * 4))
                  for call in cut.tool_calls)
    return replace(cut, parts=parts, tool_calls=calls, reasoning=())


def summary_message(summary: str) -> Message:
    return Message("user", (TextPart(f"{prompts.text('summary_prefix')}\n\n{summary}\n\n{SUMMARY_END}"),))


def _fit_newest_round(items: list[Item], budget: int) -> list[Item]:
    """Cut the tool results of the newest round (the newest user or assistant message and what follows) so the
    round fits `budget`."""
    start = max((index for index, item in enumerate(items) if item.message.role != "tool"), default=0)
    results = [index for index in range(start, len(items)) if items[index].message.role == "tool"]
    used = sum(message_tokens(item.message) for item in items[start:])
    if used <= budget or not results:
        return items
    fixed = used - sum(message_tokens(items[index].message) for index in results)
    each = max(1_000, (budget - fixed) // len(results))
    fitted = list(items)
    for index in results:
        fitted[index] = Item(items[index].id, cut_message(items[index].message, each))
    return fitted


def assemble(stored: Sequence[StoredMessage], bounds: Boundaries, budget: int) -> list[Item]:
    """What is sent for a session's stored messages. With no boundary, every message as stored. Otherwise the head
    (each message through `cut_head` once there is a summary), the summary as a user message, the rest with
    the placeholder pass below `pruned_id`, and the newest round cut to the tail budget."""
    if not bounds.active:
        return [Item(item.id, item.message) for item in stored]
    if bounds.summary is not None:
        head = [item for item in stored if item.id <= bounds.head_id]
        rest = [item for item in stored if item.id >= bounds.tail_id]
        items = [Item(item.id, cut_head(item.message)) for item in head]
        items.append(Item(None, summary_message(bounds.summary)))
    else:
        end = head_end(stored)
        rest = list(stored[end:])
        items = [Item(item.id, item.message) for item in stored[:end]]
    for item in rest:
        pruned = bounds.pruned_id is not None and item.id < bounds.pruned_id
        items.append(Item(item.id, placeholder(item.message) if pruned else item.message))
    return _fit_newest_round(items, budget)


def estimate(items: Sequence[Item], base: int, anchor_tokens: int | None = None,
             anchor_id: int | None = None) -> int:
    """The request's size in tokens: the provider's count for the call whose newest message was `anchor_id`, plus
    the heuristic for every message after it; the heuristic for the whole request when there is no usable anchor."""
    ids = [item.id for item in items]
    if anchor_tokens is not None and anchor_id is not None and anchor_id in ids:
        return anchor_tokens + sum(message_tokens(item.message) for item in items[ids.index(anchor_id) + 1:])
    return base + sum(message_tokens(item.message) for item in items)


def serialize(messages: Sequence[Message]) -> str:
    """Messages as labelled, redacted text for the summary call (Hermes's format), after the placeholder pass."""
    records = []
    for message in messages:
        message = placeholder(message)
        content = redact(message.text)
        if len(content) > _CONTENT_MAX:
            content = f"{content[:_CONTENT_HEAD]}\n...[truncated]...\n{content[-_CONTENT_TAIL:]}"
        if message.role == "tool":
            records.append(f"[TOOL RESULT {message.tool_call_id}]: {content}")
            continue
        if message.tool_calls:
            lines = []
            for call in message.tool_calls:
                arguments = redact(call.arguments)
                if len(arguments) > _ARGS_MAX:
                    arguments = arguments[:_ARGS_HEAD] + "..."
                lines.append(f"  {call.name}({arguments})")
            content += "\n[Tool calls:\n" + "\n".join(lines) + "\n]"
        records.append(f"[{message.role.upper()}]: {content}")
    return "\n\n".join(records)


def summary_turns(previous: str | None, middle: Sequence[Message]) -> str:
    """The middle serialized for the summary call, cut head-and-tail so that it and the previous summary stay
    within SUMMARY_INPUT_TOKENS."""
    room = SUMMARY_INPUT_TOKENS - (text_tokens(previous) if previous else 0)
    return _cut_text(serialize(middle), max(1_000, room))


def _paths_in(text: str) -> list[str]:
    return [match.rstrip(".,:;") for match in _ABSOLUTE_PATH.findall(text)]


def collect_files(messages: Sequence[Message], previous: Sequence[str] = ()) -> list[str]:
    """File paths named in `messages`, after the previous list: the `path` argument of the file tools, and absolute
    paths in user messages and in terminal and execute_code arguments. Deduplicated in first-seen order; the newest
    FILES_LIMIT are kept."""
    found = list(previous)
    for message in messages:
        if message.role == "user":
            found += _paths_in(message.text)
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.arguments or "{}")
            except ValueError:
                continue
            if not isinstance(arguments, dict):
                continue
            if call.name in _PATH_ARGUMENT_TOOLS and isinstance(arguments.get("path"), str):
                found.append(arguments["path"])
            elif call.name in _PATH_TEXT_ARGUMENTS and isinstance(arguments.get(_PATH_TEXT_ARGUMENTS[call.name]), str):
                found += _paths_in(arguments[_PATH_TEXT_ARGUMENTS[call.name]])
    return list(dict.fromkeys(path for path in found if len(path) > 1))[-FILES_LIMIT:]


def split_files(summary: str | None) -> tuple[str | None, list[str]]:
    """A stored summary's body and its "Files referenced" list (keel appends the list last)."""
    if summary is None:
        return None, []
    body, heading, files = summary.rpartition(f"\n\n{FILES_HEADING}\n")
    if not heading:
        return summary, []
    return body, [line[2:] for line in files.splitlines() if line.startswith("- ")]


def join_files(body: str, files: Sequence[str]) -> str:
    if not files:
        return body
    return f"{body}\n\n{FILES_HEADING}\n" + "\n".join(f"- {path}" for path in files)


def summary_prompt(previous: str | None, turns: str, *, today: str) -> tuple[str, str]:
    """(system, user) for the summary call: Hermes's checkpoint prompt, in its first or its update form."""
    target = max(2_000, min(text_tokens(turns) // 5, 10_000))
    sections = prompts.text("summary_sections").format(target=target, today=today)
    if previous:
        user = prompts.text("summary_update").format(previous=previous, turns=turns, sections=sections)
    else:
        user = prompts.text("summary_first").format(turns=turns, sections=sections)
    return prompts.text("summary_system"), user
