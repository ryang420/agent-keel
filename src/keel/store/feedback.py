"""Run feedback (procedural learning design §6): a rating on a finished run, the observation it becomes when learning
is on, 30-day outcome statistics per learned skill version, and automatic suspension after enough down ratings. Every
write takes the profile lock and checks admission and incarnation in one transaction; suspension needs no model."""

from __future__ import annotations

from keel.observations import FEEDBACK_TOPIC, STATS_SECONDS, SUSPEND_DOWN
from keel.review_observe import StagedObservation
from keel.store.base import TERMINAL, RunFeedback, SkillOutcome, SkillSource

_COLUMNS = "run_id, session_id, rating, comment, observation_id, created_at, updated_at"


def _feedback(row: dict) -> RunFeedback:
    return RunFeedback(row["run_id"], row["session_id"], row["rating"], row["comment"], row["observation_id"],
                       row["created_at"], row["updated_at"])


class FeedbackStoreMixin:
    async def get_feedback(self, profile_id: str, run_id: str) -> RunFeedback | None:
        row = await self.db.fetchrow(f"SELECT {_COLUMNS} FROM run_feedback WHERE profile_id = ? AND run_id = ?",
                                     profile_id, run_id)
        return _feedback(row) if row else None

    async def put_feedback(self, profile_id: str, run_id: str, rating: str, comment: str | None, *,
                           profile_incarnation: str | None = None
                           ) -> tuple[RunFeedback | None, str | None, list[str]]:
        """Store or replace the rating: (feedback, refusal code, ids of skills suspended by it)."""
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            await self.assert_profile_admitted(profile_id)
            await self._check_profile_incarnation(profile_id, profile_incarnation)
            run = await self.db.fetchrow("SELECT id, session_id, status FROM runs WHERE id = ? AND profile_id = ?",
                                         run_id, profile_id)
            if run is None:
                return None, "not_found", []
            if run["status"] not in TERMINAL:
                return None, "not_terminal", []
            now = await self.coordination_now()
            stamp = self.clock()
            previous = await self.db.fetchrow(
                "SELECT observation_id FROM run_feedback WHERE profile_id = ? AND run_id = ?", profile_id, run_id)
            if previous is not None:
                await self._drop_pending_observation(profile_id, previous["observation_id"])
            profile = await self.db.fetchrow("SELECT learning FROM profiles WHERE id = ?", profile_id)
            observation = None
            if profile["learning"]:
                observation = await self._feedback_observation(profile_id, run, rating, comment, now)
            await self.db.execute(
                "INSERT INTO run_feedback (profile_id, run_id, session_id, rating, comment, observation_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (profile_id, run_id) DO UPDATE "
                "SET rating = excluded.rating, comment = excluded.comment, "
                "observation_id = excluded.observation_id, updated_at = excluded.updated_at",
                profile_id, run_id, run["session_id"], rating, comment, observation, stamp, stamp)
            suspended = []
            if rating == "down" and profile["learning"]:
                suspended = await self._suspend_for(profile_id, run_id)
            return await self.get_feedback(profile_id, run_id), None, suspended

    async def delete_feedback(self, profile_id: str, run_id: str, *, profile_incarnation: str | None = None) -> bool:
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            await self.assert_profile_admitted(profile_id)
            await self._check_profile_incarnation(profile_id, profile_incarnation)
            row = await self.db.fetchrow(
                "DELETE FROM run_feedback WHERE profile_id = ? AND run_id = ? RETURNING observation_id",
                profile_id, run_id)
            if row is None:
                return False
            await self._drop_pending_observation(profile_id, row["observation_id"])
            return True

    async def _drop_pending_observation(self, profile_id: str, observation_id: str | None) -> None:
        if observation_id is not None:
            await self.db.execute(
                "DELETE FROM lesson_observations WHERE profile_id = ? AND id = ? AND state = 'pending'",
                profile_id, observation_id)

    async def _feedback_observation(self, profile_id: str, run, rating: str, comment: str | None,
                                    now: float) -> str:
        ends = await self.db.fetchrow(
            "SELECT MAX(id) FILTER (WHERE role = 'user') AS u, MAX(id) FILTER (WHERE role = 'assistant') AS a "
            "FROM messages WHERE session_id = ? AND run_id = ?", run["session_id"], run["id"])
        evidence = tuple(value for value in (ends["u"], ends["a"]) if value is not None)
        session = await self.db.fetchrow("SELECT generation FROM sessions WHERE id = ? AND profile_id = ?",
                                         run["session_id"], profile_id)
        item = StagedObservation(FEEDBACK_TOPIC, (comment or "")[:600], "", f"feedback_{rating}", evidence)
        return await self._insert_observation(profile_id, run["session_id"], session["generation"], item,
                                              run_id=run["id"], now=now)

    async def _suspend_for(self, profile_id: str, run_id: str) -> list[str]:
        """Suspend each active skill whose current version this run loaded and whose recent ratings say so."""
        loads = await self.db.fetch("SELECT skill_id, version FROM skill_loads WHERE profile_id = ? AND run_id = ? "
                                    "ORDER BY skill_id", profile_id, run_id)
        if not loads:
            return []
        outcomes = await self.skill_outcomes(profile_id, self.clock() - STATS_SECONDS)
        suspended = []
        for load in loads:
            skill_id, version = load["skill_id"], load["version"]
            outcome = outcomes.get((skill_id, version))
            skill = await self.db.fetchrow(
                "SELECT version, state FROM learned_skills WHERE profile_id = ? AND id = ?", profile_id, skill_id)
            if (outcome is None or skill is None or skill["version"] != version or skill["state"] != "active"
                    or outcome.down < SUSPEND_DOWN or outcome.down <= outcome.up):
                continue
            rationale = f"suspended automatically: {outcome.down} down and {outcome.up} up ratings in 30 days"
            result = await self._change_skill(
                profile_id, "suspend", {"skill_id": skill_id, "expected_version": version, "rationale": rationale},
                set(), SkillSource("automatic", run_id=run_id))
            if result["success"]:
                suspended.append(skill_id)
        return suspended

    async def skill_outcomes(self, profile_id: str, since: float) -> dict[tuple[str, int], SkillOutcome]:
        rows = await self.db.fetch(
            "SELECT l.skill_id, l.version, COUNT(*) AS loads, "
            "COUNT(*) FILTER (WHERE f.rating = 'up') AS up, COUNT(*) FILTER (WHERE f.rating = 'down') AS down, "
            "COUNT(*) FILTER (WHERE r.status = 'completed') AS completed, "
            "COUNT(*) FILTER (WHERE r.status = 'failed') AS failed, "
            "COUNT(*) FILTER (WHERE r.status = 'cancelled') AS cancelled "
            "FROM skill_loads l "
            "LEFT JOIN run_feedback f ON f.profile_id = l.profile_id AND f.run_id = l.run_id "
            "LEFT JOIN runs r ON r.id = l.run_id AND r.profile_id = l.profile_id "
            "WHERE l.profile_id = ? AND l.loaded_at >= ? GROUP BY l.skill_id, l.version", profile_id, since)
        return {(row["skill_id"], row["version"]): SkillOutcome(row["loads"], row["up"], row["down"],
                                                                row["completed"], row["failed"], row["cancelled"])
                for row in rows}
