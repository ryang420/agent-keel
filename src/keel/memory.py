"""Curated memory rules: stable entries with versions, a character budget on the joined text, and a refusal for
secrets and prompt injection. The store applies the result; this module does not touch the database."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from keel.tools.base import redact

DELIMITER = "\n§\n"
_INJECTION = (
    re.compile(r"(?i)\bignore (?:all |any )?(?:previous|prior|above|earlier) (?:instructions|prompts|rules)\b"),
    re.compile(r"(?i)\b(?:disregard|forget) (?:all |any )?(?:previous|prior|above) (?:instructions|prompts)\b"),
    re.compile(r"(?i)<\|(?:im_start|im_end|system)\|>"),
    re.compile(r"(?i)\byou are now\b.{0,80}\b(?:a |an )?(?:new|different) (?:ai|assistant|model)\b"),
    re.compile(r"(?i)\b(?:exfiltrate|send) (?:the |all )?(?:secrets|credentials|system prompt)\b"),
)


def joined(entries: list[str]) -> str:
    return DELIMITER.join(entries)


def rejected(content: str) -> str | None:
    """Why `content` must not be stored, or None. Secrets use the preview redactor; injection patterns are bounded."""
    if redact(content) != content:
        return "That entry contains a secret. Remove it and try again."
    for pattern in _INJECTION:
        if pattern.search(content):
            return "That entry looks like an instruction to the model. Rephrase it as a fact."
    return None


def snapshot(memory_entries: list[str], user_entries: list[str]) -> str:
    def block(title: str, entries: list[str]) -> str:
        body = "\n".join(f"- {entry}" for entry in entries) or "(empty)"
        return f"{title}\n{body}"

    return (block("MEMORY (your personal notes)", memory_entries) + "\n\n"
            + block("USER PROFILE (who the user is)", user_entries))


@dataclass(frozen=True)
class Slot:
    """A live entry: the store's stable id and version (an entry added by the batch has a provisional id)."""
    id: str
    text: str
    version: int = 1


@dataclass(frozen=True)
class Change:
    action: str  # "add", "replace" or "remove"
    entry_id: str
    old_text: str | None
    new_text: str | None


def _find(entries: list[str], old_text: str) -> int | str:
    exact = [i for i, entry in enumerate(entries) if entry == old_text]
    matches = exact or [i for i, entry in enumerate(entries) if old_text in entry]
    distinct = {entries[i] for i in matches}
    if len(distinct) > 1:
        return "several entries"
    if not matches:
        return "none"
    return matches[0]


def _locate(working: list[Slot], op: dict, where: str) -> int | tuple[str, str]:
    """The index of the entry `op` names (by entry_id, else old_text), or (code, error)."""
    entry_id = op.get("entry_id")
    if entry_id is not None:
        if not isinstance(entry_id, str) or not entry_id:
            return "invalid", f"{where}: entry_id must be a non-empty string."
        index = next((i for i, slot in enumerate(working) if slot.id == entry_id), None)
        if index is None:
            return "not_found", f"No entry has id {entry_id!r}."
    else:
        old = (op.get("old_text") or "").strip()
        if not old:
            return "invalid", f"{where}: replace and remove need entry_id or old_text to locate an entry."
        found = _find([slot.text for slot in working], old)
        if found == "several entries":
            return "ambiguous", f"Several entries match {old!r}. Quote more of the entry or use entry_id."
        if found == "none":
            return "not_found", f"No entry matches {old!r}."
        index = found
    expected = op.get("expected_version")
    if expected is not None:
        if isinstance(expected, bool) or not isinstance(expected, int):
            return "invalid", f"{where}: expected_version must be an integer."
        slot = working[index]
        if slot.version != expected:
            return ("version_conflict",
                    f"Entry {slot.id} is at version {slot.version}, not {expected}. List the entries and try again.")
    return index


def apply_slots(slots: list[Slot], ops: list[dict], limit: int,
                new_id: Callable[[], str]) -> tuple[list[Slot], dict, list[Change]]:
    """Apply `ops` to a copy. On any refusal the original slots come back with `success` false and no changes."""
    working = list(slots)
    changes: list[Change] = []
    added: set[str] = set()
    duplicate = False
    for pos, op in enumerate(ops, start=1):
        where = f"operation {pos}" if len(ops) > 1 else "the entry"
        if not isinstance(op, dict):
            return slots, _fail(slots, limit, "invalid", f"{where} must be an object."), []
        action = op.get("action")
        content = (op.get("content") or op.get("new_text") or "").strip()
        if action == "add":
            if not content:
                return slots, _fail(slots, limit, "invalid", f"{where}: content is required."), []
            if reason := rejected(content):
                return slots, _fail(slots, limit, "rejected", reason), []
            if any(slot.text == content for slot in working):
                duplicate = True
                continue
            slot = Slot(new_id(), content)
            added.add(slot.id)
            working.append(slot)
            changes.append(Change("add", slot.id, None, content))
            continue
        if action not in ("replace", "remove"):
            return slots, _fail(slots, limit, "invalid", f"{where}: action must be add, replace or remove."), []
        found = _locate(working, op, where)
        if isinstance(found, tuple):
            return slots, _fail(slots, limit, *found), []
        slot = working[found]
        if action == "remove":
            del working[found]
            changes.append(Change("remove", slot.id, slot.text, None))
            continue
        if not content:
            return slots, _fail(slots, limit, "invalid", f"{where}: replace needs the full new entry."), []
        if reason := rejected(content):
            return slots, _fail(slots, limit, "rejected", reason), []
        if content != slot.text:
            version = slot.version if slot.id in added else slot.version + 1
            working[found] = Slot(slot.id, content, version)
            changes.append(Change("replace", slot.id, slot.text, content))
    size = len(joined([slot.text for slot in working]))
    if size > limit:
        return slots, _fail(slots, limit, "over_limit",
                            f"That would use {size}/{limit} characters. "
                            "Shorten an entry or remove one, then try again."), []
    if changes:
        message = "Saved."
    else:
        message = "That entry already exists." if duplicate else "No change."
    return working, {"success": True, "message": message, **_view(working, limit)}, changes


def _view(slots: list[Slot], limit: int) -> dict:
    texts = [slot.text for slot in slots]
    return {"usage": f"{len(joined(texts))}/{limit}", "entries": texts,
            "items": [{"id": slot.id, "text": slot.text, "version": slot.version} for slot in slots]}


def _fail(slots: list[Slot], limit: int, code: str, error: str) -> dict:
    return {"success": False, "code": code, "error": error, **_view(slots, limit)}
