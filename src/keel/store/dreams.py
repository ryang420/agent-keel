"""Dream jobs (procedural learning design §5): profile-level consolidation jobs run by the memory review workers. A
profile with learning on becomes due from its pending observations; one lease per profile covers dreams and memory
reviews, attempts per profile per day are capped, leases run on database time and the commit is atomic. Content times
use the store's content clock; leases, retries and the daily window use database time."""

from __future__ import annotations

import json
import uuid

from keel.store.base import DreamBusy, DreamClaim, DreamJob, DreamNotRetryable, ProfileDeletionBusy
from keel.store.reviews import REVIEW_ATTEMPTS, RETRY_BASE_S, USAGE_KEPT

DREAM_STATUSES = ("pending", "running", "done", "failed", "discarded")
DAY_S = 86_400.0
_ERROR_CHARS = 500
_BATCH = 50  # profiles examined per queue pass


def _job(row: dict) -> DreamJob:
    return DreamJob(row["id"], row["profile_id"], row["status"], row["trigger"], row["provider"], row["model"],
                    row["attempts"], row["error_code"], row["error"], json.loads(row["usage"]),
                    json.loads(row["changes"]), row["created_at"], row["updated_at"], row["finished_at"])


def _claim(row: dict) -> DreamClaim:
    return DreamClaim(_job(row), row["lease_token"], row["profile_incarnation"], row["input_at"])


def _usage(row: dict, entry: dict | None) -> str:
    kept = json.loads(row["usage"])
    if entry is not None:
        kept.append(entry)
    return json.dumps(kept[-USAGE_KEPT:])


