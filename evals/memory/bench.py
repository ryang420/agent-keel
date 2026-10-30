"""Lexical search benchmark (cross-session memory design §7): 100,000 messages in one profile plus other profiles,
bulk-inserted unindexed and indexed by the real backfill. Reports index bytes, backfill throughput, append overhead
and p50/p95 latency of session_search (excluding model calls) against the 500 ms p95 target.

Text model: Latin words and Han characters are drawn with Zipf-like weights (rank r has weight 1/r), so a few terms
are very common and posting lists are realistic; query terms come from the same weighted distribution. Queries are
classed "latin", "han" or "absent" (a word that is never in the data); the target is met only when every class's p95
is under 500 ms, latin and han queries mostly hit, and absent queries never do. Search runs warm-cache (the data was
just written) after ANALYZE of the search tables."""

from __future__ import annotations

import json
import os
import platform
import random
import statistics
import time

from keel.models import Message, TextPart, part_to_dict
from keel.tools import ToolContext, run_tool

TARGET_P95_MS = 500
BACKFILL_BATCH = 500
CLASSES = ("latin", "han", "absent")
_HAN = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面"
        "而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性"
        "好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第"
        "向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管")
_LATIN = [f"{a}{b}" for a in ("al", "be", "co", "da", "en", "fi", "go", "ha", "in", "jo", "ka", "lu", "mo", "ne",
                              "or", "pa", "qu", "ri", "so", "tu")
          for b in ("ron", "lex", "mint", "dar", "vis", "tor", "nel", "ped", "sik", "vo", "gan", "zul", "ber",
                    "cam", "fen", "hol", "jit", "kor", "lam", "mur")]


_LATIN_WEIGHTS = [1 / r for r in range(1, len(_LATIN) + 1)]
_HAN_WEIGHTS = [1 / r for r in range(1, len(_HAN) + 1)]


def _latin(rng: random.Random, count: int) -> list[str]:
    return rng.choices(_LATIN, weights=_LATIN_WEIGHTS, k=count)


def _han(rng: random.Random, count: int) -> str:
    return "".join(rng.choices(_HAN, weights=_HAN_WEIGHTS, k=count))


def synthetic_text(rng: random.Random) -> str:
    """A message of 5–60 words: about 30% Chinese, the rest from a 400-word Latin vocabulary."""
    if rng.random() < 0.3:
        return _han(rng, rng.randint(8, 80))
    return " ".join(_latin(rng, rng.randint(5, 60)))


def _query(rng: random.Random, index: int) -> tuple[str, str]:
    """(class, query); classes cycle 5:3:2 so every class is present even in a small run."""
    slot = index % 10
    if slot < 5:
        return "latin", " ".join(_latin(rng, rng.randint(1, 3)))
    if slot < 8:
        return "han", _han(rng, rng.randint(2, 4))
    return "absent", f"absentword{rng.randint(0, 10**6)}"


async def _profile(store, profile_id: str) -> None:
    await store.upsert_profile(profile_id, models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="",
                               skills=[], memory=True)


async def _bulk(store, profile_id: str, count: int, sessions: int, rng: random.Random) -> None:
    """Insert `count` messages without indexing them, in batches, across `sessions` sessions."""
    names = [f"{profile_id}-s{i}" for i in range(sessions)]
    for name in names:
        await store.ensure_session(profile_id, name)
    now = time.time() - count
    for start in range(0, count, 1000):
        batch = range(start, min(count, start + 1000))
        session_ids = [names[i % sessions] for i in batch]
        roles = ["user" if i % 2 == 0 else "assistant" for i in batch]
        parts = [json.dumps([part_to_dict(TextPart(synthetic_text(rng)))], ensure_ascii=False) for _ in batch]
        times = [now + i for i in batch]
        await store.db.execute(
            "INSERT INTO messages (session_id, run_id, role, parts, tool_calls, tool_call_id, tool_name, reasoning, "
            "active, created_at) SELECT s, NULL, r, p, '[]', NULL, NULL, '[]', 1, t "
            "FROM unnest(?::text[], ?::text[], ?::text[], ?::double precision[]) AS x(s, r, p, t)",
            session_ids, roles, parts, times)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def _stats(values: list[float]) -> dict:
    return {"mean": statistics.mean(values), "median": statistics.median(values), "p95": _percentile(values, 0.95)}


async def _search(ctx: ToolContext, query: str) -> tuple[float, bool]:
    started = time.perf_counter()
    outcome = await run_tool("session_search", json.dumps({"query": query}, ensure_ascii=False), ctx)
    elapsed = (time.perf_counter() - started) * 1000
    assert not outcome.error, outcome.content  # a failing search invalidates the benchmark
    return elapsed, bool(json.loads(outcome.content)["hits"])


