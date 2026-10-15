"""What every model provider implements, and the one error type that leaves a provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from keel.models import ModelEvent, ModelRequest

# Seconds without any byte before a model call fails as "timeout". Only a backstop for a dead connection: a
# stalled Mantle stream sends keep-alives but no events, which keeps resetting it. StallLimits catch that.
READ_TIMEOUT_SECONDS = 600.0
HIGH_EFFORTS = frozenset({"high", "xhigh", "max"})


@dataclass(frozen=True)
class StallLimits:
    """How long a model call may go without a response event before it fails as "timeout", which is retried
    while nothing has been streamed yet.

    The first event (response.created) normally arrives within seconds. After it, a service may stay silent
    while it prefills a large prompt or reasons, so the gap between events is allowed to grow with the request
    size; at high effort both limits are raised to `high_effort`, since the model may think for minutes first.
    """

    first_event: float = 120.0
    between_events: float = 60.0
    high_effort: float = 600.0

    def for_request(self, effort: str | None, body_bytes: int) -> tuple[float, float]:
        """(first-event limit, between-events limit) for one call. Tokens are estimated as body bytes / 4;
        images inflate the estimate, which only lengthens the limit."""
        tokens = body_bytes // 4
        between = max(self.between_events, 180.0 if tokens > 100_000 else 120.0 if tokens > 50_000 else 0.0)
        floor = self.high_effort if effort in HIGH_EFFORTS else 0.0
        return max(self.first_event, floor), max(between, floor)

ERROR_CODES = ("auth", "rate_limited", "server_error", "timeout", "context_overflow",
               "invalid_reasoning", "bad_request")


class ModelError(Exception):
    def __init__(self, code: str, message: str, retry_after: float | None = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown model error code {code!r}")
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class Capabilities:
    context_window: int
    vision: bool
    reasoning_replay: bool
    parallel_tool_calls: bool
    prompt_cache: str  # "key" | "breakpoints" | "none"


class ModelProvider(Protocol):
    name: str

    def capabilities(self, model: str) -> Capabilities: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider: ModelProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> ModelProvider:
        return self._providers[name]

    def names(self) -> list[str]:
        return sorted(self._providers)

    async def aclose(self) -> None:
        for provider in self._providers.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
