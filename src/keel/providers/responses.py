"""The OpenAI Responses API wire protocol (Bedrock Mantle now, OpenAI later).

Adapted from Hermes (Nous Research hermes-agent, MIT, v2026.9.24): request shape from
agent/transports/codex.py, reasoning replay from agent/codex_responses_adapter.py, and the rule of
assembling output from `response.output_item.done` (never `response.completed.output`) from
agent/codex_runtime.py. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import AsyncIterator, Iterable

import httpx

from keel.models import (
    Completed, ImagePart, Message, ModelEvent, ModelRequest, ReasoningItem, ReasoningReady, TextDelta,
    TextPart, ToolCall, ToolCallReady, Usage,
)
from keel.providers.base import ModelError


def cache_key(scope: str, system: str, tools: tuple[dict, ...]) -> str:
    """Stable for a session's system prompt and tools; per-run instructions are deliberately left out."""
    tools_text = json.dumps(sorted(tools, key=lambda t: t.get("name", "")), sort_keys=True)
    digest = hashlib.sha256(f"{scope}\0{system}\0{tools_text}".encode()).hexdigest()
    return "pck_" + digest[:24]


def to_input_items(messages: Iterable[Message], provider: str, model: str) -> list[dict]:
    items: list[dict] = []
    for message in messages:
        if message.role == "user":
            content = []
            for part in message.parts:
                if isinstance(part, TextPart):
                    content.append({"type": "input_text", "text": part.text})
                elif isinstance(part, ImagePart):
                    content.append({"type": "input_image",
                                    "image_url": f"data:{part.media_type};base64,{part.data_b64}"})
            items.append({"role": "user", "content": content})
        elif message.role == "assistant":
            for item in message.reasoning:
                if item.provider == provider and item.model == model and item.payload.get("encrypted_content"):
                    items.append({"type": "reasoning", "encrypted_content": item.payload["encrypted_content"],
                                  "summary": item.payload.get("summary", [])})
            if message.text:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": message.text}]})
            for call in message.tool_calls:
                items.append({"type": "function_call", "call_id": call.id, "name": call.name,
                              "arguments": call.arguments})
        elif message.role == "tool":
            items.append({"type": "function_call_output", "call_id": message.tool_call_id, "output": message.text})
    return items


def parse_usage(data: dict | None) -> Usage:
    if not data:
        return Usage()
    details = data.get("input_tokens_details")
    read = write = None
    if isinstance(details, dict):
        read = details.get("cached_tokens")
        write = details.get("cache_write_tokens", details.get("cache_creation_tokens"))  # None: not reported
    return Usage(data.get("input_tokens"), data.get("output_tokens"), read, write)


def _looks_like_context_overflow(text: str) -> bool:
    lowered = text.lower()
    return "context" in lowered and any(word in lowered for word in ("length", "window", "too long", "maximum"))


def _looks_like_bad_reasoning(text: str) -> bool:
    lowered = text.lower()
    return "invalid_encrypted_content" in lowered or ("encrypted" in lowered and "reasoning" in lowered)


def error_from_payload(error: dict, default: str = "server_error") -> ModelError:
    code = str(error.get("code") or "")
    message = str(error.get("message") or code or "The model call failed.")
    text = f"{code} {message}"
    if _looks_like_bad_reasoning(text):
        return ModelError("invalid_reasoning", message)
    if "context_length" in code or _looks_like_context_overflow(message):
        return ModelError("context_overflow", message)
    if "rate_limit" in code:
        return ModelError("rate_limited", message)
    return ModelError(default, message)


def error_from_http(status: int, body: str, retry_after: str | None) -> ModelError:
    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        parsed = {}
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        error = {"message": parsed.get("message") if isinstance(parsed, dict) else None}
    message = str(error.get("message") or body or f"HTTP {status}")[:500]
    if status in (401, 403):
        return ModelError("auth", message)
    if status == 429:
        try:
            wait = float(retry_after) if retry_after else None
        except ValueError:
            wait = None
        return ModelError("rate_limited", message, retry_after=wait)
    if status == 413:
        return ModelError("context_overflow", message)
    if status == 408:
        return ModelError("timeout", message)
    if status >= 500:
        return ModelError("server_error", message)
    return error_from_payload({**error, "message": message}, default="bad_request")


def _decode_event(event_type: str, data_lines: list[str]) -> tuple[str, dict] | None:
    if not data_lines:
        return None
    try:
        data = json.loads("\n".join(data_lines))
    except ValueError:
        return None
    return (data.get("type") or event_type, data) if isinstance(data, dict) else None


async def _sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, dict]]:
    event_type, data_lines = "", []
    async for line in lines:
        if line == "":
            decoded = _decode_event(event_type, data_lines)
            if decoded is not None:
                yield decoded
            event_type, data_lines = "", []
        elif line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    # A stream cut right after its last event, before the blank line, still delivers that event.
    decoded = _decode_event(event_type, data_lines)
    if decoded is not None:
        yield decoded


