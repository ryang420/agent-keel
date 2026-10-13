import asyncio
import base64
import json

import httpx
import pytest

from keel.api import Keel, create_app
from keel.events import InMemoryEventBus
from keel.models import TextDelta
from keel.providers.base import Capabilities, ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.worker import WorkerPool

AUTH = {"Authorization": "Bearer test-key"}
PROFILE = {"models": [{"provider": "fake", "model": "m"}], "sandbox": {}, "soul": "", "skills": []}


@pytest.fixture
async def service(store):
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, registry)
    pool = WorkerPool(store, bus, executor, owner="w1", workers=2, poll_seconds=0.01)
    keel = Keel(store=store, bus=bus, providers=registry, executor=executor, api_key="test-key", max_running=2)
    app = create_app(keel)
    await pool.start()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel", headers=AUTH)
    yield client, provider, store
    await client.aclose()
    await pool.close(drain_seconds=0.1)


async def settle(client, run_id, seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        body = (await client.get(f"/v1/profiles/p1/runs/{run_id}")).json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(body)


async def send(client, text="Hello there", session="chat-1", **extra):
    payload = {"input": text, "session_id": session, "provider": "fake", "model": "m", **extra}
    return await client.post("/v1/profiles/p1/runs", json=payload)


async def test_health_is_open_and_everything_else_needs_the_key(service):
    client, _, _ = service
    bare = httpx.AsyncClient(transport=client._transport, base_url="http://keel")
    assert (await bare.get("/healthz")).json() == {"status": "ok"}
    denied = await bare.put("/v1/profiles/p1", json=PROFILE)
    assert denied.status_code == 401 and denied.json()["error"]["code"] == "unauthorized"
    wrong = await bare.get("/v1/profiles/p1/sessions", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    await bare.aclose()


async def test_a_run_completes_streams_and_is_stored(service):
    client, _, _ = service
    assert (await client.put("/v1/profiles/p1", json=PROFILE)).status_code == 200
    started = await send(client, instructions="Be brief.", reasoning_effort="low")
    assert started.status_code == 202
    body = started.json()
    assert body["status"] == "queued" and body["session_id"] == "chat-1"
    run = await settle(client, body["run_id"])
    assert run["status"] == "completed" and run["output"] == "ok"
    assert run["usage"] == {"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0, "cache_write_tokens": 0}

    stream = await client.get(f"/v1/profiles/p1/runs/{body['run_id']}/events")
    frames = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
    assert [f["type"] for f in frames] == ["run.started", "message.delta", "run.completed"]
    assert frames[1]["text"] == "ok" and [f["seq"] for f in frames] == [1, 2, 3]
    later = await client.get(f"/v1/profiles/p1/runs/{body['run_id']}/events", params={"after": 2})
    assert [json.loads(l[6:])["type"] for l in later.text.splitlines() if l.startswith("data: ")] == ["run.completed"]

    page = (await client.get("/v1/profiles/p1/sessions/chat-1/messages")).json()
    assert [(m["role"], m["content"]) for m in page["data"]] == [("user", "Hello there"), ("assistant", "ok")]
    assert page["pagination"] == {"limit": 200, "offset": 0, "returned": 2}
    first_id = page["data"][0]["id"]
    assert (await client.get("/v1/profiles/p1/sessions/chat-1/boundary")).json() == \
        {"last_message_id": page["data"][1]["id"]}
    after = (await client.get("/v1/profiles/p1/sessions/chat-1/messages", params={"after_id": first_id})).json()
    assert [m["role"] for m in after["data"]] == ["assistant"]
    sessions = (await client.get("/v1/profiles/p1/sessions")).json()["data"]
    assert sessions[0]["id"] == "chat-1" and sessions[0]["title"] == "Hello there"


async def test_file_context_travels_as_its_own_text_part(service):
    client, provider, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    body = (await send(client, file_context="Attached: a.csv")).json()
    await settle(client, body["run_id"])
    assert [p.text for p in provider.requests[-1].messages[-1].parts] == ["Hello there", "Attached: a.csv"]


async def test_refusals(service):
    client, provider, _ = service
    assert (await send(client)).json()["error"]["code"] == "profile_not_found"
    await client.put("/v1/profiles/p1", json=PROFILE)
    bad_model = await send(client, model="other")
    assert bad_model.status_code == 400 and bad_model.json()["error"]["code"] == "model_not_allowed"
    empty = await send(client, text="   ")
    assert empty.status_code == 400 and empty.json()["error"]["code"] == "invalid_input"
    bad_session = await send(client, session="has spaces")
    assert bad_session.status_code == 400
    missing = await client.get("/v1/profiles/p1/runs/run_nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "run_not_found"


async def test_a_busy_session_and_a_full_service_are_refused(service):
    client, provider, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    provider.script = [["hang"], ["hang"]]
    first = (await send(client, session="a")).json()
    busy = await send(client, session="a")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "session_busy"
    await send(client, session="b")
    full = await send(client, session="c")
    assert full.status_code == 429 and full.json()["error"]["code"] == "at_capacity"
    active = (await client.get("/v1/profiles/p1/runs", params={"status": "active"})).json()["data"]
    assert first["run_id"] in [r["run_id"] for r in active]
    # A refused run leaves no empty session behind.
    ids = [s["id"] for s in (await client.get("/v1/profiles/p1/sessions")).json()["data"]]
    assert "c" not in ids and sorted(ids) == ["a", "b"]


async def test_stop_cancels_a_running_run(service):
    client, provider, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    provider.script = [[TextDelta("par"), "hang"]]
    run_id = (await send(client)).json()["run_id"]
    for _ in range(100):
        if (await client.get(f"/v1/profiles/p1/runs/{run_id}")).json()["status"] == "running":
            break
        await asyncio.sleep(0.02)
    stopped = await client.post(f"/v1/profiles/p1/runs/{run_id}/stop")
    assert stopped.status_code == 200 and stopped.json()["status"] in ("stopping", "cancelled")
    assert (await settle(client, run_id))["status"] == "cancelled"


async def test_rename_conflict_and_delete(service):
    client, _, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    for session in ("x", "y"):
        await settle(client, (await send(client, session=session)).json()["run_id"])
    renamed = await client.patch("/v1/profiles/p1/sessions/x", json={"title": "  Q3 review "})
    assert renamed.status_code == 200 and renamed.json()["title"] == "Q3 review"
    clash = await client.patch("/v1/profiles/p1/sessions/y", json={"title": "Q3 review"})
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "title_in_use"
    blank = await client.patch("/v1/profiles/p1/sessions/y", json={"title": "  "})
    assert blank.status_code == 400
    assert (await client.delete("/v1/profiles/p1/sessions/x")).json() == {"deleted": True}
    gone = await client.get("/v1/profiles/p1/sessions/x/messages")
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "session_not_found"


async def test_a_create_run_race_maps_to_session_busy(service, monkeypatch):
    client, provider, store = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    provider.script = [["hang"]]
    assert (await send(client, session="r")).status_code == 202

    async def none(_session_id):
        return None

    monkeypatch.setattr(store, "active_run_for_session", none)
    raced = await send(client, session="r")
    assert raced.status_code == 409 and raced.json()["error"]["code"] == "session_busy"
    deleted = await client.delete("/v1/profiles/p1/sessions/r")
    assert deleted.status_code == 409 and deleted.json()["error"]["code"] == "session_busy"


async def test_events_after_the_terminal_event_end_promptly(service):
    client, _, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    run_id = (await send(client)).json()["run_id"]
    await settle(client, run_id)
    stream = await asyncio.wait_for(client.get(f"/v1/profiles/p1/runs/{run_id}/events", params={"after": 3}), 5)
    assert [l for l in stream.text.splitlines() if l.startswith("data: ")] == []


async def test_malformed_bodies_and_queries_use_the_error_envelope(service):
    client, _, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    bad = await client.put("/v1/profiles/p1", content=b"{nope", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_input"
    listed = await client.post("/v1/profiles/p1/runs", json=[1])
    assert listed.status_code == 400 and listed.json()["error"]["code"] == "invalid_input"
    renamed = await client.patch("/v1/profiles/p1/sessions/x", content=b"[]", headers={"Content-Type": "application/json"})
    assert renamed.status_code == 400
    for field, value in (("skills", "a"), ("skills", [1]), ("sandbox", []), ("soul", 5)):
        resp = await client.put("/v1/profiles/p1", json={**PROFILE, field: value})
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_input", field
    query = await client.get("/v1/profiles/p1/sessions", params={"limit": "abc"})
    assert query.status_code == 400 and query.json()["error"]["code"] == "invalid_input"


async def test_other_profiles_cannot_reach_a_profiles_sessions_or_runs(service):
    client, _, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    await client.put("/v1/profiles/p2", json=PROFILE)
    run_id = (await send(client, session="s1")).json()["run_id"]
    await settle(client, run_id)
    base = "/v1/profiles/p2"
    checks = [
        (client.get(f"{base}/sessions/s1/messages"), "session_not_found"),
        (client.get(f"{base}/sessions/s1/boundary"), "session_not_found"),
        (client.patch(f"{base}/sessions/s1", json={"title": "mine"}), "session_not_found"),
        (client.delete(f"{base}/sessions/s1"), "session_not_found"),
        (client.get(f"{base}/runs/{run_id}"), "run_not_found"),
        (client.get(f"{base}/runs/{run_id}/events"), "run_not_found"),
        (client.post(f"{base}/runs/{run_id}/stop"), "run_not_found"),
    ]
    for call, code in checks:
        resp = await call
        assert resp.status_code == 404 and resp.json()["error"]["code"] == code, (code, resp.text)
    assert (await client.get(f"{base}/sessions")).json()["data"] == []
    assert (await client.get(f"{base}/runs", params={"status": "completed"})).json()["data"] == []
    assert (await client.get("/v1/profiles/p1/sessions/s1/messages")).status_code == 200


async def _finished_without_terminal_event(client, provider, store):
    await client.put("/v1/profiles/p1", json=PROFILE)
    provider.script = [["hang"]]
    run_id = (await send(client)).json()["run_id"]
    for _ in range(200):
        if (await client.get(f"/v1/profiles/p1/runs/{run_id}")).json()["status"] == "running":
            break
        await asyncio.sleep(0.02)
    assert await store.finish_run(run_id, "completed", output="x")
    return run_id


async def test_stream_waits_for_a_terminal_event_that_lands_after_the_status(service):
    client, provider, store = service
    run_id = await _finished_without_terminal_event(client, provider, store)
    executor = client._transport.app.state.keel.executor
    stream = asyncio.create_task(client.get(f"/v1/profiles/p1/runs/{run_id}/events"))
    await asyncio.sleep(0.2)
    assert not stream.done()
    await executor.emit(run_id, "run.completed", {"output": "x", "usage": {}})
    response = await asyncio.wait_for(stream, 5)
    types = [json.loads(l[6:])["type"] for l in response.text.splitlines() if l.startswith("data: ")]
    assert types[-1] == "run.completed"


async def test_a_run_finished_seconds_ago_still_waits_for_its_terminal_event(service):
    client, provider, store = service
    run_id = await _finished_without_terminal_event(client, provider, store)
    store.clock.now += 30
    executor = client._transport.app.state.keel.executor
    stream = asyncio.create_task(client.get(f"/v1/profiles/p1/runs/{run_id}/events"))
    await asyncio.sleep(0.2)
    assert not stream.done()
    await executor.emit(run_id, "run.completed", {"output": "x", "usage": {}})
    response = await asyncio.wait_for(stream, 5)
    types = [json.loads(l[6:])["type"] for l in response.text.splitlines() if l.startswith("data: ")]
    assert types[-1] == "run.completed"


async def test_a_run_finished_over_a_minute_ago_without_a_terminal_event_closes_at_once(service):
    client, provider, store = service
    run_id = await _finished_without_terminal_event(client, provider, store)
    store.clock.now += 61
    response = await asyncio.wait_for(client.get(f"/v1/profiles/p1/runs/{run_id}/events"), 2)
    assert response.status_code == 200


async def test_stream_gives_up_when_no_terminal_event_ever_comes(service, monkeypatch):
    client, provider, store = service
    monkeypatch.setattr("keel.api._TERMINAL_GRACE_SECONDS", 0.2)
    run_id = await _finished_without_terminal_event(client, provider, store)
    response = await asyncio.wait_for(client.get(f"/v1/profiles/p1/runs/{run_id}/events"), 1.5)
    assert response.status_code == 200
    assert all(json.loads(l[6:])["type"] not in ("run.completed", "run.failed", "run.cancelled")
               for l in response.text.splitlines() if l.startswith("data: "))


async def test_an_unexpected_error_is_a_json_500(service, monkeypatch):
    client, _, store = service

    async def boom(*args):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(store, "get_profile", boom)
    quiet = httpx.AsyncClient(transport=httpx.ASGITransport(app=client._transport.app, raise_app_exceptions=False),
                              base_url="http://keel", headers=AUTH)
    response = await quiet.put("/v1/profiles/p1", json=PROFILE)
    await quiet.aclose()
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    assert "secret" not in response.text


async def test_health_is_503_when_the_store_fails_or_stalls(service, monkeypatch):
    client, _, store = service

    async def broken():
        raise RuntimeError("down")

    monkeypatch.setattr(store, "ping", broken)
    down = await client.get("/healthz")
    assert down.status_code == 503 and down.json()["error"]["code"] == "unavailable"

    async def stalled():
        await asyncio.sleep(3600)

    monkeypatch.setattr(store, "ping", stalled)
    monkeypatch.setattr("keel.api._HEALTH_TIMEOUT_SECONDS", 0.05)
    slow = await client.get("/healthz")
    assert slow.status_code == 503


@pytest.mark.parametrize("field", ["instructions", "reasoning_effort"])
async def test_instructions_and_effort_must_be_strings(service, field):
    client, _, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    for bad in (5, ["a"], {"a": 1}, True):
        response = await send(client, **{field: bad})
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_input"
    assert (await send(client, **{field: None})).status_code == 202


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


async def test_an_image_run_reaches_the_model_and_is_stored_by_reference(service):
    client, provider, store = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    started = await send(client, text=[{"type": "text", "text": "What is this?"},
                                       {"type": "image", "media_type": "image/png", "data": PNG}])
    assert started.status_code == 202
    await settle(client, started.json()["run_id"])
    sent = provider.requests[-1].messages[-1].parts
    assert sent[1].media_type == "image/png" and sent[1].data_b64 == PNG and sent[1].blob_id
    page = (await client.get("/v1/profiles/p1/sessions/chat-1/messages")).json()["data"]
    assert page[0]["parts"][1] == {"type": "image", "media_type": "image/png", "blob_id": sent[1].blob_id}
    assert "data_b64" not in json.dumps(page)


async def test_an_image_only_message_is_accepted(service):
    client, _, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    started = await send(client, text=[{"type": "image", "media_type": "image/jpeg", "data": PNG}])
    assert started.status_code == 202


@pytest.mark.parametrize("parts, message", [
    ([{"type": "image", "media_type": "image/tiff", "data": PNG}], "media_type"),
    ([{"type": "image", "media_type": "image/png", "data": "not base64!"}], "raw standard base64"),
    ([{"type": "image", "media_type": "image/png", "data": ""}], "must not be empty"),
    ([{"type": "image", "media_type": "image/png", "data": "A" * 8_000_000}], "5 MB"),
    ([{"type": "image", "media_type": "image/png", "data": PNG}] * 5, "at most 4"),
    ([{"type": "image", "media_type": "image/png",
       "data": base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode()}], "5 MB"),
])
async def test_bad_images_are_refused(service, parts, message):
    client, _, _ = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    refused = await send(client, text=parts)
    assert refused.status_code == 400 and message in refused.json()["error"]["message"]


async def test_a_model_without_vision_refuses_images(service):
    client, provider, _ = service
    provider._capabilities = Capabilities(128_000, False, False, True, "none")
    await client.put("/v1/profiles/p1", json=PROFILE)
    refused = await send(client, text=[{"type": "image", "media_type": "image/png", "data": PNG}])
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "model_not_vision_capable"


async def test_a_profile_sandbox_policy_is_validated(service):
    client, _, _ = service
    good = {**PROFILE, "sandbox": {"image": "img", "workdir": "/w"}}
    assert (await client.put("/v1/profiles/p1", json=good)).status_code == 200
    bad = await client.put("/v1/profiles/p1", json={**PROFILE, "sandbox": {"image": "img"}})
    assert bad.status_code == 400 and "workdir" in bad.json()["error"]["message"]


async def test_no_blob_is_left_behind_when_a_later_step_fails(service, monkeypatch):
    client, _, store = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    stored = []
    real_put = store.put_blob

    async def second_put_fails(*args, **kwargs):
        if stored:
            raise RuntimeError("disk full")
        stored.append(await real_put(*args, **kwargs))
        return stored[0]

    monkeypatch.setattr(store, "put_blob", second_put_fails)
    image = {"type": "image", "media_type": "image/png", "data": PNG}
    with pytest.raises(RuntimeError):
        await send(client, text=[image, image])
    assert await store.get_blobs(stored) == {}
    monkeypatch.undo()

    async def create_fails(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "create_run", create_fails)
    puts = []

    async def recording_put(*args, **kwargs):
        puts.append(await real_put(*args, **kwargs))
        return puts[-1]

    monkeypatch.setattr(store, "put_blob", recording_put)
    with pytest.raises(RuntimeError):
        await send(client, text=[image])
    assert puts and await store.get_blobs(puts) == {}


async def test_the_stream_of_a_pruned_run_closes_at_once(service):
    client, _, store = service
    await client.put("/v1/profiles/p1", json=PROFILE)
    run_id = (await send(client)).json()["run_id"]
    await settle(client, run_id)
    store.clock.now += 8 * 86_400
    assert await store.prune_events(store.clock() - 7 * 86_400) > 0
    response = await asyncio.wait_for(client.get(f"/v1/profiles/p1/runs/{run_id}/events"), 2)
    assert response.status_code == 200 and response.text == ""
