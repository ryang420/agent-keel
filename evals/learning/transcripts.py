"""Dream transcripts for the learning curve. The curve's database is dropped when it ends, and a refused proposal is
only ever a tool result handed back to the model, so why a dream committed nothing is kept here as it happens: for
each dream call, the messages added since the attempt's previous call and the model's reply, under the evaluator's
tree (never a sandbox mount)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from keel.models import Completed, Message, ModelRequest, TextDelta, ToolCallReady
from keel.providers.base import ProviderRegistry
from keel.tools.base import redact


def _message(message: Message) -> dict:
    entry: dict = {"role": message.role, "text": redact(message.text)}
    if message.tool_calls:
        entry["tool_calls"] = [{"name": call.name, "arguments": redact(call.arguments)} for call in message.tool_calls]
    if message.tool_name:
        entry["tool_name"] = message.tool_name
    return entry


class DreamRecorder:
    """Where dream calls are written: `target` is set while the curve drains a sequence's dreams, None otherwise."""

    def __init__(self) -> None:
        self.target: Path | None = None
        self._seen = 0  # messages of the current attempt already written

    def wrap(self, registry: ProviderRegistry) -> ProviderRegistry:
        wrapped = ProviderRegistry()
        for name in registry.names():
            wrapped.register(_Recording(registry.get(name), self))
        return wrapped

    def start(self, target: Path | None) -> None:
        self.target, self._seen = target, 0

    def write(self, request: ModelRequest, text: str, calls: list[dict], usage, error: str | None = None) -> None:
        if self.target is None:
            return
        messages = request.messages
        # a request no longer than the last one written starts a new attempt
        new = messages[self._seen:] if len(messages) > self._seen else messages
        self._seen = len(messages)
        line = {"at": time.time(), "model": request.model, "messages": [_message(m) for m in new],
                "reply": {"text": redact(text), "tool_calls": calls},
                "usage": usage.to_dict() if usage is not None else None}
        if error is not None:
            line["error"] = error
        self.target.parent.mkdir(parents=True, exist_ok=True)
        with self.target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, default=str) + "\n")


class _Recording:
    """A provider proxy that forwards the stream unchanged and hands each dream call to the recorder when it ends."""

    def __init__(self, inner, recorder: DreamRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    @property
    def name(self) -> str:
        return self._inner.name

    def capabilities(self, model: str):
        return self._inner.capabilities(model)

    async def stream(self, request: ModelRequest):
        if self._recorder.target is None:
            async for event in self._inner.stream(request):
                yield event
            return
        text: list[str] = []
        calls: list[dict] = []
        usage = None
        error: str | None = None
        try:
            async for event in self._inner.stream(request):
                if isinstance(event, TextDelta):
                    text.append(event.text)
                elif isinstance(event, ToolCallReady):
                    calls.append({"name": event.call.name, "arguments": redact(event.call.arguments)})
                elif isinstance(event, Completed):
                    usage = event.usage
                yield event
        except Exception as exc:
            error = redact(f"{type(exc).__name__}: {exc}")[:300]
            raise
        finally:
            self._recorder.write(request, "".join(text), calls, usage, error)

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()