async def _until_stalled(events: AsyncIterator[tuple[str, dict]],
                         stall_seconds: float | None) -> AsyncIterator[tuple[str, dict]]:
    """Pass events through, failing as "timeout" once no response event has arrived for `stall_seconds`.

    A read timeout alone cannot see a stall: Mantle keeps a stalled stream open with keep-alive traffic, which
    resets it. So only `response.*` and `error` events count as progress; comments and other events do not.
    """
    if stall_seconds is None:
        async for event in events:
            yield event
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + stall_seconds
    while True:
        try:
            async with asyncio.timeout_at(deadline):
                kind, data = await anext(events)
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise ModelError("timeout", f"The model sent no response event for {stall_seconds:g} s.") from None
        if kind.startswith("response.") or kind == "error":
            deadline = loop.time() + stall_seconds
        yield kind, data


class _Assembler:
    def __init__(self, provider: str, model: str) -> None:
        self.provider, self.model = provider, model
        self.pending: dict[str, dict] = {}
        self.done = False

    def feed(self, kind: str, data: dict) -> list[ModelEvent]:
        if kind == "response.output_text.delta":
            delta = data.get("delta") or ""
            return [TextDelta(delta)] if delta else []
        if kind == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") == "function_call" and item.get("id"):
                self.pending[item["id"]] = {"call_id": item.get("call_id") or item["id"],
                                            "name": item.get("name") or "", "arguments": item.get("arguments") or ""}
            return []
        if kind == "response.function_call_arguments.delta":
            pending = self.pending.get(data.get("item_id") or "")
            if pending is not None:
                pending["arguments"] += data.get("delta") or ""
            return []
        if kind == "response.output_item.done":
            return self._item_done(data.get("item") or {})
        if kind in ("response.completed", "response.incomplete"):
            response = data.get("response") or {}
            events: list[ModelEvent] = [
                ToolCallReady(ToolCall(p["call_id"], p["name"], p["arguments"] or "{}"))
                for p in self.pending.values()
            ]
            self.pending.clear()
            self.done = True
            status = "completed" if kind == "response.completed" else "incomplete"
            events.append(Completed(parse_usage(response.get("usage")), status))
            return events
        if kind == "response.failed":
            raise error_from_payload((data.get("response") or {}).get("error") or {})
        if kind == "error":
            raise error_from_payload(data.get("error") if isinstance(data.get("error"), dict) else data)
        return []

    def _item_done(self, item: dict) -> list[ModelEvent]:
        kind = item.get("type")
        if kind == "function_call":
            self.pending.pop(item.get("id") or "", None)
            return [ToolCallReady(ToolCall(item.get("call_id") or item.get("id") or "", item.get("name") or "",
                                           item.get("arguments") or "{}"))]
        if kind == "reasoning" and item.get("encrypted_content"):
            payload = {"encrypted_content": item["encrypted_content"], "summary": item.get("summary") or []}
            return [ReasoningReady(ReasoningItem(self.provider, self.model, "reasoning", payload))]
        return []


class ResponsesTransport:
    def __init__(self, provider_name: str, url: str, client: httpx.AsyncClient, *, compat: bool = False,
                 stall_seconds: float | None = None) -> None:
        # compat: omit include, prompt_cache_key and reasoning.summary, which strict servers may reject.
        self.compat = compat
        self.stall_seconds = stall_seconds
        self.provider_name = provider_name
        self.url = url
        self.client = client

    def build_body(self, request: ModelRequest) -> dict:
        instructions = request.system if not request.instructions else f"{request.system}\n\n{request.instructions}"
        body: dict = {
            "model": request.model,
            "instructions": instructions,
            "input": to_input_items(request.messages, self.provider_name, request.model),
            "stream": True,
            "store": False,
        }
        if not self.compat:
            body["prompt_cache_key"] = cache_key(request.cache_scope, request.system, request.tools)
            # Always: reasoning from an earlier turn can only be replayed if this one returns it (spec §5).
            body["include"] = ["reasoning.encrypted_content"]
        if request.max_output_tokens is not None:
            body["max_output_tokens"] = request.max_output_tokens
        if request.tools:
            body["tools"] = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
                              "parameters": t["parameters"]} for t in request.tools]
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True
        if request.reasoning_effort == "none":
            body["reasoning"] = {"effort": "none"}
        elif request.reasoning_effort:
            body["reasoning"] = {"effort": request.reasoning_effort}
            if not self.compat:
                body["reasoning"]["summary"] = "auto"
        return body

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        body = self.build_body(request)
        assembler = _Assembler(self.provider_name, request.model)
        try:
            async with self.client.stream("POST", self.url, json=body,
                                          headers={"accept": "text/event-stream"}) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")
                    raise error_from_http(response.status_code, text, response.headers.get("retry-after"))
                async for kind, data in _until_stalled(_sse_events(response.aiter_lines()), self.stall_seconds):
                    for event in assembler.feed(kind, data):
                        yield event
                    if assembler.done:
                        return
        except httpx.TimeoutException as exc:
            raise ModelError("timeout", f"The model call timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise ModelError("server_error", f"Could not reach the model: {exc}") from exc
        raise ModelError("server_error", "The model stream ended before the response completed.")
