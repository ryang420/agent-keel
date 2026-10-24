"""Bedrock Mantle: OpenAI-family models over the OpenAI Responses API, and Claude models (ids starting with
`anthropic.`) over the Anthropic Messages API, on the same host and SigV4 credentials."""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from keel.models import ModelEvent, ModelRequest
from keel.providers.base import READ_TIMEOUT_SECONDS, Capabilities, StallLimits
from keel.providers.messages import MessagesTransport
from keel.providers.responses import ResponsesTransport
from keel.providers.sigv4 import SigV4Auth

# OpenAI-family models on Mantle have a 272k window, takes images, needs its encrypted
# reasoning replayed, and caches by prompt_cache_key.
MANTLE_CAPABILITIES = Capabilities(context_window=272_000, vision=True, reasoning_replay=True,
                                   parallel_tool_calls=True, prompt_cache="key")
# Claude models: every one Mantle serves takes at least 200k tokens and images, replays its thinking blocks and
# caches at cache_control breakpoints.
CLAUDE_CAPABILITIES = Capabilities(context_window=200_000, vision=True, reasoning_replay=True,
                                   parallel_tool_calls=True, prompt_cache="breakpoints")
CLAUDE_PREFIX = "anthropic."


def mantle_url(region: str) -> str:
    return f"https://bedrock-mantle.{region}.api.aws/openai/v1/responses"


def claude_url(region: str) -> str:
    return f"https://bedrock-mantle.{region}.api.aws/anthropic/v1/messages"


class BedrockMantleProvider:
    name = "bedrock"

    def __init__(self, region: str, *, client: httpx.AsyncClient | None = None, credentials=None,
                 timeout: float = READ_TIMEOUT_SECONDS, stall: StallLimits = StallLimits()) -> None:
        self.region = region
        self._client = client or httpx.AsyncClient(
            auth=SigV4Auth(region, credentials=credentials),
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        self._transport = ResponsesTransport(self.name, mantle_url(region), self._client, stall=stall)
        self._claude = MessagesTransport(self.name, claude_url(region), self._client, stall=stall)

    def capabilities(self, model: str) -> Capabilities:
        return CLAUDE_CAPABILITIES if model.startswith(CLAUDE_PREFIX) else MANTLE_CAPABILITIES

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        if request.model.startswith(CLAUDE_PREFIX):
            return self._claude.stream(request)
        return self._transport.stream(request)

    async def aclose(self) -> None:
        await self._client.aclose()
