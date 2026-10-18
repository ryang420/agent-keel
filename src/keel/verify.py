"""Whether a model id works, and whether it reads images: two tiny calls through the provider itself.

The first call is text only: the id routes and the account may call it. The second adds a 1x1 PNG. Image support is
decided from that call's real answer, never guessed. Nothing is stored.
"""

from __future__ import annotations

import asyncio

from keel.models import Completed, ImagePart, Message, ModelRequest, TextPart
from keel.providers.base import ModelError, ModelProvider

TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
PROMPT = "Reply with OK"
MAX_OUTPUT_TOKENS = 16
UNAVAILABLE = "The model provider didn't answer; try again."


class VerifyError(Exception):
    """code: access_denied, not_found or unsupported (the model is refused), or unavailable (try again)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _not_found(text: str) -> bool:
    text = text.lower()
    return "model" in text and any(w in text for w in ("not found", "does not exist", "identifier", "unknown"))


def classify(error: ModelError) -> VerifyError:
    text = str(error)
    if error.code == "auth":
        return VerifyError("access_denied", "This account has no access to that model.")
    if error.code in ("rate_limited", "server_error", "timeout"):
        return VerifyError("unavailable", UNAVAILABLE)
    if _not_found(text):
        return VerifyError("not_found", "The provider doesn't recognize this model id.")
    return VerifyError("unsupported", "The provider rejected the test request for this model.")


async def _call(provider: ModelProvider, model: str, parts: tuple, timeout: float) -> None:
    request = ModelRequest(provider=provider.name, model=model, system="",
                           messages=(Message(role="user", parts=parts),), max_output_tokens=MAX_OUTPUT_TOKENS)

    async def drain() -> None:
        async for event in provider.stream(request):
            if isinstance(event, Completed):
                return

    try:
        await asyncio.wait_for(drain(), timeout)
    except asyncio.TimeoutError:
        raise ModelError("timeout", "The verification call timed out.") from None


async def verify_model(provider: ModelProvider, model: str, *, timeout: float = 30.0) -> bool:
    """True when the model reads images, False when it refuses them; VerifyError when the model is unusable."""
    try:
        await _call(provider, model, (TextPart(PROMPT),), timeout)
    except ModelError as exc:
        raise classify(exc) from exc
    try:
        await _call(provider, model, (TextPart(PROMPT), ImagePart("image/png", TINY_PNG_B64)), timeout)
    except ModelError as exc:
        if exc.code == "bad_request" and "image" in str(exc).lower():
            return False
        raise classify(exc) from exc
    return True
