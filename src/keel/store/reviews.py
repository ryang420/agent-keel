"""The durable memory-review queue (cross-session memory design §4): per-session review state written with a run's
completion, jobs that claim an immutable source range, leases on database time and an atomic commit."""

from __future__ import annotations

import json
import uuid

from keel.observations import MAX_PER_JOB
from keel.review_input import SourceRow
from keel.store.base import (MemorySource, ProfileDeletionBusy, ReviewClaim, ReviewJob, ReviewNotRetryable,
                             ReviewSourceGone)

REVIEW_ATTEMPTS = 3
RETRY_BASE_S = 30.0
USAGE_KEPT = 20
REVIEW_STATUSES = ("pending", "running", "done", "failed", "discarded")
_OPEN = "('pending', 'running', 'failed')"
_ERROR_CHARS = 500


def _job(row: dict) -> ReviewJob:
    return ReviewJob(row["id"], row["profile_id"], row["session_id"], row["status"], row["provider"], row["model"],
                     row["source_from"], row["source_through"], row["cursor_message"], row["cursor_offset"],
                     row["attempts"], row["error_code"], row["error"], json.loads(row["usage"]), row["created_at"],
                     row["updated_at"], row["finished_at"])


def _claim(row: dict) -> ReviewClaim:
    return ReviewClaim(_job(row), row["lease_token"], row["generation"], row["profile_incarnation"])


