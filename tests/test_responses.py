import json

import httpx
import pytest

from keel.models import (
    Completed, ImagePart, Message, ModelRequest, ReasoningItem, ReasoningReady, TextDelta, TextPart, ToolCall,
    ToolCallReady, Usage,
)
from keel.providers.base import ModelError
from keel.providers.responses import (
    ResponsesTransport, cache_key, error_from_http, parse_usage, to_input_items,
)

URL = "https://model.test/openai/v1/responses"
OWN = ReasoningItem("bedrock", "m1", "reasoning", {"encrypted_content": "enc-own", "summary": []})
FOREIGN = ReasoningItem("bedrock", "m2", "reasoning", {"encrypted_content": "enc-other", "summary": []})


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


COMPLETED = {"type": "response.completed", "response": {"status": "completed", "usage": {
    "input_tokens": 1200, "output_tokens": 30, "input_tokens_details": {"cached_tokens": 1000}}}}


def transport_for(handler) -> tuple[ResponsesTransport, list]:
    seen = []

    def record(request: httpx.Request):
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return ResponsesTransport("bedrock", URL, client), seen


def request(**overrides) -> ModelRequest:
    base = dict(provider="bedrock", model="m1", system="SYSTEM", instructions="RUN",
                messages=(Message("user", (TextPart("hello"),)),), reasoning_effort="low", cache_scope="s1")
    base.update(overrides)
    return ModelRequest(**base)


async def collect(transport, req):
    return [event async for event in transport.stream(req)]


def test_the_body_carries_instructions_effort_cache_key_and_no_storage():
    transport, _ = transport_for(lambda r: httpx.Response(200))
    body = transport.build_body(request())
    assert body["instructions"] == "SYSTEM\n\nRUN" and body["model"] == "m1"
    assert body["stream"] is True and body["store"] is False
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["prompt_cache_key"] == cache_key("s1", "SYSTEM", ())
    assert "tools" not in body
    assert transport.build_body(request(instructions=None))["instructions"] == "SYSTEM"
    assert transport.build_body(request(reasoning_effort="none"))["reasoning"] == {"effort": "none"}
    assert "reasoning" not in transport.build_body(request(reasoning_effort=None))
    # Encrypted reasoning is always requested, whatever the effort.
    for effort in (None, "none", "low"):
        assert transport.build_body(request(reasoning_effort=effort))["include"] == ["reasoning.encrypted_content"]
    assert "max_output_tokens" not in body
    assert transport.build_body(request(max_output_tokens=256))["max_output_tokens"] == 256


def test_the_cache_key_ignores_run_instructions_but_not_system_or_tools():
    tool = {"name": "t", "description": "d", "parameters": {"type": "object"}}
    assert cache_key("s", "A", ()) == cache_key("s", "A", ())
    assert cache_key("s", "A", ()) != cache_key("s", "B", ())
    assert cache_key("s", "A", ()) != cache_key("s", "A", (tool,))
    assert cache_key("s", "A", ()).startswith("pck_") and len(cache_key("s", "A", ())) == 28


def test_tools_become_function_tools():
    transport, _ = transport_for(lambda r: httpx.Response(200))
    tool = {"name": "terminal", "description": "Run a command", "parameters": {"type": "object"}}
    body = transport.build_body(request(tools=(tool,)))
    assert body["tools"] == [{"type": "function", "name": "terminal", "description": "Run a command",
                              "parameters": {"type": "object"}}]
    assert body["tool_choice"] == "auto" and body["parallel_tool_calls"] is True


def test_input_items_replay_only_this_models_reasoning():
    messages = (
        Message("user", (TextPart("q"), ImagePart("image/png", "AAAA"))),
        Message("assistant", (TextPart("answer"),), tool_calls=(ToolCall("c1", "terminal", '{"command":"ls"}'),),
                reasoning=(OWN, FOREIGN)),
        Message("tool", (TextPart("out"),), tool_call_id="c1", tool_name="terminal"),
    )
    assert to_input_items(messages, "bedrock", "m1") == [
        {"role": "user", "content": [{"type": "input_text", "text": "q"},
                                     {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
        {"type": "reasoning", "encrypted_content": "enc-own", "summary": []},
        {"role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
        {"type": "function_call", "call_id": "c1", "name": "terminal", "arguments": '{"command":"ls"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "out"},
    ]


async def test_a_stream_yields_text_reasoning_and_usage():
    events = (
        {"type": "response.created", "response": {}},
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.output_item.done", "item": {"type": "reasoning", "id": "rs_1",
                                                       "encrypted_content": "ENC", "summary": []}},
        COMPLETED,
    )
    transport, seen = transport_for(lambda r: httpx.Response(200, content=sse(*events)))
    result = await collect(transport, request())
    assert result == [
        TextDelta("Hel"), TextDelta("lo"),
        ReasoningReady(ReasoningItem("bedrock", "m1", "reasoning", {"encrypted_content": "ENC", "summary": []})),
        Completed(Usage(1200, 30, 1000, None)),
    ]
    assert seen[0].url == URL and json.loads(seen[0].content)["model"] == "m1"


async def test_function_calls_come_from_done_items_or_are_settled_at_completion():
    events = (
        {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_1",
                                                        "call_id": "c1", "name": "a", "arguments": ""}},
        {"type": "response.output_item.done", "item": {"type": "function_call", "id": "fc_1",
                                                       "call_id": "c1", "name": "a", "arguments": '{"x":1}'}},
        {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_2",
                                                        "call_id": "c2", "name": "b", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_2", "delta": '{"y":'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_2", "delta": "2}"},
        {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_3",
                                                        "call_id": "c3", "name": "c", "arguments": ""}},
        COMPLETED,
    )
    transport, _ = transport_for(lambda r: httpx.Response(200, content=sse(*events)))
    result = await collect(transport, request())
    calls = [e.call for e in result if isinstance(e, ToolCallReady)]
    assert calls == [ToolCall("c1", "a", '{"x":1}'), ToolCall("c2", "b", '{"y":2}'), ToolCall("c3", "c", "{}")]
    assert isinstance(result[-1], Completed)


async def test_a_stream_that_ends_early_is_a_server_error():
    transport, _ = transport_for(lambda r: httpx.Response(200, content=sse(
        {"type": "response.output_text.delta", "delta": "x"})))
    with pytest.raises(ModelError) as caught:
        await collect(transport, request())
    assert caught.value.code == "server_error"


async def test_a_failed_response_maps_its_error():
    transport, _ = transport_for(lambda r: httpx.Response(200, content=sse(
        {"type": "response.failed", "response": {"error": {"code": "invalid_encrypted_content",
                                                           "message": "bad blob"}}})))
    with pytest.raises(ModelError) as caught:
        await collect(transport, request())
    assert caught.value.code == "invalid_reasoning"


async def test_an_http_error_is_raised_before_any_event():
    transport, _ = transport_for(lambda r: httpx.Response(
        429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}}))
    with pytest.raises(ModelError) as caught:
        await collect(transport, request())
    assert (caught.value.code, caught.value.retry_after) == ("rate_limited", 7.0)


