"""Learned skill rows (procedural learning design §2, §3, §7): one row per skill, an immutable version per change
with the evidence it cites, and the learned skills each run loaded. Every change takes the profile lock and checks
admission, incarnation and, for model writers, the learning flag in the same transaction."""

from __future__ import annotations

import json
import uuid

from keel.learned import MAX_DETAIL, MAX_SKILLS, MAX_TOPIC, check
from keel.review_observe import StagedObservation
from keel.store.base import LearnedSkill, LearnedSkillVersion, SkillSource

_ACTIONS = ("create", "revise", "retire", "rollback", "suspend", "resume")
_FIELDS = ("name", "description", "body", "category")
_SKILL = ("SELECT id, name, description, category, body, version, state, origin, consumer_changed_at, created_at, "
          "updated_at FROM learned_skills WHERE profile_id = ?")


def _skill(row: dict) -> LearnedSkill:
    return LearnedSkill(row["id"], row["name"], row["description"], row["category"], row["body"], row["version"],
                        row["state"], row["origin"], row["consumer_changed_at"], row["created_at"], row["updated_at"])


def _fail(code: str, error: str, skill: LearnedSkill | None = None) -> dict:
    result = {"success": False, "code": code, "error": error}
    if skill is not None:
        result["skill"] = skill.summary()
    return result


