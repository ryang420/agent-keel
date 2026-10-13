import json

from keel import compress, prompts
from keel.models import ImagePart, Message, ReasoningItem, TextPart, ToolCall
from keel.store.base import StoredMessage


def stored(*messages):
    return [StoredMessage(index + 1, "s1", None, message, 0.0) for index, message in enumerate(messages)]


def user(text):
    return Message("user", (TextPart(text),))


def assistant(text="", *calls):
    return Message("assistant", (TextPart(text),) if text else (), tool_calls=tuple(calls))


def result(call_id, text, name="terminal"):
    return Message("tool", (TextPart(text),), tool_call_id=call_id, tool_name=name)


def call(call_id, name="terminal", **args):
    return ToolCall(call_id, name, json.dumps(args))


BIG = "x" * 16_000  # 4,000 tokens


def test_ascii_counts_a_quarter_token_and_other_characters_one():
    assert compress.text_tokens("") == 0
    assert compress.text_tokens("abcd" * 100) == 100
    assert compress.text_tokens("数据分析") == 4
    assert compress.text_tokens("ab数据") == 1 + 2


def test_an_image_has_a_fixed_cost_and_the_base_counts_system_and_tools():
    picture = Message("user", (TextPart("abcd"), ImagePart("image/png", blob_id="b1")))
    assert compress.message_tokens(picture) == compress.MESSAGE_OVERHEAD + 1 + compress.IMAGE_TOKENS
    assert compress.base_tokens("abcd", ({"a": 1},)) == 1 + 2


def test_the_tail_budget_is_a_share_of_the_window_clamped():
    assert compress.tail_budget(128_000) == 10_000
    assert compress.tail_budget(600_000) == 15_000
    assert compress.tail_budget(2_000_000) == 25_000


def test_the_head_is_three_messages_extended_to_cover_tool_results():
    assert compress.head_end(stored(user("a"), assistant("b"), user("c"), assistant("d"))) == 3
    calls = stored(user("a"), assistant("", call("c1"), call("c2")), result("c1", "x"), result("c2", "y"),
                   assistant("z"))
    assert compress.head_end(calls) == 4
    assert compress.head_end(stored(user("a"))) == 1


def test_the_tail_fits_its_budget_and_never_splits_a_call_from_its_result():
    messages = stored(user(BIG), assistant(BIG), user(BIG), assistant("", call("c1")), result("c1", BIG),
                      assistant(BIG), user("next"))
    assert compress.tail_start(messages, 10_000) == 3
    assert compress.tail_start(messages, 5_000) == 5
    assert compress.tail_start(messages, 1) == 6
    newest_round = stored(user("a"), assistant("", call("c1")), result("c1", BIG))
    assert compress.tail_start(newest_round, 1) == 1


def test_the_placeholder_pass_replaces_long_tool_results_and_images():
    long = compress.placeholder(result("c1", "y" * 201, name="read_file"))
    assert long.text == "[read_file result omitted from context: 201 characters]"
    assert (long.tool_call_id, long.tool_name) == ("c1", "read_file")
    short = result("c2", "y" * 200)
    assert compress.placeholder(short) == short
    picture = Message("user", (TextPart("see"), ImagePart("image/png", blob_id="b1")))
    assert compress.placeholder(picture).parts == (TextPart("see"), TextPart(compress.IMAGE_OMITTED))


def test_with_no_boundary_every_message_is_sent_as_stored():
    messages = stored(user("a"), assistant("b"))
    assert compress.assemble(messages, compress.Boundaries(), 10_000) == [
        compress.Item(1, messages[0].message), compress.Item(2, messages[1].message)]


def test_a_summary_replaces_the_middle_between_head_and_tail():
    messages = stored(user("a"), assistant("b"), user("c"), assistant("d"), user("e"), assistant("f"), user("g"))
    items = compress.assemble(messages, compress.Boundaries("S", 3, 6, 6), 10_000)
    assert [item.id for item in items] == [1, 2, 3, None, 6, 7]
    text = items[3].message.text
    assert items[3].message.role == "user"
    assert text == f"{prompts.text('summary_prefix')}\n\nS\n\n{compress.SUMMARY_END}"