async def test_a_connection_failure_is_a_server_error_and_a_timeout_is_a_timeout():
    def refuse(r):
        raise httpx.ConnectError("refused", request=r)

    def stall(r):
        raise httpx.ReadTimeout("slow", request=r)

    for handler, code in ((refuse, "server_error"), (stall, "timeout")):
        transport, _ = transport_for(handler)
        with pytest.raises(ModelError) as caught:
            await collect(transport, request())
        assert caught.value.code == code


@pytest.mark.parametrize(("status", "body", "code"), [
    (401, '{"message": "denied"}', "auth"),
    (403, "forbidden", "auth"),
    (413, "too big", "context_overflow"),
    (400, '{"error": {"code": "context_length_exceeded", "message": "too long"}}', "context_overflow"),
    (400, '{"message": "Input is too long for the maximum context window"}', "context_overflow"),
    (400, '{"error": {"message": "encrypted reasoning was created for a different model"}}', "invalid_reasoning"),
    (400, '{"error": {"message": "bad field"}}', "bad_request"),
    (408, "", "timeout"),
    (500, "", "server_error"),
    (503, "", "server_error"),
])
def test_http_errors_map_to_neutral_codes(status, body, code):
    assert error_from_http(status, body, None).code == code


def test_usage_is_none_when_unreported_and_zero_writes_when_details_exist():
    assert parse_usage(None) == Usage()
    assert parse_usage({"input_tokens": 5, "output_tokens": 1}) == Usage(5, 1, None, None)
    assert parse_usage({"input_tokens": 5, "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2}}) == Usage(5, 1, 3, 2)


async def test_a_last_event_without_its_trailing_blank_line_is_still_delivered():
    raw = sse({"type": "response.output_text.delta", "delta": "Hi"}).decode() + \
        "event: response.completed\ndata: " + json.dumps(COMPLETED)  # no final blank line
    transport, _ = transport_for(lambda r: httpx.Response(200, content=raw.encode()))
    result = await collect(transport, request())
    assert result[0] == TextDelta("Hi") and isinstance(result[-1], Completed)


def test_cache_write_is_none_unless_the_provider_reports_it():
    assert parse_usage({"input_tokens": 5, "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 3}}) == Usage(5, 1, 3, None)
    assert parse_usage({"input_tokens": 5, "output_tokens": 1}) == Usage(5, 1, None, None)
    assert (Usage(5, 1, 3, None) + Usage(2, 1, 0, None)).cache_write_tokens is None
    assert Usage(5, 1, 3, None).to_dict()["cache_write_tokens"] is None


async def test_keep_alives_do_not_hide_a_stalled_stream():
    import asyncio

    async def keep_alive():
        yield sse({"type": "response.created", "response": {}})
        while True:  # what a stalled Mantle stream sends: bytes, but no response event
            await asyncio.sleep(0.02)
            yield b": keep-alive\n\n"
            yield b'data: {"type": "keepalive"}\n\n'

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=keep_alive())))
    transport = ResponsesTransport("bedrock", URL, client, stall_seconds=0.2)
    with pytest.raises(ModelError) as caught:
        await asyncio.wait_for(collect(transport, request()), 5)
    assert caught.value.code == "timeout" and "no response event" in str(caught.value)


async def test_a_slow_stream_that_keeps_sending_response_events_is_not_cut_off():
    import asyncio

    async def slow():
        for word in ("a", "b", "c", "d"):
            await asyncio.sleep(0.1)
            yield sse({"type": "response.output_text.delta", "delta": word})
        yield sse(COMPLETED)

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=slow())))
    transport = ResponsesTransport("bedrock", URL, client, stall_seconds=0.25)
    events = await collect(transport, request())
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "abcd"
    assert isinstance(events[-1], Completed)
