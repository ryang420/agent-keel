import pytest

from keel.models import Completed, Message, ModelRequest, ReasoningItem, TextDelta, TextPart, Usage
from keel.providers.base import ModelError, ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.providers.retry import backoff_seconds, stream_with_retries

ITEM = ReasoningItem("fake", "m", "reasoning", {"encrypted_content": "e"})
REQUEST = ModelRequest("fake", "m", "system", (Message("assistant", (TextPart("a"),), reasoning=(ITEM,)),
                                                Message("user", (TextPart("q"),))))


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


async def collect(provider, sleep=None, **kwargs):
    return [event async for event in stream_with_retries(provider, REQUEST, sleep=sleep or Sleeps(), **kwargs)]


async def test_a_transient_failure_before_any_output_is_retried():
    sleep = Sleeps()
    provider = FakeProvider(script=[[ModelError("server_error", "boom")],
                                    [TextDelta("hi"), Completed(Usage(1, 1, 0, 0))]])
    events = await collect(provider, sleep=sleep, rng=lambda: 0.0)
    assert events == [TextDelta("hi"), Completed(Usage(1, 1, 0, 0))]
    assert sleep.calls == [5.0] and len(provider.requests) == 2


async def test_a_failure_after_output_is_not_retried():
    provider = FakeProvider(script=[[TextDelta("partial"), ModelError("server_error", "boom")]])
    with pytest.raises(ModelError, match="boom"):
        await collect(provider)
    assert len(provider.requests) == 1


async def test_retries_stop_after_the_last_attempt():
    provider = FakeProvider(script=[[ModelError("timeout", "slow")]] * 3)
    with pytest.raises(ModelError) as caught:
        await collect(provider)
    assert caught.value.code == "timeout" and len(provider.requests) == 3


async def test_a_non_retryable_error_is_raised_at_once():
    provider = FakeProvider(script=[[ModelError("bad_request", "no")]])
    with pytest.raises(ModelError):
        await collect(provider)
    assert len(provider.requests) == 1


async def test_invalid_reasoning_retries_once_without_reasoning():
    provider = FakeProvider(script=[[ModelError("invalid_reasoning", "stale")],
                                    [Completed(Usage())]])
    await collect(provider)
    assert provider.requests[0].messages[0].reasoning == (ITEM,)
    assert provider.requests[1].messages[0].reasoning == ()


def test_backoff_doubles_caps_and_honours_retry_after():
    assert [backoff_seconds(n, None, 0.0) for n in (1, 2, 3, 6, 9)] == [5.0, 10.0, 20.0, 120.0, 120.0]
    assert backoff_seconds(1, None, 1.0) == 7.5
    assert backoff_seconds(1, -5.0, 0.5) == 0.0
    assert backoff_seconds(1, 30.0, 0.5) == 30.0 and backoff_seconds(1, 9_999.0, 0.5) == 600.0


async def test_the_registry_finds_providers_by_name():
    registry = ProviderRegistry()
    provider = FakeProvider()
    registry.register(provider)
    assert registry.get("fake") is provider and registry.names() == ["fake"]
    with pytest.raises(KeyError):
        registry.get("other")
    await registry.aclose()


def test_model_error_carries_code_and_message():
    error = ModelError("rate_limited", "slow down", retry_after=3.0)
    assert (error.code, str(error), error.retry_after) == ("rate_limited", "slow down", 3.0)
    with pytest.raises(ValueError):
        ModelError("nonsense", "x")
