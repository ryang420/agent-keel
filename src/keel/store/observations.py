"""Lesson observation rows (procedural learning design §4, §6): written by a memory review's commit, by feedback and
by skill_manage; read by the dream (plan ⑦) and the tests. Every query is scoped by profile. Observation times use
the store's content clock, the same as the skill and version times they are compared with."""

from __future__ import annotations

import json
import uuid

from keel.store.base import Observation

_COLUMNS = ("id, session_id, topic, situation, approach, outcome, evidence, loaded, state, review_job_id, run_id, "
            "consumed_by, created_at, note")


def _observation(row: dict) -> Observation:
    return Observation(row["id"], row["session_id"], row["topic"], row["situation"], row["approach"], row["outcome"],
                       tuple(json.loads(row["evidence"])),
                       tuple((item["skill_id"], item["version"]) for item in json.loads(row["loaded"])),
                       row["state"], row["review_job_id"], row["run_id"], row["consumed_by"], row["created_at"],
                       row["note"])


class ObservationStoreMixin:
    async def observations(self, profile_id: str, *, states: tuple[str, ...] | None = None,
                           limit: int = 100) -> list[Observation]:
        sql = f"SELECT {_COLUMNS} FROM lesson_observations WHERE profile_id = ?"
        args: list = [profile_id]
        if states is not None:
            sql += (" AND state IN (" + ", ".join("?" for _ in states) + ")") if states else " AND FALSE"
            args += list(states)
        rows = await self.db.fetch(sql + " ORDER BY created_at, id LIMIT ?", *args, limit)
        return [_observation(row) for row in rows]

    async def review_message_runs(self, profile_id: str, session_id: str, message_ids: set[int]) -> dict[int, bool]:
        """For each message: did its run complete without reaching the iteration cap?"""
        if not message_ids:
            return {}
        rows = await self.db.fetch(
            "SELECT m.id, r.status, r.capped FROM messages m JOIN sessions s ON s.id = m.session_id "
            "LEFT JOIN runs r ON r.id = m.run_id WHERE s.profile_id = ? AND m.session_id = ? "
            "AND m.id BETWEEN ? AND ?", profile_id, session_id, min(message_ids), max(message_ids))
        return {row["id"]: row["status"] == "completed" and not row["capped"]
                for row in rows if row["id"] in message_ids}

    async def requested_message_ids(self, profile_id: str, session_id: str) -> set[int]:
        rows = await self.db.fetch(
            "SELECT evidence FROM lesson_observations WHERE profile_id = ? AND session_id = ? AND outcome = 'requested'",
            profile_id, session_id)
        return {item for row in rows for item in json.loads(row["evidence"])}

    async def review_observation_count(self, profile_id: str, job_id: str) -> int:
        row = await self.db.fetchrow(
            "SELECT COUNT(*) AS n FROM lesson_observations WHERE profile_id = ? AND review_job_id = ?",
            profile_id, job_id)
        return row["n"]

    async def _loaded_for(self, profile_id: str, session_id: str, evidence: tuple[int, ...]) -> list[dict]:
        """The learned skills the cited messages' runs loaded: from skill_loads, never from the model."""
        if not evidence:
            return []
        rows = await self.db.fetch(
            "SELECT DISTINCT l.skill_id, l.version FROM skill_loads l JOIN messages m ON m.run_id = l.run_id "
            "WHERE l.profile_id = ? AND m.session_id = ? AND m.id BETWEEN ? AND ? AND m.id IN ("
            + ", ".join("?" for _ in evidence) + ") ORDER BY l.skill_id, l.version",
            profile_id, session_id, min(evidence), max(evidence), *evidence)
        return [{"skill_id": row["skill_id"], "version": row["version"]} for row in rows]

    async def _insert_observation(self, profile_id: str, session_id: str, generation: str, item, *, now: float,
                                  review_job_id: str | None = None, run_id: str | None = None,
                                  state: str = "pending", consumed_by: str | None = None) -> str:
        observation = uuid.uuid4().hex
        loaded = await self._loaded_for(profile_id, session_id, item.evidence)
        await self.db.execute(
            "INSERT INTO lesson_observations (id, profile_id, session_id, generation, topic, situation, approach, "
            "outcome, evidence, loaded, state, consumed_by, review_job_id, run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            observation, profile_id, session_id, generation, item.topic, item.situation, item.approach, item.outcome,
            json.dumps(list(item.evidence)), json.dumps(loaded), state, consumed_by, review_job_id, run_id, now)
        return observation

    async def expire_observations(self, before: float) -> int:
        rows = await self.db.fetch(
            "UPDATE lesson_observations SET state = 'expired' WHERE state = 'pending' AND created_at < ? "
            "RETURNING id", before)
        return len(rows)
