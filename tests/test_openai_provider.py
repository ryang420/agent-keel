import json
import os

import httpx
import pytest

from keel.models import Completed, Message, ModelRequest, TextDelta, TextPart, ToolCall, ToolCallReady
from keel.providers.base import ModelError
from keel.providers.openai import OPENAI_CAPABILITIES, OpenAIProvider
from keel.providers.responses import ResponsesTransport

REQUEST = ModelRequest("openai", "gpt-5-mini", "You help.", (Message("user", (TextPart("hi"),)),),
                       reasoning_effort="low", cache_scope="s1")


def sse(*events: dict) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


COMPLETED = {"type": "response.completed", "response": {"usage": {"input_tokens": 5, "output_tokens": 2}}}


def provider_for(handler, **kwargs):
    seen = []

    def wrapped(request: httpx.Request):
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped),
                               headers={"Authorization": "Bearer sk-test"})
    return OpenAIProvider("sk-test", client=client, **kwargs), seen


async def test_the_request_is_bearer_authenticated_with_the_shared_body():
    provider, seen = provider_for(lambda r: httpx.Response(200, content=sse(COMPLETED)))
    [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    assert str(seen[0].url) == "https://api.openai.com/v1/responses"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    body = json.loads(seen[0].content)
    probe = httpx.AsyncClient()
    expected = ResponsesTransport("openai", "x", probe).build_body(REQUEST)
    await probe.aclose()
    assert body == expected
    assert body["store"] is False and "reasoning.encrypted_content" in body["include"]
    assert body["prompt_cache_key"]


async def test_the_default_client_sends_the_bearer_header():
    provider = OpenAIProvider("sk-real")
    try:
        assert provider._client.headers["authorization"] == "Bearer sk-real"
    finally:
        await provider.aclose()


async def test_text_tool_calls_and_usage_come_through():
    events = (
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_1", "call_id": "c1",
                                                       "name": "terminal", "arguments": '{"command":"ls"}'}},
        COMPLETED,
    )
    provider, _ = provider_for(lambda r: httpx.Response(200, content=sse(*events)))
    result = [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    assert [e.text for e in result if isinstance(e, TextDelta)] == ["Hel", "lo"]
    assert [e.call for e in result if isinstance(e, ToolCallReady)] == [ToolCall("c1", "terminal", '{"command":"ls"}')]
    assert isinstance(result[-1], Completed)
    assert (result[-1].usage.input_tokens, result[-1].usage.output_tokens) == (5, 2)


async def test_a_401_is_an_auth_error():
    provider, _ = provider_for(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(ModelError) as caught:
        [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    assert caught.value.code == "auth"


async def test_a_429_carries_retry_after():
    provider, _ = provider_for(lambda r: httpx.Response(429, headers={"Retry-After": "7"},
                                                        json={"error": {"message": "slow down"}}))
    with pytest.raises(ModelError) as caught:
        [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    assert caught.value.code == "rate_limited" and caught.value.retry_after == 7


async def test_a_trailing_slash_and_a_custom_name_are_handled():
    provider, seen = provider_for(lambda r: httpx.Response(200, content=sse(COMPLETED)),
                                  base_url="http://llm.local:8000/v1/", name="local")
    [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    assert provider.name == "local"
    assert str(seen[0].url) == "http://llm.local:8000/v1/responses"


async def test_capabilities():
    provider = OpenAIProvider("sk-test")
    caps = provider.capabilities("gpt-5-mini")
    assert provider.name == "openai" and caps == OPENAI_CAPABILITIES
    assert caps.vision and caps.reasoning_replay and caps.prompt_cache == "key"
    await provider.aclose()


@pytest.mark.skipif(os.environ.get("KEEL_LIVE_OPENAI") != "1", reason="set KEEL_LIVE_OPENAI=1 to call OpenAI")
async def test_live_openai_answers():
    key = os.environ.get("KEEL_OPENAI_API_KEY") or os.environ["OPENAI_API_KEY"]
    base = os.environ.get("KEEL_OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("KEEL_LIVE_OPENAI_MODEL", "gpt-5-mini")
    provider = OpenAIProvider(key, base_url=base)
    request = ModelRequest("openai", model, "Reply tersely.", (Message("user", (TextPart("Reply with exactly: OK"),)),),
                           reasoning_effort="low", cache_scope="live")
    try:
        events = [e async for e in provider.stream(request)]
    finally:
        await provider.aclose()
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "OK" in text and isinstance(events[-1], Completed) and events[-1].usage.input_tokens


async def test_an_error_body_that_echoes_the_key_is_scrubbed():
    provider, _ = provider_for(lambda r: httpx.Response(401, text="Incorrect API key provided: sk-test"))
    with pytest.raises(ModelError) as caught:
        [e async for e in provider.stream(REQUEST)]
    assert caught.value.code == "auth" and "sk-test" not in str(caught.value)
    assert str(caught.value) == "The provider rejected the API key."
    provider, _ = provider_for(lambda r: httpx.Response(500, text="upstream failed for Bearer sk-test"))
    with pytest.raises(ModelError) as caught:
        [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    assert caught.value.code == "server_error" and "sk-test" not in str(caught.value)
    assert "***" in str(caught.value)


async def test_compat_omits_fields_strict_servers_reject():
    provider, seen = provider_for(lambda r: httpx.Response(200, content=sse(COMPLETED)), compat=True)
    [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    body = json.loads(seen[0].content)
    assert "include" not in body and "prompt_cache_key" not in body
    assert body["reasoning"] == {"effort": "low"}
    assert provider.capabilities("m").prompt_cache == "none"


async def test_compat_off_keeps_them():
    provider, seen = provider_for(lambda r: httpx.Response(200, content=sse(COMPLETED)))
    [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    body = json.loads(seen[0].content)
    assert body["include"] and body["prompt_cache_key"] and body["reasoning"]["summary"] == "auto"


async def test_the_context_window_defaults_and_overrides():
    default = OpenAIProvider("k")
    custom = OpenAIProvider("k", context_window=32_000)
    assert default.capabilities("m").context_window == 128_000
    assert custom.capabilities("m").context_window == 32_000
    await default.aclose()
    await custom.aclose()


async def test_plain_http_warns(caplog):
    with caplog.at_level("WARNING"):
        provider = OpenAIProvider("k", base_url="http://llm:8000/v1")
    await provider.aclose()
    assert "clear text" in caplog.text
