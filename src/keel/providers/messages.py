"""The Anthropic Messages API wire protocol (Claude models on Bedrock Mantle).

An assistant turn is replayed as the exact content blocks the model produced, so thinking signatures stay valid
and the prompt cache stays warm. Its reasoning item stores the layout: thinking blocks whole, text blocks by
length, tool_use blocks by id. The text and calls come from the message itself, so nothing is stored twice.
See docs/design/2026-10-23-bedrock-anthropic-messages.md.
"""

from __future__ import annotations

import json
import re
from typing import AsyncIterator, Iterable

import httpx

from keel.models import (
    Completed, ImagePart, Message, ModelEvent, ModelRequest, ReasoningItem, ReasoningReady, TextDelta, TextPart,
    ToolCall, ToolCallReady, Usage,
)
from keel.providers.base import ModelError, StallLimits
from keel.providers.responses import _sse_events, _until_stalled

API_VERSION = "2023-06-01"
# Lets a request ask for drop_block: thinking whose conversation prefix changed (compression, compaction, new run
# instructions) is dropped for that request instead of failing it with a 400.
BINDING_BETA = "thinking-binding-controls-2026-08-01"
CONTENT_KIND = "anthropic_content"
DEFAULT_MAX_TOKENS = 32_000
EPHEMERAL = {"type": "ephemeral"}
EMPTY_TOOL_RESULT = "(no output)"
_VERSION = re.compile(r"claude-[a-z]+-(\d{1,2})(?:-(\d{1,2})(?!\d))?")
_PROGRESS = frozenset({"message_start", "content_block_start", "content_block_delta", "content_block_stop",
                       "message_delta", "message_stop", "error"})


def adaptive_thinking(model: str) -> bool:
    """Claude 4.6 and later take adaptive thinking and effort; 4.5 and older reject both."""
    match = _VERSION.search(model)
    return bool(match) and (int(match.group(1)), int(match.group(2) or 0)) >= (4, 6)


def _tool_input(arguments: str) -> dict:
    try:
        value = json.loads(arguments or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _rebuilt(message: Message) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": message.text}] if message.text else []
    content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": _tool_input(c.arguments)}
                for c in message.tool_calls]
    return content


def _replayed(message: Message, layout: list) -> list[dict] | None:
    """The turn's original blocks, or None when the message no longer matches the layout (it was edited)."""
    text, offset, calls = message.text, 0, {c.id: c for c in message.tool_calls}
    content, used = [], []
    for block in layout:
        if not isinstance(block, dict):
            return None
        if block.get("type") == "text" and "chars" in block:
            chars = block["chars"]
            content.append({"type": "text", "text": text[offset:offset + chars]})
            offset += chars
        elif block.get("type") == "tool_use" and set(block) == {"type", "id"}:
            call = calls.get(block["id"])
            if call is None:
                return None
            content.append({"type": "tool_use", "id": call.id, "name": call.name,
                            "input": _tool_input(call.arguments)})
            used.append(call.id)
        else:
            content.append(block)
    if offset != len(text) or used != [c.id for c in message.tool_calls]:
        return None
    return content


def _assistant_content(message: Message, provider: str, model: str) -> list[dict]:
    for item in message.reasoning:
        if (item.provider, item.model, item.kind) == (provider, model, CONTENT_KIND):
            content = _replayed(message, item.payload.get("blocks") or [])
            if content is not None:
                return content
    return _rebuilt(message)


def to_messages(messages: Iterable[Message], provider: str, model: str) -> list[dict]:
    out: list[dict] = []
    for message in messages:
        if message.role == "user":
            content = []
            for part in message.parts:
                if isinstance(part, TextPart):
                    content.append({"type": "text", "text": part.text})
                elif isinstance(part, ImagePart):
                    content.append({"type": "image", "source": {"type": "base64", "media_type": part.media_type,
                                                                "data": part.data_b64}})
            if content:
                out.append({"role": "user", "content": content})
        elif message.role == "assistant":
            content = _assistant_content(message, provider, model)
            if content:
                out.append({"role": "assistant", "content": content})
        elif message.role == "tool":
            result = {"type": "tool_result", "tool_use_id": message.tool_call_id,
                      "content": message.text or EMPTY_TOOL_RESULT}
            previous = out[-1] if out else None
            # The results of one turn's calls go back together, in one user message.
            if previous and previous["role"] == "user" and previous["content"][-1]["type"] == "tool_result":
                previous["content"].append(result)
            else:
                out.append({"role": "user", "content": [result]})
    return out


