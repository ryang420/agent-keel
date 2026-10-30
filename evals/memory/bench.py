"""Lexical search benchmark (cross-session memory design §7): 100,000 messages in one profile plus other profiles,
bulk-inserted unindexed and indexed by the real backfill. Reports index bytes, backfill throughput, append overhead
and p50/p95 latency of session_search (excluding model calls) against the 500 ms p95 target."""

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
_HAN = ("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面"
        "而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性"
        "好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第"
        "向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管")
_LATIN = [f"{a}{b}" for a in ("al", "be", "co", "da", "en", "fi", "go", "ha", "in", "jo", "ka", "lu", "mo", "ne",
                              "or", "pa", "qu", "ri", "so", "tu")
          for b in ("ron", "lex", "mint", "dar", "vis", "tor", "nel", "ped", "sik", "vo", "gan", "zul", "ber",
                    "cam", "fen", "hol", "jit", "kor", "lam", "mur")]


def synthetic_text(rng: random.Random) -> str:
    """A message of 5–60 words: about 30% Chinese, the rest from a 400-word Latin vocabulary."""
    if rng.random() < 0.3:
        return "".join(rng.choice(_HAN) for _ in range(rng.randint(8, 80)))
    return " ".join(rng.choice(_LATIN) for _ in range(rng.randint(5, 60)))


def _query(rng: random.Random) -> str:
    kind = rng.random()
    if kind < 0.5:
        return " ".join(rng.choice(_LATIN) for _ in range(rng.randint(1, 3)))
    if kind < 0.8:
        return "".join(rng.choice(_HAN) for _ in range(rng.randint(2, 4)))
    return f"absentword{rng.randint(0, 10**6)}"


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
    while await store.backfill_history(500) is not None:
        pass
    backfill_s = time.perf_counter() - started
    size = await store.db.fetchrow("SELECT pg_total_relation_size('message_search') "
                                   "+ pg_total_relation_size('message_tokens') AS bytes")
    timings = {"indexed": [], "raw": []}
    for i in range(min(1000, max(10, messages // 100))):
        text = synthetic_text(rng)
        started = time.perf_counter()
        await store.append_message("bench-s0", None, Message("user", (TextPart(text),)))
        timings["indexed"].append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        await store.db.execute(
            "INSERT INTO messages (session_id, run_id, role, parts, tool_calls, tool_call_id, tool_name, reasoning, "
            "active, created_at) VALUES ('bench-s1', NULL, 'user', ?, '[]', NULL, NULL, '[]', 1, ?)",
            json.dumps([part_to_dict(TextPart(text))], ensure_ascii=False), time.time())
        timings["raw"].append((time.perf_counter() - started) * 1000)
    profile = await store.get_profile("bench")
    await store.ensure_session("bench", "bench-now")
    ctx = ToolContext(store=store, profile_id="bench", profile_incarnation=profile.incarnation,
                      session_id="bench-now", allowed=frozenset({"session_search"}))
    latencies = []
    for _ in range(queries):
        query = _query(rng)
        started = time.perf_counter()
        outcome = await run_tool("session_search", json.dumps({"query": query}, ensure_ascii=False), ctx)
        latencies.append((time.perf_counter() - started) * 1000)
        assert not outcome.error, outcome.content
    version = await store.db.fetchrow("SELECT current_setting('server_version') AS v")
    indexed, raw = statistics.mean(timings["indexed"]), statistics.mean(timings["raw"])
    p95 = _percentile(latencies, 0.95)
    return {"messages": messages, "others": others, "index_bytes": int(size["bytes"]),
            "backfill_messages_per_s": (messages + others) / backfill_s if backfill_s else 0.0,
            "append_ms": {"indexed": indexed, "raw": raw, "overhead": indexed - raw},
            "search_ms": {"p50": _percentile(latencies, 0.5), "p95": p95, "max": max(latencies),
                          "count": len(latencies)},
            "target_p95_ms": TARGET_P95_MS, "met": p95 < TARGET_P95_MS,
            "machine": {"platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count(),
                        "python": platform.python_version(), "postgres": version["v"]}}
