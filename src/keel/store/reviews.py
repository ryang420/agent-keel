"""The durable memory-review queue (cross-session memory design §4): per-session review state written with a run's
completion, jobs that claim an immutable source range, leases on database time and an atomic commit."""

from __future__ import annotations


class ReviewStoreMixin:
    async def _mark_review_dirty(self, run_row: dict) -> None:
        """Inside finish_run's transaction: a completed run extends its session's unreviewed range. A session seen
        for the first time starts at this run's first message, so the older archive is never reviewed. Only a
        memory-enabled profile with an incarnation is marked."""
        now = await self.coordination_now()
        await self.db.execute(
            "INSERT INTO memory_reviews (profile_id, session_id, generation, profile_incarnation, reviewed_through, "
            "queued_through, dirty_through, pending_turns, first_dirty_at, last_completed_at, provider, model) "
            "SELECT s.profile_id, s.id, s.generation, p.incarnation, b.first - 1, b.first - 1, b.last, 1, ?, ?, ?, ? "
            "FROM sessions s JOIN profiles p ON p.id = s.profile_id CROSS JOIN "
            "(SELECT MIN(id) FILTER (WHERE run_id = ?) AS first, MAX(id) AS last FROM messages "
            "WHERE session_id = ?) b "
            "WHERE s.id = ? AND s.profile_id = ? AND p.memory = 1 AND p.incarnation IS NOT NULL "
            "AND b.first IS NOT NULL "
            "ON CONFLICT (profile_id, session_id) DO UPDATE SET "
            "dirty_through = GREATEST(memory_reviews.dirty_through, excluded.dirty_through), "
            "pending_turns = memory_reviews.pending_turns + 1, "
            "first_dirty_at = COALESCE(memory_reviews.first_dirty_at, excluded.first_dirty_at), "
            "last_completed_at = excluded.last_completed_at, provider = excluded.provider, model = excluded.model",
            now, now, run_row["provider"], run_row["model"], run_row["id"], run_row["session_id"],
            run_row["session_id"], run_row["profile_id"])

    async def _forget_session_reviews(self, profile_id: str, session_id: str) -> None:
        for table in ("memory_review_jobs", "memory_reviews"):
            await self.db.execute(f"DELETE FROM {table} WHERE profile_id = ? AND session_id = ?",
                                  profile_id, session_id)

    async def _restart_reviews(self, profile_id: str) -> None:
        """Memory was just turned on: forget unreviewed ranges and queued or failed jobs from before, so review
        starts again from the next completed run. A running job is left to its lease; its commit is discarded."""
        await self.db.execute("DELETE FROM memory_reviews WHERE profile_id = ?", profile_id)
        await self.db.execute(
            "DELETE FROM memory_review_jobs WHERE profile_id = ? AND status IN ('pending', 'failed')", profile_id)