def _used_tools(messages: list[dict]) -> list[str]:
    names: list[str] = []
    for message in messages:
        for block in message["content"]:
            if block.get("type") == "tool_use" and block["name"] not in names:
                names.append(block["name"])
    return names


def parse_usage(start: dict | None, end: dict | None) -> Usage:
    if not start and not end:
        return Usage()
    start, end = start or {}, end or {}
    uncached, read, write = (start.get(k) for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens"))
    total = None if uncached is None else uncached + (read or 0) + (write or 0)
    return Usage(total, end.get("output_tokens", start.get("output_tokens")), read, write)


def _looks_like_bad_signature(text: str) -> bool:
    lowered = text.lower()
    return "signature" in lowered and "thinking" in lowered


def error_from_payload(error: dict, default: str = "server_error") -> ModelError:
    kind = str(error.get("type") or "")
    message = str(error.get("message") or kind or "The model call failed.")[:500]
    if kind in ("authentication_error", "permission_error"):
        return ModelError("auth", message)
    if kind == "rate_limit_error":
        return ModelError("rate_limited", message)
    if kind in ("overloaded_error", "api_error"):
        return ModelError("server_error", message)
    if kind == "request_too_large" or "prompt is too long" in message.lower():
        return ModelError("context_overflow", message)
    if _looks_like_bad_signature(message):
        return ModelError("invalid_reasoning", message)
    if kind == "invalid_request_error":
        return ModelError("bad_request", message)
    return ModelError(default, message)


def error_from_http(status: int, body: str, retry_after: str | None) -> ModelError:
    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        parsed = {}
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        error = {"message": (parsed.get("message") if isinstance(parsed, dict) else None) or body or f"HTTP {status}"}
    if status in (401, 403):
        return ModelError("auth", str(error.get("message") or f"HTTP {status}")[:500])
    if status == 429:
        try:
            wait = float(retry_after) if retry_after else None
        except ValueError:
            wait = None
        return ModelError("rate_limited", str(error.get("message") or "Rate limited.")[:500], retry_after=wait)
    if status == 413:
        return ModelError("context_overflow", str(error.get("message") or "Request too large.")[:500])
    if status == 408:
        return ModelError("timeout", str(error.get("message") or "Request timed out.")[:500])
    if status >= 500:
        return ModelError("server_error", str(error.get("message") or f"HTTP {status}")[:500])
    return error_from_payload(error, default="bad_request")


class _Assembler:
    def __init__(self, provider: str, model: str) -> None:
        self.provider, self.model = provider, model
        self.blocks: dict[int, dict] = {}
        self.json: dict[int, str] = {}
        self.start: dict | None = None
        self.end: dict | None = None
        self.stop_reason: str | None = None
        self.done = False

    def feed(self, kind: str, data: dict) -> list[ModelEvent]:
        if kind == "message_start":
            self.start = (data.get("message") or {}).get("usage")
        elif kind == "content_block_start":
            block = dict(data.get("content_block") or {})
            if block.get("type") == "text":
                block["text"] = ""
            self.blocks[data.get("index", len(self.blocks))] = block
        elif kind == "content_block_delta":
            return self._delta(data.get("index"), data.get("delta") or {})
        elif kind == "content_block_stop":
            return self._stop(data.get("index"))
        elif kind == "message_delta":
            self.stop_reason = (data.get("delta") or {}).get("stop_reason") or self.stop_reason
            self.end = data.get("usage") or self.end
            if self.stop_reason == "refusal":
                raise ModelError("bad_request", "The model declined to answer this request.")
        elif kind == "message_stop":
            self.done = True
            events: list[ModelEvent] = []
            if self.blocks:
                events.append(ReasoningReady(ReasoningItem(self.provider, self.model, CONTENT_KIND,
                                                           {"blocks": self._layout()})))
            status = "incomplete" if self.stop_reason == "max_tokens" else "completed"
            events.append(Completed(parse_usage(self.start, self.end), status))
            return events
        elif kind == "error":
            raise error_from_payload(data.get("error") if isinstance(data.get("error"), dict) else data)
        return []

    def _delta(self, index, delta: dict) -> list[ModelEvent]:
        block = self.blocks.get(index)
        if block is None:
            return []
        kind = delta.get("type")
        if kind == "text_delta":
            text = delta.get("text") or ""
            block["text"] += text
            return [TextDelta(text)] if text else []
        if kind == "thinking_delta":
            block["thinking"] = (block.get("thinking") or "") + (delta.get("thinking") or "")
        elif kind == "signature_delta":
            block["signature"] = (block.get("signature") or "") + (delta.get("signature") or "")
        elif kind == "input_json_delta":
            self.json[index] = self.json.get(index, "") + (delta.get("partial_json") or "")
        return []

    def _stop(self, index) -> list[ModelEvent]:
        block = self.blocks.get(index)
        if block is None or block.get("type") != "tool_use":
            return []
        raw = self.json.pop(index, "")
        block["input"] = _tool_input(raw) if raw else block.get("input") or {}
        # The model's own JSON text; a call it cut short stays as sent, so the tool reports it.
        arguments = raw or json.dumps(block["input"])
        return [ToolCallReady(ToolCall(block.get("id") or "", block.get("name") or "", arguments))]

    def _layout(self) -> list[dict]:
        layout = []
        for index in sorted(self.blocks):
            block = self.blocks[index]
            if block.get("type") == "text":
                if block["text"]:
                    layout.append({"type": "text", "chars": len(block["text"])})
            elif block.get("type") == "tool_use":
                layout.append({"type": "tool_use", "id": block.get("id") or ""})
            else:
                layout.append(block)
        return layout


class MessagesTransport:
    def __init__(self, provider_name: str, url: str, client: httpx.AsyncClient, *,
                 stall: StallLimits | None = None) -> None:
        self.provider_name = provider_name
        self.url = url
        self.client = client
        self.stall = stall

    def build_body(self, request: ModelRequest) -> dict:
        messages = to_messages(request.messages, self.provider_name, request.model)
        body: dict = {"model": request.model, "max_tokens": request.max_output_tokens or DEFAULT_MAX_TOKENS,
                      "stream": True}
        system = []
        if request.system:
            # The session's system prompt is stable between compactions; per-run instructions come after it.
            system.append({"type": "text", "text": request.system, "cache_control": EPHEMERAL})
        if request.instructions:
            system.append({"type": "text", "text": request.instructions})
        if system:
            body["system"] = system
        if messages:
            last = messages[-1]["content"]
            last[-1] = {**last[-1], "cache_control": EPHEMERAL}
        body["messages"] = messages
        if request.tools:
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                              "input_schema": t["parameters"]} for t in request.tools]
        elif used := _used_tools(messages):
            # Tool blocks need tool definitions; this call may not use them.
            body["tools"] = [{"name": name, "description": "", "input_schema": {"type": "object"}} for name in used]
            body["tool_choice"] = {"type": "none"}
        if adaptive_thinking(request.model):
            body["thinking"] = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
            if request.reasoning_effort:
                # These models cannot turn thinking off; the lowest effort is the nearest thing.
                effort = "low" if request.reasoning_effort == "none" else request.reasoning_effort
                body["output_config"] = {"effort": effort}
        return body

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        body = self.build_body(request)
        assembler = _Assembler(self.provider_name, request.model)
        headers = {"accept": "text/event-stream", "anthropic-version": API_VERSION, "anthropic-beta": BINDING_BETA}
        try:
            async with self.client.stream("POST", self.url, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")
                    raise error_from_http(response.status_code, text, response.headers.get("retry-after"))
                events = _sse_events(response.aiter_lines())
                if self.stall is not None:
                    first, between = self.stall.for_request(request.reasoning_effort, len(json.dumps(body)))
                    events = _until_stalled(events, first, between, progress=_PROGRESS.__contains__)
                async for kind, data in events:
                    for event in assembler.feed(kind, data):
                        yield event
                    if assembler.done:
                        return
        except httpx.TimeoutException as exc:
            raise ModelError("timeout", f"The model call timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise ModelError("server_error", f"Could not reach the model: {exc}") from exc
        raise ModelError("server_error", "The model stream ended before the response completed.")
