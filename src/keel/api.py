"""The HTTP API (spec §2): profiles, runs with replayable SSE events, and sessions."""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
import hmac
import json
import logging
import re
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from keel.models import part_to_dict, ImagePart, TextPart
from keel.sandbox.base import PolicyError, parse_policy
from keel.store.base import ACTIVE, SessionBusy, SessionNotFound, TitleInUse
from keel.titles import sanitize_title

log = logging.getLogger(__name__)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}
_KEEPALIVE_SECONDS = 15.0
_TERMINAL_GRACE_SECONDS = 5.0
_HEALTH_TIMEOUT_SECONDS = 3.0
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_B64_CHARS = (MAX_IMAGE_BYTES + 2) // 3 * 4  # the longest base64 text a permitted image can have


@dataclass
class Keel:
    store: object
    bus: object
    providers: object
    executor: object
    api_key: str
    max_running: int
    sandbox_mount_root: str | None = None


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _check_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ApiError(400, "invalid_input", f"{what} must be 1–128 letters, digits, '.', '_' or '-'.")
    return value


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_input", "The request body must be a JSON object.")
    return body


def run_json(run) -> dict:
    return {"run_id": run.id, "status": run.status, "session_id": run.session_id, "provider": run.provider,
            "model": run.model, "reasoning_effort": run.reasoning_effort, "output": run.output,
            "usage": run.usage.to_dict(), "error_code": run.error_code, "error": run.error,
            "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at}


def message_json(stored) -> dict:
    message = stored.message
    return {
        "id": stored.id, "role": message.role, "content": message.text,
        "parts": [part_to_dict(p) for p in message.parts],
        "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                       for c in message.tool_calls] or None,
        "tool_call_id": message.tool_call_id, "tool_name": message.tool_name,
        "run_id": stored.run_id, "created_at": stored.created_at,
    }


def session_json(session) -> dict:
    return {"id": session.id, "title": session.title, "title_source": session.title_source,
            "created_at": session.created_at, "last_active_at": session.last_active_at,
            "message_count": session.message_count}


def _input_parts(body: dict) -> list[dict]:
    """The run's input as part dicts; image parts still carry their base64 data (start_run stores it as a blob)."""
    raw = body.get("input")
    if isinstance(raw, str):
        parts = [{"type": "text", "text": raw}]
    elif isinstance(raw, list) and all(isinstance(p, dict) for p in raw):
        parts = raw
    else:
        raise ApiError(400, "invalid_input", "input must be a string or a list of parts.")
    out: list[dict] = []
    for part in parts:
        if part.get("type") == "image":
            media, data = part.get("media_type"), part.get("data")
            if media not in IMAGE_TYPES:
                raise ApiError(400, "invalid_input", f"An image's media_type must be one of {', '.join(IMAGE_TYPES)}.")
            if isinstance(data, str) and len(data) > MAX_IMAGE_B64_CHARS:  # checked before decoding anything
                raise ApiError(400, "invalid_input", "Each image must be at most 5 MB.")
            try:
                size = len(base64.b64decode(data, validate=True)) if isinstance(data, str) else None
            except (binascii.Error, ValueError):
                size = None
            if size is None:
                raise ApiError(400, "invalid_input",
                               "An image's data must be raw standard base64 (no data: prefix, no line breaks).")
            if size == 0:
                raise ApiError(400, "invalid_input", "An image must not be empty.")
            if size > MAX_IMAGE_BYTES:
                raise ApiError(400, "invalid_input", "Each image must be at most 5 MB.")
            out.append(part_to_dict(ImagePart(media, data)))
        elif part.get("type") == "text" and isinstance(part.get("text"), str):
            out.append(part_to_dict(TextPart(part["text"])))
        else:
            raise ApiError(400, "invalid_input",
                           "Every input part must be {'type': 'text', 'text'} or {'type': 'image', 'media_type', 'data'}.")
    if sum(1 for p in out if p["type"] == "image") > MAX_IMAGES:
        raise ApiError(400, "invalid_input", f"A run takes at most {MAX_IMAGES} images.")
    if isinstance(body.get("file_context"), str) and body["file_context"].strip():
        out.append(part_to_dict(TextPart(body["file_context"])))
    if not any(p["type"] == "image" or p["text"].strip() for p in out):
        raise ApiError(400, "invalid_input", "input is empty.")
    return out


def create_app(keel: Keel | None = None, lifespan=None) -> FastAPI:
    app = FastAPI(title="agent-keel", lifespan=lifespan)
    if keel is not None:
        app.state.keel = keel

    def k(request: Request) -> Keel:
        return request.app.state.keel

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return _error(400, "invalid_input", "A query or path parameter is invalid.")

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception):
        log.error("unhandled error", exc_info=exc)
        return _error(500, "internal_error", "The service hit an unexpected error.")

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if getattr(request.app.state, "keel", None) is None:
            return _error(503, "unavailable", "The service is not ready.")
        if request.url.path != "/healthz":
            supplied = request.headers.get("authorization", "")
            expected = "Bearer " + k(request).api_key
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                return _error(401, "unauthorized", "A valid service key is required.")
        return await call_next(request)

    @app.get("/healthz")
    async def healthz(request: Request):
        try:
            await asyncio.wait_for(k(request).store.ping(), _HEALTH_TIMEOUT_SECONDS)
        except Exception:
            log.warning("health check: the store did not answer", exc_info=True)
            return _error(503, "unavailable", "The store is not answering.")
        return {"status": "ok"}

    async def profile_or_404(request: Request, profile_id: str):
        profile = await k(request).store.get_profile(_check_id(profile_id, "profile"))
        if profile is None:
            raise ApiError(404, "profile_not_found", "No such profile.")
        return profile

    async def session_or_404(request: Request, profile_id: str, session_id: str):
        session = await k(request).store.get_session(profile_id, session_id)
        if session is None:
            raise ApiError(404, "session_not_found", "No such session.")
        return session

    async def run_or_404(request: Request, profile_id: str, run_id: str):
        run = await k(request).store.get_run(profile_id, run_id)
        if run is None:
            raise ApiError(404, "run_not_found", "No such run.")
        return run

    @app.put("/v1/profiles/{profile_id}")
    async def put_profile(request: Request, profile_id: str):
        body = await _json_body(request)
        _check_id(profile_id, "profile")
        models = [] if body.get("models") is None else body["models"]
        if not isinstance(models, list):
            raise ApiError(400, "invalid_input", "models must be a list of {'provider', 'model'}.")
        skills, sandbox, soul = body.get("skills"), body.get("sandbox"), body.get("soul")
        skills = [] if skills is None else skills
        sandbox = {} if sandbox is None else sandbox
        soul = "" if soul is None else soul
        if not (isinstance(skills, list) and all(isinstance(x, str) for x in skills)):
            raise ApiError(400, "invalid_input", "skills must be a list of strings.")
        if not isinstance(sandbox, dict):
            raise ApiError(400, "invalid_input", "sandbox must be an object.")
        if sandbox:
            try:
                parse_policy(sandbox, k(request).sandbox_mount_root)
            except PolicyError as exc:
                raise ApiError(400, "invalid_input", str(exc))
        if not isinstance(soul, str):
            raise ApiError(400, "invalid_input", "soul must be a string.")
        if not all(isinstance(m, dict) and m.get("provider") and m.get("model") for m in models):
            raise ApiError(400, "invalid_input", "models must be a list of {'provider', 'model'}.")
        profile = await k(request).store.upsert_profile(
            profile_id, models=[{"provider": m["provider"], "model": m["model"]} for m in models],
            sandbox=sandbox, soul=soul, skills=skills)
        return {"id": profile.id, "models": profile.models, "sandbox": profile.sandbox, "soul": profile.soul,
                "skills": profile.skills, "updated_at": profile.updated_at}

    @app.post("/v1/profiles/{profile_id}/runs", status_code=202)
    async def start_run(request: Request, profile_id: str):
        keel_ = k(request)
        body = await _json_body(request)
        profile = await profile_or_404(request, profile_id)
        provider, model = body.get("provider"), body.get("model")
        if {"provider": provider, "model": model} not in profile.models:
            raise ApiError(400, "model_not_allowed", "That model is not enabled for this profile.")
        if provider not in keel_.providers.names():
            raise ApiError(400, "provider_unavailable", f"The provider {provider!r} is not configured.")
        parts = _input_parts(body)
        if any(p["type"] == "image" for p in parts) and not keel_.providers.get(provider).capabilities(model).vision:
            raise ApiError(400, "model_not_vision_capable", "That model cannot read images.")
        for field in ("instructions", "reasoning_effort"):
            if body.get(field) is not None and not isinstance(body[field], str):
                raise ApiError(400, "invalid_input", f"{field} must be a string.")
        session_id = _check_id(body.get("session_id") or f"ses_{uuid.uuid4().hex}", "session_id")
        # Refuse before creating anything, so a refused run leaves no empty session behind.
        existing = await keel_.store.get_session(profile_id, session_id)
        if existing is not None and await keel_.store.active_run_for_session(session_id) is not None:
            raise ApiError(409, "session_busy", "This session already has a run in progress.")
        if await keel_.store.count_in_flight() >= keel_.max_running:
            return JSONResponse({"error": {"code": "at_capacity", "message": "Too many runs in progress."}},
                                status_code=429, headers={"Retry-After": "1"})
        try:
            await keel_.store.ensure_session(profile_id, session_id)
        except SessionNotFound:
            raise ApiError(404, "session_not_found", "No such session.")
        blobs = []
        try:
            for index, part in enumerate(parts):
                if part["type"] == "image":
                    blob = await keel_.store.put_blob(profile_id, session_id, part["media_type"], part["data_b64"])
                    blobs.append(blob)
                    parts[index] = part_to_dict(ImagePart(part["media_type"], blob_id=blob))
            run = await keel_.store.create_run(
                profile_id=profile_id, session_id=session_id, provider=provider, model=model,
                reasoning_effort=body.get("reasoning_effort"), input_parts=parts,
                instructions=body.get("instructions"))
        except BaseException as exc:
            # Whatever went wrong after the first blob was written, no blob may be left without a run.
            if blobs:
                try:
                    await keel_.store.delete_blobs(blobs)
                except Exception:
                    log.warning("could not delete the blobs of a refused run", exc_info=True)
            if isinstance(exc, SessionBusy):
                raise ApiError(409, "session_busy", "This session already has a run in progress.") from exc
            raise
        return {"run_id": run.id, "status": run.status, "session_id": session_id}

    @app.get("/v1/profiles/{profile_id}/runs")
    async def list_runs(request: Request, profile_id: str, status: str = "active"):
        statuses = ACTIVE if status == "active" else tuple(s for s in status.split(",") if s)
        runs = await k(request).store.list_runs(profile_id, statuses)
        return {"data": [run_json(run) for run in runs]}

    @app.get("/v1/profiles/{profile_id}/runs/{run_id}")
    async def get_run(request: Request, profile_id: str, run_id: str):
        return run_json(await run_or_404(request, profile_id, run_id))

    @app.post("/v1/profiles/{profile_id}/runs/{run_id}/stop")
    async def stop_run(request: Request, profile_id: str, run_id: str):
        keel_ = k(request)
        before = await run_or_404(request, profile_id, run_id)
        run = await keel_.store.request_stop(profile_id, run_id)
        if run is None:
            raise ApiError(404, "run_not_found", "No such run.")
        if before.status == "queued" and run.status == "cancelled":
            await keel_.executor.emit(run_id, "run.cancelled", {})
        await keel_.bus.notify("stop:" + run_id)
        return run_json(run)

    @app.get("/v1/profiles/{profile_id}/runs/{run_id}/events")
    async def run_events(request: Request, profile_id: str, run_id: str, after: int = 0):
        keel_ = k(request)
        await run_or_404(request, profile_id, run_id)

        async def frames():
            cursor = max(0, after)
            while True:
                waiter = keel_.bus.waiter(run_id)
                events = await keel_.store.events_after(run_id, cursor)
                for event in events:
                    cursor = event.seq
                    payload = {**event.data, "seq": event.seq, "type": event.type, "run_id": run_id, "at": event.at}
                    yield f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(payload)}\n\n"
                    if event.type in _TERMINAL_EVENTS:
                        return
                if events:
                    continue
                run = await keel_.store.get_run(profile_id, run_id)
                if run is None:
                    return
                if run.status in ("completed", "failed", "cancelled"):
                    # The run row turns terminal before its terminal event is appended, so wait briefly for it.
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + _TERMINAL_GRACE_SECONDS
                    while True:
                        waiter = keel_.bus.waiter(run_id)
                        for event in await keel_.store.events_after(run_id, max(cursor - 1, 0)):
                            if event.seq > cursor:
                                cursor = event.seq
                                payload = {**event.data, "seq": event.seq, "type": event.type,
                                           "run_id": run_id, "at": event.at}
                                yield f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(payload)}\n\n"
                            if event.type in _TERMINAL_EVENTS:
                                return
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            return
                        try:
                            await asyncio.wait_for(waiter.wait(), min(remaining, 1.0))
                        except TimeoutError:
                            continue
                try:
                    await asyncio.wait_for(waiter.wait(), _KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keepalive\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/v1/profiles/{profile_id}/sessions")
    async def list_sessions(request: Request, profile_id: str, limit: int = 100):
        sessions = await k(request).store.list_sessions(profile_id, max(1, min(limit, 500)))
        return {"data": [session_json(s) for s in sessions]}

    @app.get("/v1/profiles/{profile_id}/sessions/{session_id}/messages")
    async def session_messages(request: Request, profile_id: str, session_id: str, limit: int = 200,
                               offset: int = 0, after_id: int | None = None):
        store = k(request).store
        await session_or_404(request, profile_id, session_id)
        if after_id is not None:
            rows = await store.messages_after(session_id, after_id)
            return {"data": [message_json(m) for m in rows]}
        limit, offset = max(1, min(limit, 500)), max(0, offset)
        rows = await store.messages_page(session_id, limit, offset)
        return {"data": [message_json(m) for m in rows],
                "pagination": {"limit": limit, "offset": offset, "returned": len(rows)}}

    @app.get("/v1/profiles/{profile_id}/sessions/{session_id}/boundary")
    async def boundary(request: Request, profile_id: str, session_id: str):
        await session_or_404(request, profile_id, session_id)
        return {"last_message_id": await k(request).store.last_message_id(session_id)}

    @app.patch("/v1/profiles/{profile_id}/sessions/{session_id}")
    async def rename(request: Request, profile_id: str, session_id: str):
        body = await _json_body(request)
        title = sanitize_title(body.get("title") or "") if isinstance(body.get("title"), str) else ""
        if not title:
            raise ApiError(400, "invalid_input", "A title is required.")
        try:
            session = await k(request).store.set_title(profile_id, session_id, title, "user")
        except SessionNotFound:
            raise ApiError(404, "session_not_found", "No such session.")
        except TitleInUse:
            raise ApiError(409, "title_in_use", "Another conversation already has that title.")
        return session_json(session)

    @app.delete("/v1/profiles/{profile_id}/sessions/{session_id}")
    async def delete(request: Request, profile_id: str, session_id: str):
        store = k(request).store
        try:
            deleted = await store.delete_session(profile_id, session_id)
        except SessionBusy:
            raise ApiError(409, "session_busy", "Stop the run in progress before deleting the session.")
        if not deleted:
            raise ApiError(404, "session_not_found", "No such session.")
        return {"deleted": True}

    return app
