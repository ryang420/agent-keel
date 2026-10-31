"""Consumer routes for a profile's learned skills (procedural learning design §7). Consumer changes are
authoritative: they enter the version history with source `consumer`. The routes work whether or not learning is
enabled, like the memory routes."""

from __future__ import annotations

from dataclasses import astuple

from fastapi import FastAPI, Request

from keel.api import ApiError, _json_body
from keel.observations import STATS_SECONDS
from keel.store.base import ProfileIncarnationMismatch, SkillOutcome, SkillSource

STATES = ("active", "suspended", "retired")
_NO_OUTCOME = SkillOutcome(0, 0, 0, 0, 0, 0)
_ERRORS = {"not_found": (404, "learned_skill_not_found"), "version_conflict": (409, "version_conflict"),
           "name_taken": (409, "name_taken"), "limit": (409, "skill_limit"), "retired": (409, "skill_retired"),
           "invalid": (400, "invalid_input")}
_FIELDS = ("name", "description", "body", "category")


def skill_json(skill, outcome: SkillOutcome, *, body: bool = False) -> dict:
    found = {"id": skill.id, "name": skill.name, "description": skill.description, "category": skill.category,
             "state": skill.state, "version": skill.version, "origin": skill.origin,
             "created_at": skill.created_at, "updated_at": skill.updated_at,
             "consumer_changed_at": skill.consumer_changed_at, "stats": {"loads_30d": outcome.loads, "up_30d": outcome.up, "down_30d": outcome.down,
                       "completed_30d": outcome.completed, "failed_30d": outcome.failed,
                       "cancelled_30d": outcome.cancelled}}
    if body:
        found["body"] = skill.body
    return found


def version_json(version) -> dict:
    return {"version": version.version, "change": version.change, "name": version.name,
            "description": version.description, "category": version.category, "body": version.body,
            "source": {"kind": version.source_kind, "run_id": version.run_id, "session_id": version.session_id,
                       "job_id": version.job_id},
            "evidence": {"message_ids": list(version.message_ids), "observation_ids": list(version.observation_ids)},
            "rationale": version.rationale, "created_at": version.created_at}


def _version(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "invalid_input", "expected_version must be an integer.")
    return value


def add_learned_routes(app: FastAPI, k, profile_or_404) -> None:
    """Mount the learned-skill routes. `k` returns the Keel of a request; `profile_or_404` loads the caller's
    profile (a foreign or missing profile is the same 404)."""

    async def outcomes(request: Request, profile_id: str) -> dict[str, SkillOutcome]:
        """Per skill id, the outcomes of its current text: every version from where the text began to the current."""
        store = k(request).store
        by_version = await store.skill_outcomes(profile_id, since=store.clock() - STATS_SECONDS)
        starts = await store.skill_text_versions(profile_id)
        totals: dict[str, SkillOutcome] = {}
        for (skill_id, version), outcome in by_version.items():
            if version >= starts.get(skill_id, 1):
                totals[skill_id] = SkillOutcome(*(a + b for a, b in zip(astuple(totals.get(skill_id, _NO_OUTCOME)),
                                                                        astuple(outcome))))
        return totals

    async def apply(request: Request, profile, op: dict):
        store = k(request).store
        try:
            result = await store.change_learned_skill(profile.id, op, source=SkillSource("consumer"),
                                                      profile_incarnation=profile.incarnation)
        except ProfileIncarnationMismatch:
            raise ApiError(404, "profile_not_found", "No such profile.") from None
        if not result["success"]:
            status, code = _ERRORS.get(result["code"], (400, "invalid_input"))
            raise ApiError(status, code, result["error"])
        skill = await store.learned_skill(profile.id, result["skill"]["id"])
        counts = await outcomes(request, profile.id)
        return skill_json(skill, counts.get(skill.id, _NO_OUTCOME), body=True)

    async def change(request: Request, profile_id: str, skill_id: str, action: str, extra: tuple[str, ...] = ()):
        profile = await profile_or_404(request, profile_id)
        body = await _json_body(request)
        op = {"action": action, "skill_id": skill_id, "expected_version": _version(body.get("expected_version"))}
        op.update({key: body[key] for key in extra if key in body})
        return await apply(request, profile, op)

    @app.get("/v1/profiles/{profile_id}/skills/learned")
    async def list_learned(request: Request, profile_id: str, state: str | None = None):
        """List the profile's learned skills with state, version, origin and load counts."""
        await profile_or_404(request, profile_id)
        if state is not None and state not in STATES:
            raise ApiError(400, "invalid_input", f"state must be one of {', '.join(STATES)}.")
        skills = await k(request).store.learned_skills(profile_id, states=None if state is None else (state,))
        counts = await outcomes(request, profile_id)
        return {"skills": [skill_json(skill, counts.get(skill.id, _NO_OUTCOME)) for skill in skills]}

    @app.post("/v1/profiles/{profile_id}/skills/learned", status_code=201)
    async def create_learned(request: Request, profile_id: str):
        """Create a skill of origin `consumer`, validated like every other writer's."""
        profile = await profile_or_404(request, profile_id)
        body = await _json_body(request)
        return await apply(request, profile, {"action": "create", **{key: body.get(key) for key in _FIELDS}})

    @app.get("/v1/profiles/{profile_id}/skills/learned/{skill_id}")
    async def get_learned(request: Request, profile_id: str, skill_id: str):
        """The current version and the whole version history with sources, evidence and rationale."""
        await profile_or_404(request, profile_id)
        store = k(request).store
        skill = await store.learned_skill(profile_id, skill_id)
        if skill is None:
            raise ApiError(404, "learned_skill_not_found", "No such learned skill.")
        counts = await outcomes(request, profile_id)
        versions = await store.learned_skill_versions(profile_id, skill_id)
        return {**skill_json(skill, counts.get(skill.id, _NO_OUTCOME), body=True),
                "versions": [version_json(version) for version in versions]}

    @app.patch("/v1/profiles/{profile_id}/skills/learned/{skill_id}")
    async def patch_learned(request: Request, profile_id: str, skill_id: str):
        """Edit name, description, body or category if the skill is still at expected_version."""
        return await change(request, profile_id, skill_id, "revise", _FIELDS)

    @app.post("/v1/profiles/{profile_id}/skills/learned/{skill_id}/rollback")
    async def rollback_learned(request: Request, profile_id: str, skill_id: str):
        """A new version equal to `version`."""
        return await change(request, profile_id, skill_id, "rollback", ("version",))

    @app.post("/v1/profiles/{profile_id}/skills/learned/{skill_id}/suspend")
    async def suspend_learned(request: Request, profile_id: str, skill_id: str):
        return await change(request, profile_id, skill_id, "suspend")

    @app.post("/v1/profiles/{profile_id}/skills/learned/{skill_id}/resume")
    async def resume_learned(request: Request, profile_id: str, skill_id: str):
        return await change(request, profile_id, skill_id, "resume")

    @app.delete("/v1/profiles/{profile_id}/skills/learned/{skill_id}")
    async def retire_learned(request: Request, profile_id: str, skill_id: str, expected_version: int | None = None):
        """Retire the skill; its history is kept until the profile is deleted."""
        profile = await profile_or_404(request, profile_id)
        if expected_version is None:
            raise ApiError(400, "invalid_input", "expected_version is required.")
        await apply(request, profile, {"action": "retire", "skill_id": skill_id, "expected_version": expected_version})
        return {"retired": True}
