"""Per-request token accounting for executions.

The provider is wrapped in a transparent proxy: the request and the events are passed through unchanged, while each
call reserves an input estimate plus the output cap before it starts and charges the reported usage when it ends.
Titles and any background calls made through the registry are charged too. A call that cannot reserve raises
``BudgetExhausted`` before the upstream provider is touched.
"""

from __future__ import annotations

import json

from keel.models import Completed, ModelRequest, part_to_dict

from .budget import Ledger


class Charging:
    """The ledger plus the case whose next request is being charged."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.case = ""

    def switch(self, case_id: str) -> None:
        self.case = case_id


def request_tokens(request: ModelRequest) -> int:
    """Estimate a request's input tokens from its text and tool schemas (characters over four)."""
    chars = len(request.system) + len(request.instructions or "") + len(json.dumps(request.tools, default=str))
    for message in request.messages:
        for part in message.parts:
            chars += len(part_to_dict(part).get("text") or "")
    return max(1, chars // 4)


def _charge(usage) -> int | None:
    if usage.input_tokens is None or usage.output_tokens is None:
        return None
    return usage.input_tokens + usage.output_tokens


class BudgetProvider:
    """A provider proxy that admits every call through the ledger and forwards the stream unchanged."""

    def __init__(self, inner, charging: Charging, *, max_output: int = 8192) -> None:
        self._inner = inner
        self._charging = charging
        self._max_output = max_output

    @property
    def name(self) -> str:
        return self._inner.name

    def capabilities(self, model: str):
        return self._inner.capabilities(model)

    async def stream(self, request: ModelRequest):
        ledger = self._charging.ledger
        case_id = self._charging.case
        reserved = ledger.reserve(case_id, request_tokens(request) + self._max_output)
        settled = False
        try:
            async for event in self._inner.stream(request):
                if isinstance(event, Completed) and not settled:
                    ledger.settle(case_id, reserved, _charge(event.usage))
                    settled = True
                yield event
        finally:
            if not settled:
                ledger.settle(case_id, reserved, None)

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()
