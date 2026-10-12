"""What every model provider implements, and the one error type that leaves a provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from keel.models import ModelEvent, ModelRequest

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
