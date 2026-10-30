import time

import pytest

from keel.history import (
    QueryError, excerpt, index_tokens, iso, normalize, parse_time, phrase, query_tokens, render, searchable_text,
)
from keel.models import ImagePart, Message, TextPart, ToolCall


def test_normalize_folds_case_and_width():
    assert normalize("ＡＢＣ Straße") == "abc strasse"


def test_latin_words_and_digits_are_tokens_and_punctuation_separates():
    assert index_tokens("Deploy v2.1 to prod-eu!") == {"deploy", "v2", "1", "to", "prod", "eu"}


def test_han_text_is_indexed_as_single_characters_and_bigrams():
    assert index_tokens("数据库迁移") == {"数", "据", "库", "迁", "移", "数据", "据库", "库迁", "迁移"}


def test_a_han_query_uses_bigrams_and_a_single_character_query_uses_the_character():
    assert query_tokens("数据库") == ["数据", "据库"]
    assert query_tokens("库") == ["库"]
    assert query_tokens("用 PostgreSQL 数据库") == ["用", "postgresql", "数据", "据库"]


def test_query_limits():
    with pytest.raises(QueryError):
        query_tokens("a" * 257)
    with pytest.raises(QueryError):
        query_tokens("!!! ???")
    with pytest.raises(QueryError):
        query_tokens(" ".join(f"w{i}" for i in range(33)))
    assert len(query_tokens(" ".join(f"w{i}" for i in range(32)))) == 32


def test_a_long_word_is_cut_to_64_characters():
    assert index_tokens("x" * 100) == {"x" * 64}


def test_only_user_and_assistant_text_is_searchable_and_it_is_redacted():
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    assert searchable_text(Message("user", (TextPart(f"key {secret}"), ImagePart("image/png", "", "b1")))) \
        == "key [REDACTED]"
    assert searchable_text(Message("tool", (TextPart("result"),), tool_call_id="c1", tool_name="terminal")) is None
    assert searchable_text(Message("assistant", (), tool_calls=(ToolCall("c1", "terminal", "{}"),))) is None


def test_render_labels_what_it_omits():
    running = Message("assistant", (TextPart("Running it."),), tool_calls=(ToolCall("c1", "terminal", "{}"),))
    assert render(running) \
        == "Running it.\n[called tool terminal; its arguments and result are omitted]"
    assert render(Message("tool", (TextPart("ok"),), tool_call_id="c1", tool_name="terminal")) \
        == "[result of tool terminal omitted]"
    assert render(Message("user", (ImagePart("image/png", "", "b1"),))) == "[image omitted]"


def test_excerpt_centres_on_the_first_match_and_reports_truncation():
    text = "x " * 400 + "the migration plan" + " y" * 400
    piece, truncated = excerpt(text, ["migration"], width=60)
    assert "migration" in piece and truncated and len(piece) == 60
    assert excerpt("short text", ["text"]) == ("short text", False)


def test_parse_time_requires_a_timezone():
    assert parse_time("2026-10-01T00:00:00+00:00", "after") == 1790812800.0
    with pytest.raises(QueryError):
        parse_time("2026-10-01T00:00:00", "after")
    with pytest.raises(QueryError):
        parse_time("yesterday", "after")
    assert iso(1790812800.0) == "2026-10-01T00:00:00+00:00"


def test_phrase_collapses_whitespace():
    assert phrase("  Data\n  Base ") == "data base"


def test_tokenizing_is_linear_on_long_input():
    started = time.perf_counter()
    index_tokens(("ab1-" * 50_000) + ("数据" * 50_000))
    assert time.perf_counter() - started < 2.0
