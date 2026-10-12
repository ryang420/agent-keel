"""The provider-neutral conversation format shared by the loop, the store and every provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    """Image bytes as base64 in `data_b64`. A stored message keeps only `blob_id`; the runner loads the bytes."""

    media_type: str
    data_b64: str = ""
    blob_id: str | None = None


Part = Union[TextPart, ImagePart]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text; "{}" when the model sent none


@dataclass(frozen=True)
class ReasoningItem:
    """Opaque to everything but the provider and model that produced it."""

    provider: str
    model: str
    kind: str
    payload: dict


Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class Message:
    """An assistant message's content is ordered reasoning items first, then text parts, then tool calls.
    That is the order every provider replays it in (the Responses API's and Anthropic's canonical order
    within one message). Thinking interleaved with tool use arrives as separate messages after each tool
    result. A tool message carries its result in `parts` with `tool_call_id` and `tool_name`."""

    role: Role
    parts: tuple[Part, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None
    reasoning: tuple[ReasoningItem, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(part.text for part in self.parts if isinstance(part, TextPart))


USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


@dataclass(frozen=True)
class Usage:
    """Token counts. input_tokens is the whole prompt, cached parts included; unknown is None."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    def __add__(self, other: "Usage") -> "Usage":
        def add(a: int | None, b: int | None) -> int | None:
            return None if a is None and b is None else (a or 0) + (b or 0)

        return Usage(*(add(getattr(self, name), getattr(other, name)) for name in USAGE_FIELDS))

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in USAGE_FIELDS}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Usage":
        return cls(**{name: data.get(name) for name in USAGE_FIELDS}) if data else cls()


@dataclass(frozen=True)
class ModelRequest:
    provider: str
    model: str
    system: str
    messages: tuple[Message, ...]
    instructions: str | None = None
    tools: tuple[dict, ...] = ()
    reasoning_effort: str | None = None
    cache_scope: str = ""
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallReady:
    call: ToolCall


@dataclass(frozen=True)
class ReasoningReady:
    item: ReasoningItem


@dataclass(frozen=True)
class Completed:
    usage: Usage
    status: str = "completed"


ModelEvent = Union[TextDelta, ToolCallReady, ReasoningReady, Completed]


def part_to_dict(part: Part) -> dict:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    if part.blob_id:
        return {"type": "image", "media_type": part.media_type, "blob_id": part.blob_id}
    return {"type": "image", "media_type": part.media_type, "data_b64": part.data_b64}


def part_from_dict(data: dict) -> Part:
    kind = data.get("type")
    if kind == "text":
        return TextPart(str(data.get("text", "")))
    if kind == "image":
        blob = data.get("blob_id")
        return ImagePart(str(data["media_type"]), str(data.get("data_b64") or ""), str(blob) if blob else None)
    raise ValueError(f"unknown part type {kind!r}")


def tool_call_to_dict(call: ToolCall) -> dict:
    return {"id": call.id, "name": call.name, "arguments": call.arguments}


def tool_call_from_dict(data: dict) -> ToolCall:
    return ToolCall(str(data["id"]), str(data["name"]), str(data.get("arguments") or "{}"))


def reasoning_to_dict(item: ReasoningItem) -> dict:
    return {"provider": item.provider, "model": item.model, "kind": item.kind, "payload": item.payload}


def reasoning_from_dict(data: dict) -> ReasoningItem:
    return ReasoningItem(str(data["provider"]), str(data["model"]), str(data["kind"]), dict(data["payload"]))