class DreamStoreMixin:
    async def claim_dream(self, owner: str, lease_seconds: float, rules) -> DreamClaim | None:
        """Expire lapsed leases, queue due profiles, then lease the oldest due dream of a profile with nothing of
        its own running and attempts left today."""
        await self._expire_dream_leases()
        await self._queue_dreams(rules)
        now = await self.coordination_now()
        candidates = await self.db.fetch(
            "SELECT j.id, j.profile_id FROM dream_jobs j JOIN profiles p ON p.id = j.profile_id "
            "WHERE j.status = 'pending' AND j.next_attempt_at <= ? AND p.memory = 1 AND p.learning = 1 "
            "AND p.incarnation = j.profile_incarnation "
            "AND NOT EXISTS (SELECT 1 FROM memory_review_jobs r WHERE r.profile_id = j.profile_id "
            "AND r.status = 'running') "
            "AND (SELECT COUNT(*) FROM dream_attempts a WHERE a.profile_id = j.profile_id AND a.started_at > ?) < ? "
            "AND NOT EXISTS (SELECT 1 FROM profile_deletions d WHERE d.profile_id = j.profile_id) "
            "ORDER BY j.next_attempt_at, j.created_at, j.id LIMIT 5", now, now - DAY_S, rules.daily_attempts)
        for candidate in candidates:
            claimed = await self._lease_dream(candidate["id"], candidate["profile_id"], owner, lease_seconds, rules)
            if claimed is not None:
                return claimed
        return None

    async def _expire_dream_leases(self) -> None:
        async with self.db.transaction():
            now = await self.coordination_now()
            await self.db.execute(
                "UPDATE dream_jobs SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END, "
                "finished_at = CASE WHEN attempts >= ? THEN CAST(? AS DOUBLE PRECISION) ELSE NULL END, "
                "error_code = 'lease_expired', error = 'The dream worker stopped renewing its lease.', "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, next_attempt_at = ?, "
                "updated_at = ? WHERE status = 'running' AND lease_expires_at < ?",
                REVIEW_ATTEMPTS, REVIEW_ATTEMPTS, self.clock(), now, self.clock(), now)

    async def _queue_dreams(self, rules) -> None:
        """Examine profiles with pending observations whose newest is past the debounce, oldest pending first."""
        rows = await self.db.fetch(
            "SELECT o.profile_id FROM lesson_observations o JOIN profiles p ON p.id = o.profile_id "
            "WHERE o.state = 'pending' AND p.memory = 1 AND p.learning = 1 AND p.incarnation IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM dream_jobs j WHERE j.profile_id = o.profile_id "
            "AND j.status IN ('pending', 'running')) "
            "AND NOT EXISTS (SELECT 1 FROM profile_deletions d WHERE d.profile_id = o.profile_id) "
            "GROUP BY o.profile_id HAVING MAX(o.created_at) <= ? ORDER BY MIN(o.created_at) LIMIT ?",
            self.clock() - rules.debounce_seconds, _BATCH)
        for row in rows:
            await self._queue_dream(row["profile_id"], rules)

    async def _queue_dream(self, profile_id: str, rules) -> None:
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            profile = await self.db.fetchrow(
                "SELECT incarnation FROM profiles WHERE id = ? AND memory = 1 AND learning = 1 "
                "AND incarnation IS NOT NULL AND NOT EXISTS (SELECT 1 FROM profile_deletions d "
                "WHERE d.profile_id = profiles.id)", profile_id)
            if profile is None or await self.db.fetchrow(
                    "SELECT 1 AS open FROM dream_jobs WHERE profile_id = ? AND status IN ('pending', 'running')",
                    profile_id):
                return
            trigger = await self._dream_trigger(profile_id, rules, self.clock())
            if trigger is None:
                return
            route = await self._dream_route(profile_id)
            stamp = self.clock()
            await self.db.execute(
                "INSERT INTO dream_jobs (id, profile_id, profile_incarnation, status, trigger, provider, model, "
                "next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                uuid.uuid4().hex, profile_id, profile["incarnation"], trigger, route[0], route[1],
                await self.coordination_now(), stamp, stamp)

    async def _dream_trigger(self, profile_id: str, rules, now: float) -> str | None:
        """Why the profile is due (§5), or None. Counts only evidence newer than the last completed dream's input,
        except the daily rule, which needs the oldest pending observation and the last dream both a day old."""
        pending = await self.db.fetchrow(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest, MAX(created_at) AS newest FROM lesson_observations "
            "WHERE profile_id = ? AND state = 'pending'", profile_id)
        if not pending["n"] or pending["newest"] > now - rules.debounce_seconds:
            return None
        last = await self.db.fetchrow("SELECT MAX(input_at) AS at FROM dream_jobs WHERE profile_id = ? "
                                      "AND status = 'done'", profile_id)
        since = last["at"] if last["at"] is not None else 0.0
        fresh = await self.db.fetchrow(
            "SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE outcome = 'feedback_down' AND loaded <> '[]') AS flagged "
            "FROM lesson_observations WHERE profile_id = ? AND state = 'pending' AND created_at > ?",
            profile_id, since)
        if fresh["flagged"]:
            return "feedback"
        if await self.db.fetchrow(
                "SELECT 1 AS hit FROM learned_skill_versions WHERE profile_id = ? AND change = 'suspend' "
                "AND source_kind = 'automatic' AND created_at > ? LIMIT 1", profile_id, since):
            return "suspension"
        if fresh["n"] >= rules.min_observations:
            return "observations"
        if pending["oldest"] <= now - rules.interval_seconds and since <= now - rules.interval_seconds:
            return "interval"
        return None

    async def _dream_route(self, profile_id: str) -> tuple[str | None, str | None]:
        """The model of the profile's most recently completed run; the dreamer revalidates it per attempt."""
        row = await self.db.fetchrow(
            "SELECT provider, model FROM runs WHERE profile_id = ? AND status = 'completed' "
            "ORDER BY finished_at DESC NULLS LAST, created_at DESC LIMIT 1", profile_id)
        return (row["provider"], row["model"]) if row else (None, None)

    async def _lease_dream(self, job_id: str, profile_id: str, owner: str, lease_seconds: float,
                           rules) -> DreamClaim | None:
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            now = await self.coordination_now()
            if await self.db.fetchrow("SELECT 1 AS busy FROM memory_review_jobs WHERE profile_id = ? "
                                      "AND status = 'running'", profile_id):
                return None
            started = await self.db.fetchrow("SELECT COUNT(*) AS n FROM dream_attempts WHERE profile_id = ? "
                                             "AND started_at > ?", profile_id, now - DAY_S)
            if started["n"] >= rules.daily_attempts:
                return None
            row = await self.db.fetchrow(
                "UPDATE dream_jobs SET status = 'running', attempts = attempts + 1, lease_owner = ?, "
                "lease_token = ?, lease_expires_at = ?, input_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending' AND next_attempt_at <= ? RETURNING *",
                owner, uuid.uuid4().hex, now + lease_seconds, self.clock(), self.clock(), job_id, now)
            if row is None:
                return None
            await self.db.execute("INSERT INTO dream_attempts (profile_id, job_id, started_at) VALUES (?, ?, ?)",
                                  profile_id, job_id, now)
        return _claim(row)

    async def renew_dream(self, claim: DreamClaim, lease_seconds: float) -> bool:
        now = await self.coordination_now()
        row = await self.db.fetchrow(
            "UPDATE dream_jobs SET lease_expires_at = ? WHERE id = ? AND lease_token = ? AND status = 'running' "
            "AND lease_expires_at >= ? RETURNING id", now + lease_seconds, claim.job.id, claim.token, now)
        return row is not None

    async def _owned_dream(self, claim: DreamClaim) -> dict | None:
        """The job row, locked, while this claim's lease is current; inside the caller's transaction."""
        now = await self.coordination_now()
        return await self.db.fetchrow(
            "SELECT * FROM dream_jobs WHERE id = ? AND lease_token = ? AND status = 'running' "
            "AND lease_expires_at >= ? FOR UPDATE", claim.job.id, claim.token, now)

    async def _settle_dream(self, row: dict, status: str, *, code: str | None = None, message: str | None = None,
                            usage: dict | None = None, attempts: int | None = None,
                            next_attempt_at: float | None = None, changes: dict | None = None) -> None:
        """End the current attempt: clear the lease, record the outcome, the attempt's usage and any changes."""
        finished = self.clock() if status in ("done", "failed", "discarded") else None
        await self.db.execute(
            "UPDATE dream_jobs SET status = ?, error_code = ?, error = ?, usage = ?, attempts = ?, "
            "next_attempt_at = ?, changes = ?, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
            "updated_at = ?, finished_at = CAST(? AS DOUBLE PRECISION) WHERE id = ?",
            status, code, None if message is None else message[:_ERROR_CHARS], _usage(row, usage),
            row["attempts"] if attempts is None else attempts,
            row["next_attempt_at"] if next_attempt_at is None else next_attempt_at,
            row["changes"] if changes is None else json.dumps(changes), self.clock(), finished, row["id"])

    async def _retry_or_fail_dream(self, row: dict, code: str, message: str, *, retry: bool,
                                   usage: dict | None) -> str:
        final = not retry or row["attempts"] >= REVIEW_ATTEMPTS
        now = await self.coordination_now()
        status = "failed" if final else "pending"
        await self._settle_dream(row, status, code=code, message=message, usage=usage,
                                 next_attempt_at=now + RETRY_BASE_S * 2 ** (row["attempts"] - 1))
        return status

    async def fail_dream(self, claim: DreamClaim, code: str, message: str, *, retry: bool,
                         usage: dict | None = None) -> str | None:
        async with self.db.transaction():
            await self.lock_profile(claim.job.profile_id)
            row = await self._owned_dream(claim)
            if row is None:
                return None
            return await self._retry_or_fail_dream(row, code, message, retry=retry, usage=usage)

    async def release_dream(self, claim: DreamClaim, *, usage: dict | None = None) -> None:
        """Give the job back unfinished (shutdown): the attempt does not count, here or in the daily cap."""
        async with self.db.transaction():
            await self.lock_profile(claim.job.profile_id)
            row = await self._owned_dream(claim)
            if row is None:
                return
            await self._settle_dream(row, "pending", code=row["error_code"], message=row["error"], usage=usage,
                                     attempts=max(0, row["attempts"] - 1))
            await self.db.execute(
                "DELETE FROM dream_attempts WHERE ctid = (SELECT ctid FROM dream_attempts WHERE profile_id = ? "
                "AND job_id = ? ORDER BY started_at DESC LIMIT 1)", claim.job.profile_id, claim.job.id)

    async def discard_dream(self, claim: DreamClaim, code: str, *, usage: dict | None = None) -> None:
        async with self.db.transaction():
            await self.lock_profile(claim.job.profile_id)
            row = await self._owned_dream(claim)
            if row is not None:
                await self._settle_dream(row, "discarded", code=code, message="The dream's owner changed.",
                                         usage=usage)

    async def _dream_gone(self, profile_id: str, incarnation: str) -> str | None:
        profile = await self.db.fetchrow("SELECT memory, learning, incarnation FROM profiles WHERE id = ?",
                                         profile_id)
        if profile is None or profile["incarnation"] != incarnation:
            return "profile_gone"
        try:
            await self.assert_profile_admitted(profile_id)
        except ProfileDeletionBusy:
            return "profile_gone"
        if not (profile["memory"] and profile["learning"]):
            return "learning_disabled"
        return None

    async def dream_owner_gone(self, claim: DreamClaim) -> str | None:
        """Why this dream may no longer write, or None. A read without the lock; the commit checks again under it."""
        return await self._dream_gone(claim.job.profile_id, claim.profile_incarnation)

    async def list_dreams(self, profile_id: str, *, status: str | None = None, limit: int = 20) -> list[DreamJob]:
        if status is None:
            rows = await self.db.fetch("SELECT * FROM dream_jobs WHERE profile_id = ? "
                                       "ORDER BY created_at DESC, id LIMIT ?", profile_id, limit)
        else:
            rows = await self.db.fetch("SELECT * FROM dream_jobs WHERE profile_id = ? AND status = ? "
                                       "ORDER BY created_at DESC, id LIMIT ?", profile_id, status, limit)
        return [_job(row) for row in rows]

    async def retry_dream(self, profile_id: str, job_id: str) -> DreamJob | None:
        """Requeue a failed dream with fresh attempts and the current route. A pending or running one is returned
        as it is; a finished one is refused, and so is a retry while another dream of the profile is open."""
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            row = await self.db.fetchrow("SELECT * FROM dream_jobs WHERE id = ? AND profile_id = ? FOR UPDATE",
                                         job_id, profile_id)
            if row is None:
                return None
            if row["status"] in ("pending", "running"):
                return _job(row)
            if row["status"] != "failed":
                raise DreamNotRetryable(job_id)
            if await self.db.fetchrow("SELECT 1 AS open FROM dream_jobs WHERE profile_id = ? "
                                      "AND status IN ('pending', 'running')", profile_id):
                raise DreamBusy(job_id)
            provider, model = await self._dream_route(profile_id)
            updated = await self.db.fetchrow(
                "UPDATE dream_jobs SET status = 'pending', attempts = 0, error_code = NULL, error = NULL, "
                "provider = COALESCE(?, provider), model = COALESCE(?, model), next_attempt_at = ?, "
                "finished_at = NULL, updated_at = ? WHERE id = ? RETURNING *",
                provider, model, await self.coordination_now(), self.clock(), job_id)
            return _job(updated)