def _version_arg(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class LearnedSkillStoreMixin:
    async def change_learned_skill(self, profile_id: str, op: dict, *, source: SkillSource,
                                   profile_incarnation: str | None = None, mutation_key: str | None = None,
                                   require_learning: bool = False) -> dict:
        """Apply one change atomically: a new version row, its evidence, and for a foreground create or revise a
        consumed `requested` observation. A mutation key that already committed returns its first result."""
        action = op.get("action")
        if action not in _ACTIONS:
            return _fail("invalid", f"action must be one of {', '.join(_ACTIONS)}.")
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            await self.assert_profile_admitted(profile_id)
            await self._check_profile_incarnation(profile_id, profile_incarnation)
            row = await self.db.fetchrow("SELECT skills, learning FROM profiles WHERE id = ?", profile_id)
            if row is None:
                return _fail("not_found", "No such learned skill.")
            if require_learning and not row["learning"]:
                return _fail("learning_disabled", "Learning is not enabled for this profile.")
            if mutation_key is not None:
                done = await self.db.fetchrow(
                    "SELECT result FROM memory_mutations WHERE profile_id = ? AND mutation_key = ?",
                    profile_id, mutation_key)
                if done is not None:
                    return {**json.loads(done["result"]), "replayed": True}
            shipped = set(json.loads(row["skills"]))
            if action == "create":
                result = await self._create_skill(profile_id, op, shipped, source)
            else:
                result = await self._change_skill(profile_id, action, op, shipped, source)
            if result["success"] and mutation_key is not None:
                await self.db.execute(
                    "INSERT INTO memory_mutations (profile_id, mutation_key, session_id, result, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    profile_id, mutation_key, source.session_id, json.dumps(result, ensure_ascii=False), self.clock())
            return result

    async def _name_taken(self, profile_id: str, name: str, shipped: set[str], skill_id: str | None) -> str | None:
        if name in shipped:
            return "A deployment skill enabled for this profile has that name."
        row = await self.db.fetchrow(
            "SELECT id FROM learned_skills WHERE profile_id = ? AND name = ? AND state <> 'retired' AND id <> ?",
            profile_id, name, skill_id or "")
        return "A learned skill already has that name; revise it instead." if row else None

    async def _create_skill(self, profile_id: str, op: dict, shipped: set[str], source: SkillSource) -> dict:
        if source.kind not in ("foreground", "dream", "consumer"):
            return _fail("invalid", "Only the foreground, a dream or the consumer can create a skill.")
        name, description, body, category = (op.get(key) for key in _FIELDS)
        if reason := check(name, description, body, category):
            return _fail("invalid", reason)
        if reason := await self._name_taken(profile_id, name, shipped, None):
            return _fail("name_taken", reason)
        live = await self.db.fetchrow(
            "SELECT COUNT(*) AS n FROM learned_skills WHERE profile_id = ? AND state <> 'retired'", profile_id)
        if live["n"] >= MAX_SKILLS:
            return _fail("limit", f"This profile already has {MAX_SKILLS} learned skills; merge or retire one first.")
        now = self.clock()
        skill = LearnedSkill(uuid.uuid4().hex, name, description, category, body, 1, "active", source.kind,
                             now if source.kind == "consumer" else None, now, now)
        await self.db.execute(
            "INSERT INTO learned_skills (id, profile_id, name, description, category, body, version, state, origin, "
            "consumer_changed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            skill.id, profile_id, name, description, category, body, 1, "active", source.kind,
            skill.consumer_changed_at, now, now)
        await self._write_version(profile_id, skill, "create", source, op.get("rationale"))
        return {"success": True, "skill": skill.summary()}

    async def _change_skill(self, profile_id: str, action: str, op: dict, shipped: set[str],
                            source: SkillSource) -> dict:
        skill_id, expected = op.get("skill_id"), op.get("expected_version")
        if not isinstance(skill_id, str) or not _version_arg(expected):
            return _fail("invalid", "skill_id and expected_version are required.")
        row = await self.db.fetchrow(_SKILL + " AND id = ?", profile_id, skill_id)
        if row is None:
            return _fail("not_found", "No such learned skill.")
        skill = _skill(row)
        if skill.state == "retired":
            return _fail("retired", "That skill is retired; its history is kept.", skill)
        if skill.version != expected:
            return _fail("version_conflict", f"The skill is at version {skill.version}, not {expected}.", skill)
        fields = {"name": skill.name, "description": skill.description, "body": skill.body,
                  "category": skill.category}
        state = skill.state
        if action == "revise":
            given = {key: op[key] for key in _FIELDS if key in op}
            if not given:
                return _fail("invalid", "Give at least one of name, description, body or category.", skill)
            fields.update(given)
        elif action == "rollback":
            target = op.get("version")
            old = await self.db.fetchrow(
                "SELECT name, description, body, category FROM learned_skill_versions "
                "WHERE profile_id = ? AND skill_id = ? AND version = ?",
                profile_id, skill_id, target) if _version_arg(target) else None
            if old is None:
                return _fail("invalid", f"The skill has no version {target}.", skill)
            fields = {key: old[key] for key in _FIELDS}
        elif action == "suspend":
            if state != "active":
                return _fail("invalid", "Only an active skill can be suspended.", skill)
            state = "suspended"
        elif action == "resume":
            if state != "suspended":
                return _fail("invalid", "Only a suspended skill can be resumed.", skill)
            state = "active"
        else:
            state = "retired"
        if action in ("revise", "rollback"):
            if reason := check(fields["name"], fields["description"], fields["body"], fields["category"]):
                return _fail("invalid", reason, skill)
            if fields["name"] != skill.name and (
                    reason := await self._name_taken(profile_id, fields["name"], shipped, skill.id)):
                return _fail("name_taken", reason, skill)
        now = self.clock()
        changed = LearnedSkill(skill.id, fields["name"], fields["description"], fields["category"], fields["body"],
                               skill.version + 1, state, skill.origin,
                               now if source.kind == "consumer" else skill.consumer_changed_at, skill.created_at, now)
        await self.db.execute(
            "UPDATE learned_skills SET name = ?, description = ?, category = ?, body = ?, version = ?, state = ?, "
            "consumer_changed_at = ?, updated_at = ? WHERE profile_id = ? AND id = ?",
            changed.name, changed.description, changed.category, changed.body, changed.version, changed.state,
            changed.consumer_changed_at, now, profile_id, skill.id)
        await self._write_version(profile_id, changed, action, source, op.get("rationale"))
        return {"success": True, "skill": changed.summary()}

    async def _write_version(self, profile_id: str, skill: LearnedSkill, change: str, source: SkillSource,
                             rationale) -> None:
        now = self.clock()
        await self.db.execute(
            "INSERT INTO learned_skill_versions (profile_id, skill_id, version, name, description, category, body, "
            "change, source_kind, run_id, session_id, tool_call_id, job_id, rationale, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            profile_id, skill.id, skill.version, skill.name, skill.description, skill.category, skill.body, change,
            source.kind, source.run_id, source.session_id, source.tool_call_id, source.job_id,
            rationale if isinstance(rationale, str) else None, now)
        if source.session_id is None or source.message_id is None:
            return
        await self.db.execute(
            "INSERT INTO learned_skill_evidence (profile_id, skill_id, version, session_id, message_id) "
            "VALUES (?, ?, ?, ?, ?)", profile_id, skill.id, skill.version, source.session_id, source.message_id)
        if source.kind == "foreground" and change in ("create", "revise"):
            observation = await self._requested_observation(profile_id, skill, source, now)
            await self.db.execute(
                "INSERT INTO learned_skill_evidence (profile_id, skill_id, version, session_id, observation_id) "
                "VALUES (?, ?, ?, ?, ?)", profile_id, skill.id, skill.version, source.session_id, observation)

    async def _requested_observation(self, profile_id: str, skill: LearnedSkill, source: SkillSource,
                                     now: float) -> str:
        """The user's explicit request, recorded as an observation this write consumed: a later dream sees it as
        history, not pending work (learning §3)."""
        session = await self.db.fetchrow("SELECT generation FROM sessions WHERE id = ? AND profile_id = ?",
                                         source.session_id, profile_id)
        item = StagedObservation(skill.name[:MAX_TOPIC], skill.description[:MAX_DETAIL], skill.body[:MAX_DETAIL],
                                 "requested", (source.message_id,))
        return await self._insert_observation(profile_id, source.session_id, session["generation"] if session else "",
                                              item, now=now, state="consumed",
                                              consumed_by=f"skill:{skill.id}:{skill.version}")

    async def learned_skills(self, profile_id: str, *, states: tuple[str, ...] | None = None) -> list[LearnedSkill]:
        where, args = "", [profile_id]
        if states is not None:
            where = f" AND state IN ({', '.join('?' for _ in states)})" if states else " AND FALSE"
            args += list(states)
        rows = await self.db.fetch(_SKILL + where + " ORDER BY name, created_at", *args)
        return [_skill(row) for row in rows]

    async def learned_skill(self, profile_id: str, skill_id: str) -> LearnedSkill | None:
        row = await self.db.fetchrow(_SKILL + " AND id = ?", profile_id, skill_id)
        return _skill(row) if row else None

    async def learned_skill_named(self, profile_id: str, name: str) -> LearnedSkill | None:
        """The live skill with this name, else the most recently retired one (so skill_view can say it is gone)."""
        row = await self.db.fetchrow(
            _SKILL + " AND name = ? ORDER BY (state = 'retired'), updated_at DESC LIMIT 1", profile_id, name)
        return _skill(row) if row else None

    async def learned_skill_versions(self, profile_id: str, skill_id: str) -> list[LearnedSkillVersion]:
        rows = await self.db.fetch(
            "SELECT * FROM learned_skill_versions WHERE profile_id = ? AND skill_id = ? ORDER BY version",
            profile_id, skill_id)
        cited = await self.db.fetch(
            "SELECT version, message_id, observation_id FROM learned_skill_evidence "
            "WHERE profile_id = ? AND skill_id = ? ORDER BY version, message_id, observation_id", profile_id, skill_id)
        found = []
        for row in rows:
            mine = [item for item in cited if item["version"] == row["version"]]
            found.append(LearnedSkillVersion(
                row["version"], row["name"], row["description"], row["category"], row["body"], row["change"],
                row["source_kind"], row["run_id"], row["session_id"], row["job_id"], row["rationale"],
                row["created_at"],
                tuple(item["message_id"] for item in mine if item["message_id"] is not None),
                tuple(item["observation_id"] for item in mine if item["observation_id"] is not None)))
        return found

    async def record_skill_load(self, profile_id: str, session_id: str, run_id: str, skill_id: str, version: int, *,
                                profile_incarnation: str | None = None) -> None:
        """skill_view loaded this version in this run: one row per (run, skill), the latest version wins."""
        async with self.db.transaction():
            await self.lock_profile(profile_id)
            await self.assert_profile_admitted(profile_id)
            await self._check_profile_incarnation(profile_id, profile_incarnation)
            await self.db.execute(
                "INSERT INTO skill_loads (profile_id, run_id, session_id, skill_id, version, loaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (profile_id, run_id, skill_id) DO UPDATE SET "
                "version = excluded.version, loaded_at = excluded.loaded_at",
                profile_id, run_id, session_id, skill_id, version, self.clock())

    async def skill_load_counts(self, profile_id: str, since: float) -> dict[tuple[str, int], int]:
        rows = await self.db.fetch(
            "SELECT skill_id, version, COUNT(*) AS n FROM skill_loads WHERE profile_id = ? AND loaded_at >= ? "
            "GROUP BY skill_id, version", profile_id, since)
        return {(row["skill_id"], row["version"]): row["n"] for row in rows}
