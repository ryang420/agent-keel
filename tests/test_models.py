import pytest

from keel.models import (
    ImagePart, Message, ReasoningItem, TextPart, ToolCall, Usage, part_from_dict, part_to_dict,
    reasoning_from_dict, reasoning_to_dict, tool_call_from_dict, tool_call_to_dict,
)


def test_message_text_joins_text_parts_only():
    message = Message("user", (TextPart("first"), ImagePart("image/png", "AAAA"), TextPart("second")))
    assert message.text == "first\nsecond"


def test_usage_adds_known_counts_and_keeps_unknown_as_none():
    total = Usage(10, 2, 8, None) + Usage(5, 1, None, None)
    assert total == Usage(15, 3, 8, None)
    assert Usage() + Usage() == Usage()


def test_usage_round_trips_through_a_dict():
    usage = Usage(100, 20, 80, 0)
    assert Usage.from_dict(usage.to_dict()) == usage
    assert Usage.from_dict({}) == Usage() and Usage.from_dict(None) == Usage()


@pytest.mark.parametrize("part", [TextPart("hi"), ImagePart("image/png", "AAAA")])
def test_parts_round_trip(part):
    assert part_from_dict(part_to_dict(part)) == part


def test_an_unknown_part_type_is_refused():
    with pytest.raises(ValueError, match="audio"):
        part_from_dict({"type": "audio"})


def test_tool_calls_and_reasoning_round_trip():
    call = ToolCall("call_1", "terminal", '{"command": "ls"}')
    item = ReasoningItem("bedrock", "openai.gpt-6-luna", "reasoning", {"encrypted_content": "x"})
    assert tool_call_from_dict(tool_call_to_dict(call)) == call
    assert reasoning_from_dict(reasoning_to_dict(item)) == item


def test_an_image_part_round_trips_as_data_or_as_a_reference():
    inline = ImagePart("image/png", "aGVsbG8=")
    stored = ImagePart("image/png", blob_id="blob_1")
    assert part_to_dict(inline) == {"type": "image", "media_type": "image/png", "data_b64": "aGVsbG8="}
    assert part_to_dict(stored) == {"type": "image", "media_type": "image/png", "blob_id": "blob_1"}
    assert part_from_dict(part_to_dict(inline)) == inline
    assert part_from_dict(part_to_dict(stored)) == stored