def _usage(row: dict, entry: dict | None) -> str:
    kept = json.loads(row["usage"])
    if entry is not None:
        kept.append(entry)
    return json.dumps(kept[-USAGE_KEPT:])


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
        starts again from the next completed run. A running job is discarded too: its lease is cleared, so its
        worker's later writes are refused and lease expiry cannot revive it."""
        await self.db.execute("DELETE FROM memory_reviews WHERE profile_id = ?", profile_id)
        await self.db.execute(
            "DELETE FROM memory_review_jobs WHERE profile_id = ? AND status IN ('pending', 'failed')", profile_id)
        await self.db.execute(
            "UPDATE memory_review_jobs SET status = 'discarded', error_code = 'restarted', "
            "error = 'Memory was turned off and on while the review ran.', lease_owner = NULL, lease_token = NULL, "
            "lease_expires_at = NULL, updated_at = ?, finished_at = CAST(? AS DOUBLE PRECISION) "
            "WHERE profile_id = ? AND status = 'running'", self.clock(), self.clock(), profile_id)

    async def claim_review(self, owner: str, lease_seconds: float, *, turns: int, idle_seconds: float,
                           max_age_seconds: float) -> ReviewClaim | None:
        """Expire lapsed leases, turn eligible ranges into jobs, then lease the oldest due job of a profile that has
        no review running."""
        await self._expire_review_leases()
        await self._queue_reviews(turns=turns, idle_seconds=idle_seconds, max_age_seconds=max_age_seconds)
        now = await self.coordination_now()
        candidates = await self.db.fetch(
            "SELECT j.id, j.profile_id FROM memory_review_jobs j JOIN profiles p ON p.id = j.profile_id "
            "WHERE j.status = 'pending' AND j.next_attempt_at <= ? AND p.memory = 1 "
            "AND p.incarnation = j.profile_incarnation "
            "AND NOT EXISTS (SELECT 1 FROM memory_review_jobs r WHERE r.profile_id = j.profile_id "
            "AND r.status = 'running') "
            "AND NOT EXISTS (SELECT 1 FROM profile_deletions d WHERE d.profile_id = j.profile_id) "
            "ORDER BY j.next_attempt_at, j.created_at, j.id LIMIT 5", now)
        for candidate in candidates:
            claimed = await self._lease_review(candidate["id"], candidate["profile_id"], owner, lease_seconds)
            if claimed is not None:
                return claimed
        return None

    async def _expire_review_leases(self) -> None:
        async with self.db.transaction():
            now = await self.coordination_now()
            await self.db.execute(
                "UPDATE memory_review_jobs SET "
                "status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END, "
                "finished_at = CASE WHEN attempts >= ? THEN CAST(? AS DOUBLE PRECISION) ELSE NULL END, "
                "error_code = 'lease_expired', error = 'The review worker stopped renewing its lease.', "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, next_attempt_at = ?, "
                "updated_at = ? WHERE status = 'running' AND lease_expires_at < ?",
                REVIEW_ATTEMPTS, REVIEW_ATTEMPTS, self.clock(), now, self.clock(), now)

    async def _queue_reviews(self, *, turns: int, idle_seconds: float, max_age_seconds: float,
                             batch: int = 20) -> None:
        now = await self.coordination_now()
        rows = await self.db.fetch(
            "SELECT r.profile_id, r.session_id FROM memory_reviews r JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.memory = 1 AND p.incarnation = r.profile_incarnation AND r.dirty_through > r.queued_through "
            "AND (r.pending_turns >= ? OR r.last_completed_at <= ? OR r.first_dirty_at <= ?) "
            "AND NOT EXISTS (SELECT 1 FROM memory_review_jobs j WHERE j.profile_id = r.profile_id "
            f"AND j.session_id = r.session_id AND j.status IN {_OPEN}) "
            "AND NOT EXISTS (SELECT 1 FROM profile_deletions d WHERE d.profile_id = r.profile_id) "
            "ORDER BY r.last_completed_at LIMIT ?",
            turns, now - idle_seconds, now - max_age_seconds, batch)
        for row in rows:
            await self._queue_review(row["profile_id"], row["session_id"], turns=turns, idle_seconds=idle_seconds,
                                     max_age_seconds=max_age_seconds)

    async def _queue_review(self, profile_id: str, session_id: str, *, turns: int, idle_seconds: float,
                            max_age_seconds: float) -> None:
        """Turn one session's eligible range into a job under the profile lock (taken before any row lock)."""
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            now = await self.coordination_now()
            state = await self.db.fetchrow(
                "SELECT r.* FROM memory_reviews r JOIN profiles p ON p.id = r.profile_id "
                "WHERE r.profile_id = ? AND r.session_id = ? AND p.memory = 1 "
                "AND p.incarnation = r.profile_incarnation AND r.dirty_through > r.queued_through "
                "AND NOT EXISTS (SELECT 1 FROM profile_deletions d WHERE d.profile_id = r.profile_id) "
                "FOR UPDATE OF r", profile_id, session_id)
            if state is None:
                return
            due = [state["last_completed_at"] + idle_seconds]
            if state["first_dirty_at"] is not None:
                due.append(state["first_dirty_at"] + max_age_seconds)
            if state["pending_turns"] >= turns:
                due.append(state["last_completed_at"])
            if min(due) > now:
                return
            if await self.db.fetchrow(f"SELECT 1 AS open FROM memory_review_jobs WHERE profile_id = ? "
                                      f"AND session_id = ? AND status IN {_OPEN}", profile_id, session_id):
                return
            stamp = self.clock()
            await self.db.execute(
                "INSERT INTO memory_review_jobs (id, profile_id, session_id, generation, profile_incarnation, status, "
                "provider, model, source_from, source_through, cursor_message, next_attempt_at, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
                uuid.uuid4().hex, profile_id, session_id, state["generation"], state["profile_incarnation"],
                state["provider"], state["model"], state["queued_through"], state["dirty_through"],
                state["queued_through"], min(due), stamp, stamp)
            await self.db.execute(
                "UPDATE memory_reviews SET queued_through = dirty_through, pending_turns = 0, first_dirty_at = NULL "
                "WHERE profile_id = ? AND session_id = ?", profile_id, session_id)

    async def _lease_review(self, job_id: str, profile_id: str, owner: str,
                            lease_seconds: float) -> ReviewClaim | None:
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            now = await self.coordination_now()
            if await self.db.fetchrow("SELECT 1 AS busy FROM memory_review_jobs WHERE profile_id = ? "
                                      "AND status = 'running'", profile_id):
                return None
            row = await self.db.fetchrow(
                "UPDATE memory_review_jobs SET status = 'running', attempts = attempts + 1, lease_owner = ?, "
                "lease_token = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending' AND next_attempt_at <= ? RETURNING *",
                owner, uuid.uuid4().hex, now + lease_seconds, self.clock(), job_id, now)
        return None if row is None else _claim(row)

    async def renew_review(self, claim: ReviewClaim, lease_seconds: float) -> bool:
        now = await self.coordination_now()
        row = await self.db.fetchrow(
            "UPDATE memory_review_jobs SET lease_expires_at = ? WHERE id = ? AND lease_token = ? "
            "AND status = 'running' AND lease_expires_at >= ? RETURNING id",
            now + lease_seconds, claim.job.id, claim.token, now)
        return row is not None

    async def _owned_review(self, claim: ReviewClaim) -> dict | None:
        """The job row, locked, while this claim's lease is current; inside the caller's transaction."""
        now = await self.coordination_now()
        return await self.db.fetchrow(
            "SELECT * FROM memory_review_jobs WHERE id = ? AND lease_token = ? AND status = 'running' "
            "AND lease_expires_at >= ? FOR UPDATE", claim.job.id, claim.token, now)

    async def _settle_review(self, row: dict, status: str, *, code: str | None = None, message: str | None = None,
                             usage: dict | None = None, attempts: int | None = None,
                             next_attempt_at: float | None = None, cursor: tuple[int, int] | None = None) -> None:
        """End the current attempt: clear the lease, record the outcome and the attempt's usage."""
        finished = self.clock() if status in ("done", "failed", "discarded") else None
        cursor_message, cursor_offset = cursor or (row["cursor_message"], row["cursor_offset"])
        await self.db.execute(
            "UPDATE memory_review_jobs SET status = ?, error_code = ?, error = ?, usage = ?, attempts = ?, "
            "next_attempt_at = ?, cursor_message = ?, cursor_offset = ?, lease_owner = NULL, lease_token = NULL, "
            "lease_expires_at = NULL, updated_at = ?, finished_at = CAST(? AS DOUBLE PRECISION) WHERE id = ?",
            status, code, None if message is None else message[:_ERROR_CHARS], _usage(row, usage),
            row["attempts"] if attempts is None else attempts,
            row["next_attempt_at"] if next_attempt_at is None else next_attempt_at,
            cursor_message, cursor_offset, self.clock(), finished, row["id"])

    async def _retry_or_fail(self, row: dict, code: str, message: str, *, retry: bool,
                             usage: dict | None) -> str:
        final = not retry or row["attempts"] >= REVIEW_ATTEMPTS
        now = await self.coordination_now()
        status = "failed" if final else "pending"
        await self._settle_review(row, status, code=code, message=message, usage=usage,
                                  next_attempt_at=now + RETRY_BASE_S * 2 ** (row["attempts"] - 1))
        return status

    async def fail_review(self, claim: ReviewClaim, code: str, message: str, *, retry: bool,
                          usage: dict | None = None) -> str | None:
        async with self.db.transaction():
            await self.lock_profile(claim.job.profile_id)
            row = await self._owned_review(claim)
            if row is None:
                return None
            return await self._retry_or_fail(row, code, message, retry=retry, usage=usage)

    async def release_review(self, claim: ReviewClaim, *, usage: dict | None = None) -> None:
        """Give the job back unfinished (shutdown): the attempt does not count."""
        async with self.db.transaction():
            await self.lock_profile(claim.job.profile_id)
            row = await self._owned_review(claim)
            if row is not None:
                await self._settle_review(row, "pending", code=row["error_code"], message=row["error"],
                                          usage=usage, attempts=max(0, row["attempts"] - 1))

    async def discard_review(self, claim: ReviewClaim, code: str, *, usage: dict | None = None) -> None:
        async with self.db.transaction():
            await self.lock_profile(claim.job.profile_id)
            row = await self._owned_review(claim)
            if row is not None:
                await self._settle_review(row, "discarded", code=code, message="The review's owner changed.",
                                          usage=usage)

    async def _review_gone(self, profile_id: str, session_id: str, generation: str,
                           incarnation: str) -> str | None:
        profile = await self.db.fetchrow("SELECT memory, incarnation FROM profiles WHERE id = ?", profile_id)
        if profile is None or profile["incarnation"] != incarnation:
            return "profile_gone"
        try:
            await self.assert_profile_admitted(profile_id)
        except ProfileDeletionBusy:
            return "profile_gone"
        if not profile["memory"]:
            return "memory_disabled"
        session = await self.db.fetchrow("SELECT generation FROM sessions WHERE id = ? AND profile_id = ?",
                                         session_id, profile_id)
        if session is None or session["generation"] != generation:
            return "session_gone"
        state = await self.db.fetchrow("SELECT 1 AS ok FROM memory_reviews WHERE profile_id = ? AND session_id = ? "
                                       "AND generation = ?", profile_id, session_id, generation)
        return None if state is not None else "restarted"

    async def review_owner_gone(self, claim: ReviewClaim) -> str | None:
        """Why this job may no longer write, or None. A read without the lock; the commit checks again under it."""
        return await self._review_gone(claim.job.profile_id, claim.job.session_id, claim.generation,
                                       claim.profile_incarnation)

    async def list_reviews(self, profile_id: str, *, status: str | None = None, limit: int = 20) -> list[ReviewJob]:
        if status is None:
            rows = await self.db.fetch("SELECT * FROM memory_review_jobs WHERE profile_id = ? "
                                       "ORDER BY created_at DESC, id LIMIT ?", profile_id, limit)
        else:
            rows = await self.db.fetch("SELECT * FROM memory_review_jobs WHERE profile_id = ? AND status = ? "
                                       "ORDER BY created_at DESC, id LIMIT ?", profile_id, status, limit)
        return [_job(row) for row in rows]

    async def retry_review(self, profile_id: str, job_id: str) -> ReviewJob | None:
        """Requeue a failed job with fresh attempts. A pending or running job is returned as it is, so a repeated
        retry starts nothing new; a finished one is refused."""
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            row = await self.db.fetchrow("SELECT * FROM memory_review_jobs WHERE id = ? AND profile_id = ? "
                                         "FOR UPDATE", job_id, profile_id)
            if row is None:
                return None
            if row["status"] in ("pending", "running"):
                return _job(row)
            if row["status"] != "failed":
                raise ReviewNotRetryable(job_id)
            gone = await self._review_gone(profile_id, row["session_id"], row["generation"],
                                           row["profile_incarnation"])
            if gone in ("profile_gone", "session_gone", "restarted"):
                raise ReviewSourceGone(job_id)
            updated = await self.db.fetchrow(
                "UPDATE memory_review_jobs SET status = 'pending', attempts = 0, error_code = NULL, error = NULL, "
                "next_attempt_at = ?, finished_at = NULL, updated_at = ? WHERE id = ? RETURNING *",
                await self.coordination_now(), self.clock(), job_id)
            return _job(updated)

    async def review_rows(self, claim: ReviewClaim, *, limit: int = 200) -> tuple[list[SourceRow], bool]:
        job = claim.job
        rows = await self.db.fetch(
            "SELECT message_id, role, text, created_at FROM message_search WHERE profile_id = ? AND session_id = ? "
            "AND message_id > ? AND message_id <= ? ORDER BY message_id LIMIT ?",
            job.profile_id, job.session_id, job.cursor_message, job.source_through, limit)
        found = [SourceRow(row["message_id"], row["role"], row["text"], row["created_at"]) for row in rows]
        return found, len(found) < limit

    async def review_context(self, claim: ReviewClaim, *, limit: int = 12) -> list[SourceRow]:
        job = claim.job
        rows = await self.db.fetch(
            "SELECT message_id, role, text, created_at FROM message_search WHERE profile_id = ? AND session_id = ? "
            "AND message_id <= ? ORDER BY message_id DESC LIMIT ?",
            job.profile_id, job.session_id, job.cursor_message, limit)
        return [SourceRow(row["message_id"], row["role"], row["text"], row["created_at"]) for row in rows]

    async def commit_review(self, claim: ReviewClaim, proposals: dict, *, end: tuple[int, int], done: bool,
                            evidence: int | None, usage: dict | None, observations: tuple = ()) -> str:
        """Apply a complete staged proposal, the job's progress and the session cursor atomically. Nothing is written
        unless this claim still holds its lease, its owner is current and every touched target is unchanged."""
        job = claim.job

        def entry(outcome: str) -> dict | None:  # a refused commit must not be recorded as a committed one
            return None if usage is None else {**usage, "outcome": outcome}

        async with self.db.transaction():
            await self.lock_profile(job.profile_id)
            row = await self._owned_review(claim)
            if row is None:
                return "lost"
            gone = await self._review_gone(job.profile_id, job.session_id, claim.generation,
                                           claim.profile_incarnation)
            if gone is not None:
                await self._settle_review(row, "discarded", code=gone, message="The review's owner changed.",
                                          usage=entry("discarded"))
                return "discarded"
            for target, (revision, _, _, _) in proposals.items():
                if await self._memory_revision(job.profile_id, target) != revision:
                    await self._retry_or_fail(row, "conflict", "Memory changed while the review ran.", retry=True,
                                              usage=entry("conflict"))
                    return "conflict"
            source = MemorySource("review", session_id=job.session_id, message_id=evidence)
            key = f"review:{job.id}:{row['cursor_message']}:{row['cursor_offset']}"
            for target, (_, before, after, changes) in proposals.items():
                await self._commit_memory(job.profile_id, target, before, after, changes, source, key)
            now = await self.coordination_now()
            if observations:  # staged lessons commit with the memory proposal, only while learning is on
                learning = await self.db.fetchrow("SELECT learning FROM profiles WHERE id = ?", job.profile_id)
                if learning is not None and learning["learning"]:
                    left = MAX_PER_JOB - await self.review_observation_count(job.profile_id, job.id)
                    for item in observations[:max(0, left)]:
                        await self._insert_observation(job.profile_id, job.session_id, claim.generation, item,
                                                       now=now, review_job_id=job.id)
            if done:
                await self._settle_review(row, "done", usage=usage, cursor=(job.source_through, 0))
                reviewed = job.source_through
            else:
                await self._settle_review(row, "pending", usage=usage, attempts=0, next_attempt_at=now,
                                          cursor=end)
                reviewed = end[0]
            await self.db.execute(
                "UPDATE memory_reviews SET reviewed_through = GREATEST(reviewed_through, ?) "
                "WHERE profile_id = ? AND session_id = ? AND generation = ?",
                reviewed, job.profile_id, job.session_id, claim.generation)
            return "committed"