async def bench(store, *, messages: int = 100_000, others: int = 20_000, sessions: int = 500, queries: int = 200,
                seed: int = 7) -> dict:
    rng = random.Random(seed)
    await _profile(store, "bench")
    await _bulk(store, "bench", messages, sessions, rng)
    for i in range(2):
        await _profile(store, f"other-{i}")
        await _bulk(store, f"other-{i}", others // 2, max(1, sessions // 10), rng)
    await store.db.execute(
        "INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
        "SELECT s.profile_id, 0, MAX(m.id), 0 FROM messages m JOIN sessions s ON s.id = m.session_id "
        "GROUP BY s.profile_id ON CONFLICT (profile_id) DO UPDATE SET done_through = 0, ceiling = excluded.ceiling")
    started = time.perf_counter()
    while await store.backfill_history(BACKFILL_BATCH) is not None:
        pass
    backfill_s = time.perf_counter() - started
    size = await store.db.fetchrow("SELECT pg_total_relation_size('message_search') "
                                   "+ pg_total_relation_size('message_tokens') AS bytes")
    timings = {"indexed": [], "raw": []}

    async def indexed(text: str) -> None:
        started = time.perf_counter()
        await store.append_message("bench-s0", None, Message("user", (TextPart(text),)))
        timings["indexed"].append((time.perf_counter() - started) * 1000)

    async def raw(text: str) -> None:
        started = time.perf_counter()
        await store.db.execute(
            "INSERT INTO messages (session_id, run_id, role, parts, tool_calls, tool_call_id, tool_name, reasoning, "
            "active, created_at) VALUES ('bench-s1', NULL, 'user', ?, '[]', NULL, NULL, '[]', 1, ?)",
            json.dumps([part_to_dict(TextPart(text))], ensure_ascii=False), time.time())
        timings["raw"].append((time.perf_counter() - started) * 1000)

    for i in range(min(1000, max(10, messages // 100))):
        text = synthetic_text(rng)
        for step in ((indexed, raw) if i % 2 == 0 else (raw, indexed)):  # alternate order to cancel cache effects
            await step(text)
    # VACUUM cannot run in a transaction (nor in a multi-statement string); executescript sends one statement on a
    # pooled connection outside a transaction, so VACUUM ANALYZE works.
    for table in ("message_search", "message_tokens"):
        await store.db.executescript(f"VACUUM ANALYZE {table}")
    profile = await store.get_profile("bench")
    await store.ensure_session("bench", "bench-now")
    ctx = ToolContext(store=store, profile_id="bench", profile_incarnation=profile.incarnation,
                      session_id="bench-now", allowed=frozenset({"session_search"}))
    warm = random.Random(seed + 1)
    for index in range(10):  # one untimed warm-up per class
        if index % 10 in (0, 5, 8):
            await _search(ctx, _query(warm, index)[1])
    samples: dict[str, list[tuple[float, bool]]] = {name: [] for name in CLASSES}
    for index in range(queries):
        kind, query = _query(rng, index)
        samples[kind].append(await _search(ctx, query))
    per_class = {}
    for kind, rows in samples.items():
        times = [t for t, _ in rows]
        per_class[kind] = ({"count": 0, "hit_rate": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0} if not rows else
                           {"count": len(rows), "hit_rate": sum(h for _, h in rows) / len(rows),
                            "p50": _percentile(times, 0.5), "p95": _percentile(times, 0.95), "max": max(times)})
    latencies = [t for rows in samples.values() for t, _ in rows]
    settings = {}
    for name in ("shared_buffers", "work_mem", "server_version"):
        settings[name] = (await store.db.fetchrow(f"SELECT current_setting('{name}') AS v"))["v"]
    mean_indexed, mean_raw = statistics.mean(timings["indexed"]), statistics.mean(timings["raw"])
    p95 = _percentile(latencies, 0.95)
    met = (all(per_class[k]["count"] > 0 and per_class[k]["p95"] < TARGET_P95_MS for k in CLASSES)
           and per_class["latin"]["hit_rate"] > 0.5 and per_class["han"]["hit_rate"] > 0.5
           and per_class["absent"]["hit_rate"] == 0)
    return {"messages": messages, "others": others, "index_bytes": int(size["bytes"]),
            "backfill_batch": BACKFILL_BATCH,
            "backfill_messages_per_s": (messages + others) / backfill_s if backfill_s else 0.0,
            "append_ms": {"indexed": mean_indexed, "raw": mean_raw, "overhead": mean_indexed - mean_raw},
            "append_stats": {"indexed": _stats(timings["indexed"]), "raw": _stats(timings["raw"])},
            "search_ms": {"p50": _percentile(latencies, 0.5), "p95": p95, "max": max(latencies),
                          "count": len(latencies)},
            "search_by_class": per_class,
            "target_p95_ms": TARGET_P95_MS, "met": met,
            "machine": {"platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count(),
                        "python": platform.python_version(), "postgres": settings["server_version"],
                        "shared_buffers": settings["shared_buffers"], "work_mem": settings["work_mem"]}}
