"""The search benchmark runs end to end at a small size and reports every measure the design asks for."""

from evals.memory.bench import bench, synthetic_text
import random


def test_synthetic_text_is_deterministic_and_mixes_languages():
    rng_a, rng_b = random.Random(7), random.Random(7)
    first = [synthetic_text(rng_a) for _ in range(50)]
    second = [synthetic_text(rng_b) for _ in range(50)]
    assert first == second
    assert any(any("一" <= ch <= "鿿" for ch in text) for text in first)
    assert any(text.isascii() for text in first)


async def test_a_small_benchmark_reports_every_measure(store):
    result = await bench(store, messages=400, others=100, sessions=8, queries=20)
    assert result["messages"] == 400 and result["others"] == 100
    assert result["index_bytes"] > 0 and result["backfill_messages_per_s"] > 0
    assert set(result["append_ms"]) == {"indexed", "raw", "overhead"}
    assert result["search_ms"]["count"] == 20 and result["search_ms"]["p95"] >= result["search_ms"]["p50"] > 0
    assert result["target_p95_ms"] == 500 and isinstance(result["met"], bool)
    assert {"postgres", "python", "cpus"} <= set(result["machine"])
    assert await store.history_index_complete("bench")
