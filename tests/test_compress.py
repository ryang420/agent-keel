import json
import time

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
    assert compress.base_tokens("abcd", ({"a": 1},), "abcd" * 10) == 1 + 2 + 10
    assert compress.base_tokens("abcd", (), None) == 1


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


def test_messages_are_serialized_as_labelled_redacted_text():
    secret = "sk-" + "a" * 24
    text = compress.serialize([user(f"key {secret}"), assistant("ok", call("c1", "read_file", path="/w/a.py")),
                               result("c1", "short")])
    assert text == ('[USER]: key [REDACTED]\n\n[ASSISTANT]: ok\n[Tool calls:\n  read_file({"path": "/w/a.py"})\n]'
                    "\n\n[TOOL RESULT c1]: short")


def test_serialization_clips_long_messages_and_arguments_and_applies_placeholders():
    long_user = compress.serialize([user("m" * 7_000)])
    assert "...[truncated]..." in long_user and len(long_user) < 6_000
    long_args = compress.serialize([assistant("", call("c1", "write_file", path="/w/x", content="c" * 2_000))])
    assert long_args.endswith("...)\n]") and len(long_args) < 1_400
    assert compress.serialize([result("c1", "y" * 500)]) == (
        "[TOOL RESULT c1]: [terminal result omitted from context: 500 characters]")


def test_the_summary_input_is_capped_with_the_previous_summary_counted():
    middle = [user("t" * 6_000) for _ in range(200)]
    turns = compress.summary_turns(None, middle)
    assert "OUTPUT TRUNCATED" in turns and compress.text_tokens(turns) <= compress.SUMMARY_INPUT_TOKENS + 50
    with_previous = compress.summary_turns("p" * 40_000, middle)
    assert compress.text_tokens(with_previous) <= compress.SUMMARY_INPUT_TOKENS - 10_000 + 50
    assert compress.summary_turns(None, [user("short")]) == "[USER]: short"
    smaller = compress.summary_turns(None, middle, limit=20_000)
    assert compress.text_tokens(smaller) <= 20_000 + 50


def test_files_referenced_come_from_tool_arguments_and_absolute_paths():
    messages = [
        user("Here is /data/uploads/report-2026.csv. See https://example.com/a/b and a/b/c too."),
        assistant("I will look at /not/collected.txt",
                  call("c1", "write_file", path="out/summary.md", content="x"),
                  call("c2", "terminal", command="python /work/run.py --in /data/x.csv"),
                  call("c3", "execute_code", code="open('/tmp/y.txt')"),
                  call("c4", "read_file", path="/w/a.py")),
        assistant("", call("c5", "read_file", path="/w/a.py"), call("c6", "patch", path="/w/b.py")),
    ]
    assert compress.collect_files(messages, ["/prev/one.txt"]) == [
        "/prev/one.txt", "/data/uploads/report-2026.csv", "out/summary.md", "/work/run.py", "/data/x.csv",
        "/tmp/y.txt", "/w/a.py", "/w/b.py"]


def test_files_referenced_keep_the_newest_fifty():
    previous = [f"/p/{n}" for n in range(60)]
    assert compress.collect_files([], previous) == previous[-50:]


def test_a_file_named_again_moves_to_the_newest_end():
    previous = [f"/p/{n}" for n in range(50)]
    assert compress.collect_files([assistant("", call("c1", "read_file", path="/p/0"))], previous) == (
        previous[1:] + ["/p/0"])
    assert compress.collect_files([user("/p/1 then /p/2 then /p/1")]) == ["/p/2", "/p/1"]


def test_file_paths_with_a_newline_or_over_1024_characters_are_skipped_and_secrets_redacted():
    secret = "sk-" + "a" * 24
    messages = [assistant("", call("c1", "write_file", path="/w/a\nb.txt"),
                          call("c2", "write_file", path="/w/" + "l" * 1_100),
                          call("c3", "read_file", path=f"/w/{secret}.txt"),
                          call("c4", "read_file", path="/w/ok.txt"))]
    assert compress.collect_files(messages) == ["/w/[REDACTED].txt", "/w/ok.txt"]


def test_the_path_pattern_is_linear_on_long_input():
    for text in ("/" + "a" * 200_000, "/a" * 100_000, "/" * 200_000, " /a." * 50_000, "a/" * 100_000):
        started = time.perf_counter()
        compress.collect_files([user(text)])
        assert time.perf_counter() - started < 1.0


def test_the_files_section_is_appended_and_read_back():
    joined = compress.join_files("BODY", ["/a", "/b"])
    assert joined == "BODY\n\n## Files referenced\n- /a\n- /b"
    assert compress.split_files(joined) == ("BODY", ["/a", "/b"])
    assert compress.split_files("BODY") == ("BODY", [])
    assert compress.split_files(None) == (None, [])
    assert compress.join_files("BODY", []) == "BODY"


def test_the_summary_prompt_has_a_first_and_an_update_form():
    system, first = compress.summary_prompt(None, "TURNS", today="October 12, 2026")
    assert system == prompts.text("summary_system")
    assert "TURNS TO SUMMARIZE:\nTURNS" in first and "PREVIOUS SUMMARY" not in first
    assert "## Historical Task Snapshot" in first and "The current date is October 12, 2026." in first
    assert "Target ~2000 tokens." in first
    _, update = compress.summary_prompt("OLD", "NEW", today="October 12, 2026")
    assert "PREVIOUS SUMMARY:\nOLD" in update and "NEW TURNS TO INCORPORATE:\nNEW" in update
    assert "## Historical Task Snapshot" in update