def test_below_the_pruned_id_messages_get_the_placeholder_pass_and_lose_reasoning():
    item = ReasoningItem("fake", "m", "reasoning", {"encrypted_content": "e"})
    messages = stored(user("a"), assistant("b"), user("c"),
                      Message("assistant", (), (call("c1"),), reasoning=(item,)), result("c1", "z" * 500),
                      assistant("done"), user("next"))
    items = compress.assemble(messages, compress.Boundaries(pruned_id=6), 10_000)
    assert items[3].message.reasoning == () and items[3].message.tool_calls == messages[3].message.tool_calls
    assert items[4].message.text == "[terminal result omitted from context: 500 characters]"
    assert [items[i].message for i in (0, 1, 2, 5, 6)] == [messages[i].message for i in (0, 1, 2, 5, 6)]


def test_with_a_summary_a_head_message_over_the_limit_is_cut_in_the_request_only():
    messages = stored(user("q" * 40_000), assistant("b"), user("c"), assistant("d"), user("e"))
    items = compress.assemble(messages, compress.Boundaries("S", 3, 4, 4), 10_000)
    first = items[0].message.text
    assert "OUTPUT TRUNCATED" in first and compress.text_tokens(first) <= compress.HEAD_MESSAGE_TOKENS + 50
    assert messages[0].message.text == "q" * 40_000
    assert compress.assemble(messages, compress.Boundaries(pruned_id=4), 10_000)[0].message == messages[0].message


def test_with_a_summary_head_call_arguments_are_capped_as_json_and_images_become_notes():
    big_call = call("c1", "write_file", path="/w/a.txt", content="c" * 40_000)
    picture = Message("user", (TextPart("see"), ImagePart("image/png", blob_id="b1")))
    messages = stored(picture, assistant("", big_call), result("c1", "ok", name="write_file"), assistant("d"),
                      user("e"))
    items = compress.assemble(messages, compress.Boundaries("S", 3, 4, 4), 10_000)
    sent = items[1].message.tool_calls[0]
    assert sent.id == "c1" and len(sent.arguments) < 16_100 and json.loads(sent.arguments)["path"] == "/w/a.txt"
    assert items[0].message.parts == (TextPart("see"), TextPart(compress.IMAGE_OMITTED))
    unchanged = compress.assemble(messages, compress.Boundaries(pruned_id=4), 10_000)
    assert [item.message for item in unchanged[:3]] == [item.message for item in messages[:3]]


def test_with_a_summary_head_messages_lose_their_reasoning():
    item = ReasoningItem("fake", "m", "reasoning", {"encrypted_content": "e"})
    messages = stored(user("a"), Message("assistant", (TextPart("b"),), reasoning=(item,)), user("c"),
                      assistant("d"), user("e"))
    items = compress.assemble(messages, compress.Boundaries("S", 3, 4, 4), 10_000)
    assert items[1].message.reasoning == () and items[1].message.text == "b"


def test_cutting_a_message_never_lengthens_a_short_part():
    message = Message("user", (TextPart("x" * 40_000), TextPart("0123456789")))
    cut = compress.cut_message(message, 1_000)
    assert "OUTPUT TRUNCATED" in cut.parts[0].text and cut.parts[1] == TextPart("0123456789")


def test_a_newest_round_over_the_tail_budget_is_cut_in_the_request_only():
    messages = stored(user("a"), assistant("b"), user("c"), assistant("", call("c1")), result("c1", "w" * 80_000))
    items = compress.assemble(messages, compress.Boundaries(pruned_id=4), 10_000)
    assert "OUTPUT TRUNCATED" in items[-1].message.text
    assert compress.message_tokens(items[-1].message) < 10_100
    assert compress.assemble(messages, compress.Boundaries(), 10_000)[-1].message == messages[-1].message


def test_the_estimate_is_anchored_on_the_last_reported_count():
    messages = stored(user("abcd" * 10), assistant("abcd" * 10), user("abcd" * 100))
    items = compress.assemble(messages, compress.Boundaries(), 10_000)
    assert compress.estimate(items, 50) == 50 + 14 + 14 + 104
    assert compress.estimate(items, 50, anchor_tokens=1_000, anchor_id=2) == 1_000 + 104
    assert compress.estimate(items, 50, anchor_tokens=1_000, anchor_id=99) == 50 + 14 + 14 + 104


def test_boundaries_come_from_the_session():
    from keel.store.base import Session

    session = Session("s1", "p1", None, None, 0.0, 0.0, 0, summary="S", summary_head_id=3, summary_tail_id=9,
                      pruned_id=12)
    assert compress.Boundaries.of(session) == compress.Boundaries("S", 3, 9, 12)
    assert compress.Boundaries.of(None) == compress.Boundaries() and not compress.Boundaries().active
    assert compress.Boundaries(pruned_id=1).active
