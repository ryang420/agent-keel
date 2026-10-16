"""python -m keel.admin: operator commands that work on the store directly.

import-sessions reads conversations copied from another system as JSON lines on stdin and writes one result line per
conversation to stdout (see docs/guide/operations.md). Each conversation is its own transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable

from keel.api import _ID
from keel.models import Message, TextPart, ToolCall
from keel.store import open_store
from keel.store.base import TITLE_RANK, ProfileTenantMismatch

ROLES = ("user", "assistant", "tool")


class Invalid(ValueError):
    pass


def _parse(raw: str) -> dict:
    try:
        record = json.loads(raw)
    except ValueError:
        raise Invalid("not a JSON object") from None
    if not isinstance(record, dict):
        raise Invalid("not a JSON object")
    for field in ("profile", "session_id"):
        if not isinstance(record.get(field), str) or not _ID.fullmatch(record[field]):
            raise Invalid(f"{field} is not a valid id")
    if not isinstance(record.get("tenant_id"), int) or isinstance(record["tenant_id"], bool):
        raise Invalid("tenant_id must be an integer")
    if record.get("title") is not None and not isinstance(record["title"], str):
        raise Invalid("title must be a string or null")
    if record.get("title_source") is not None and record["title_source"] not in TITLE_RANK:
        raise Invalid(f"title_source must be one of {sorted(TITLE_RANK)} or null")
    for field in ("created_at", "last_active_at"):
        if not isinstance(record.get(field), (int, float)):
            raise Invalid(f"{field} must be a number")
    if not isinstance(record.get("messages"), list):
        raise Invalid("messages must be a list")
    return record


def _message(raw: dict) -> tuple[Message, float]:
    if not isinstance(raw, dict) or raw.get("role") not in ROLES:
        raise Invalid(f"a message role must be one of {list(ROLES)}")
    if not isinstance(raw.get("text", ""), str) or not isinstance(raw.get("created_at"), (int, float)):
        raise Invalid("a message needs text and a numeric created_at")
    calls = []
    for call in raw.get("tool_calls") or []:
        if not isinstance(call, dict) or not all(isinstance(call.get(k), str) for k in ("id", "name")):
            raise Invalid("a tool call needs a string id and name")
        calls.append(ToolCall(call["id"], call["name"], str(call.get("arguments") or "{}")))
    if raw["role"] == "tool" and not isinstance(raw.get("tool_call_id"), str):
        raise Invalid("a tool message needs a tool_call_id")
    text = raw.get("text") or ""
    message = Message(raw["role"], (TextPart(text),) if text else (), tuple(calls), raw.get("tool_call_id"),
                      raw.get("tool_name"))
    return message, float(raw["created_at"])


def _result(session_id, status, *, profile_created=False, title=None, reason=None, message_ids=None) -> dict:
    return {"session_id": session_id, "status": status, "profile_created": profile_created, "title": title,
            "reason": reason, "message_ids": message_ids or {}}


async def import_lines(store, lines: Iterable[str], *, dry_run: bool) -> list[dict]:
    results = []
    for raw in lines:
        if not raw.strip():
            continue
        session_id = None
        try:
            record = _parse(raw)
            session_id = record["session_id"]
            messages = [_message(m) for m in record["messages"]]
            if dry_run:
                results.append(_result(session_id, "valid", title=record.get("title")))
                continue
            done = await store.import_session(
                profile_id=record["profile"], tenant_id=record["tenant_id"], session_id=session_id,
                title=record.get("title"), title_source=record.get("title_source"),
                created_at=float(record["created_at"]), last_active_at=float(record["last_active_at"]),
                messages=messages)
            ids = {str(m.get("source_id")): new for m, new in zip(record["messages"], done.message_ids)}
            results.append(_result(session_id, done.status, profile_created=done.profile_created,
                                   title=done.title, message_ids=ids))
        except Invalid as exc:
            results.append(_result(session_id, "invalid", reason=str(exc)))
        except ProfileTenantMismatch:
            results.append(_result(session_id, "invalid", reason="profile belongs to another tenant"))
    return results


async def _run(dry_run: bool) -> int:
    store = await open_store(os.environ["KEEL_DATABASE_URL"])
    try:
        results = await import_lines(store, sys.stdin, dry_run=dry_run)
    finally:
        await store.close()
    for result in results:
        print(json.dumps(result, ensure_ascii=False))
    return 1 if any(r["status"] == "invalid" for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m keel.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    imports = commands.add_parser("import-sessions", help="import conversations given as JSON lines on stdin")
    imports.add_argument("--dry-run", action="store_true", help="validate and report; write nothing")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
