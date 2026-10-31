"""Consumer feedback on a run (procedural learning design §6): one rating per run, with an optional comment that is
user-supplied data, never instructions."""

from __future__ import annotations

from fastapi import FastAPI, Request

from keel.api import ApiError, _json_body
from keel.observations import check_comment
from keel.store.base import ProfileIncarnationMismatch

RATINGS = ("up", "down")


def feedback_json(feedback) -> dict:
    return {"run_id": feedback.run_id, "rating": feedback.rating, "comment": feedback.comment,
            "created_at": feedback.created_at, "updated_at": feedback.updated_at}


def add_feedback_routes(app: FastAPI, k, profile_or_404, run_or_404) -> None:
    """Mount the feedback routes. `run_or_404` loads a run of the profile (404 run_not_found otherwise)."""

    @app.put("/v1/profiles/{profile_id}/runs/{run_id}/feedback")
    async def put_feedback(request: Request, profile_id: str, run_id: str):
        """Store or replace the run's rating; with learning on it becomes an observation, and a third down rating on
        a learned skill's version can suspend it."""
        profile = await profile_or_404(request, profile_id)
        body = await _json_body(request)
        rating, comment = body.get("rating"), body.get("comment")
        if rating not in RATINGS:
            raise ApiError(400, "invalid_input", "rating must be up or down.")
        if reason := check_comment(comment):
            raise ApiError(400, "invalid_input", reason)
        try:
            feedback, code, suspended = await k(request).store.put_feedback(
                profile.id, run_id, rating, comment, profile_incarnation=profile.incarnation)
        except ProfileIncarnationMismatch:
            raise ApiError(404, "profile_not_found", "No such profile.") from None
        if code == "not_found":
            raise ApiError(404, "run_not_found", "No such run.")
        if code == "not_terminal":
            raise ApiError(409, "run_not_terminal", "Feedback is accepted once the run has finished.")
        return {**feedback_json(feedback), "suspended": suspended}

    @app.get("/v1/profiles/{profile_id}/runs/{run_id}/feedback")
    async def get_feedback(request: Request, profile_id: str, run_id: str):
        await profile_or_404(request, profile_id)
        await run_or_404(request, profile_id, run_id)
        feedback = await k(request).store.get_feedback(profile_id, run_id)
        if feedback is None:
            raise ApiError(404, "feedback_not_found", "This run has no feedback.")
        return feedback_json(feedback)

    @app.delete("/v1/profiles/{profile_id}/runs/{run_id}/feedback")
    async def delete_feedback(request: Request, profile_id: str, run_id: str):
        profile = await profile_or_404(request, profile_id)
        await run_or_404(request, profile_id, run_id)
        try:
            deleted = await k(request).store.delete_feedback(profile.id, run_id,
                                                             profile_incarnation=profile.incarnation)
        except ProfileIncarnationMismatch:
            raise ApiError(404, "profile_not_found", "No such profile.") from None
        if not deleted:
            raise ApiError(404, "feedback_not_found", "This run has no feedback.")
        return {"deleted": True}
