"""Curated memory entries: a JSON list per target, a character budget on the joined text, and a refusal for
secrets and prompt injection. The store applies the result; this module does not touch the database."""

from __future__ import annotations

import re

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


def _usage(entries: list[str], limit: int) -> str:
    return f"{len(joined(entries))}/{limit}"


def _find(entries: list[str], old_text: str) -> int | str:
    exact = [i for i, entry in enumerate(entries) if entry == old_text]
    matches = exact or [i for i, entry in enumerate(entries) if old_text in entry]
    distinct = {entries[i] for i in matches}
    if len(distinct) > 1:
        return "several entries"
    if not matches:
        return "none"
    return matches[0]


def apply_entries(entries: list[str], ops: list[dict], limit: int) -> tuple[list[str], dict]:
    """Apply `ops` to a copy. On any refusal the original list is returned with `success` false."""
    working = list(entries)
    duplicate = False
    for pos, op in enumerate(ops, start=1):
        action = op.get("action")
        content = (op.get("content") or op.get("new_text") or "").strip()
        old = (op.get("old_text") or "").strip()
        where = f"operation {pos}" if len(ops) > 1 else "the entry"
        if action == "add":
            if not content:
                return entries, _fail(entries, limit, f"{where}: content is required.")
            if reason := rejected(content):
                return entries, _fail(entries, limit, reason)
            if content in working:
                duplicate = True
            else:
                working.append(content)
            continue
        if action not in ("replace", "remove") or not old:
            return entries, _fail(entries, limit,
                                  f"{where}: use add, replace or remove, with old_text to locate an entry.")
        found = _find(working, old)
        if found == "several entries":
            return entries, _fail(entries, limit, f"Several entries match {old!r}. Quote more of the entry.")
        if found == "none":
            return entries, _fail(entries, limit, f"No entry matches {old!r}.")
        if action == "remove":
            del working[found]
            continue
        if not content:
            return entries, _fail(entries, limit, f"{where}: replace needs the full new entry.")
        if reason := rejected(content):
            return entries, _fail(entries, limit, reason)
        working[found] = content
    if len(joined(working)) > limit:
        return entries, _fail(entries, limit,
                              f"That would use {len(joined(working))}/{limit} characters. "
                              "Shorten an entry or remove one, then try again.")
    if working == entries:
        message = "That entry already exists." if duplicate else "No change."
    else:
        message = "Saved."
    return working, {"success": True, "message": message, "usage": _usage(working, limit), "entries": working}


def _fail(entries: list[str], limit: int, error: str) -> dict:
    return {"success": False, "error": error, "usage": _usage(entries, limit), "entries": list(entries)}
