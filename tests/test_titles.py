from itertools import islice
import time

import pytest

from keel.titles import parse_model_title, title_candidates


def test_candidates_keep_going_past_nine_and_honor_the_limit():
    titles = list(islice(title_candidates("word " * 30, limit=48), 12))
    assert len(titles) == 12
    assert all(len(title) <= 48 for title in titles)
    assert titles[10].endswith("#11")
    wide = next(islice(title_candidates("exact", limit=100), 1, 2))
    assert wide == "exact #2"


@pytest.mark.parametrize("text,expected", [
    ('{"title": "Fix the login timeout"}', "Fix the login timeout"),
    ('Sure.\n{"title": "Retry the migration"}', "Retry the migration"),
    ("not json", None),
    ('{"title": ""}', None),
    ('{"title": "' + " ".join(["word"] * 13) + '"}', None),
    ('{"title": "Fix login button on mobile"}', None),
    ('{"title": "Code changes"}', None),
    ('{"title": "Friendly greeting"}', None),
])
def test_parse_model_title_keeps_only_a_specific_short_title(text, expected):
    assert parse_model_title(text) == expected


def test_the_title_object_pattern_is_linear_on_long_input():
    blobs = ["{" * 200_000, "}" * 200_000, '{"title": "' + "a" * 200_000, "word " * 50_000]
    for text in blobs:
        started = time.perf_counter()
        parse_model_title(text)
        assert time.perf_counter() - started < 1.0
