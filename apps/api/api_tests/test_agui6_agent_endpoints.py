"""AGUI.6: the AG-UI agent endpoints. Implementer's focused tests; the acceptance suite adds more.

The fake DBOS client plays the worker: on enqueue it writes a scripted turn (run_started, one
model message, run_completed) and marks the run completed, unless the test holds the run.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.ag_ui.input import derive_agent_run_id, derive_conversation_id
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher
from eap_api.routes.ag_ui import AgUiRouteSettings, get_agui_settings
from eap_api.sse import StreamLimiter, StreamSettings, get_delta_source, get_stream_limiter, get_stream_settings, no_deltas
from eap_api.app import create_app
from eap_run_store import EventLog, RunStore

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, iter_frames, requires_pg
from .test_runs import make_agent

pytestmark = requires_pg


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(90):
        yield


class ScriptedWorker:
    """A DBOS client stand-in: records enqueues and, unless `hold`, completes the turn at once."""

    def __init__(self) -> None:
        self.app: Any = None
        self.calls: list[tuple[str, str]] = []  # (run_id, input)
        self.hold = False
        self.fail_times = 0

    def enqueue(self, options: dict[str, Any], *args: Any) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("system database unreachable")
        run_id, tenant_id, _agent_id, _version, _spec, input_text = args
        self.calls.append((run_id, input_text))
        if not self.hold:
            complete(self.app, tenant_id, run_id, f"echo: {input_text}")


def complete(app, tenant_id: str, run_id: str, answer: str) -> None:
    log = EventLog(app.state.database_engine)
    rid = uuid.UUID(run_id)
    if log.get_event(tenant_id, rid, "end") is not None:
        return
    log.append(tenant_id, rid, "start", "run_started", {})
    log.append(tenant_id, rid, "model:0:final", "model_message", {"model_request": 0, "text": answer})
    log.append(tenant_id, rid, "end", "run_completed", {"output": answer})
    RunStore(app.state.database_engine).set_status(tenant_id, rid, "completed", output=answer)


@pytest.fixture
def worker() -> ScriptedWorker:
    return ScriptedWorker()


@pytest.fixture
def agui_settings() -> list[AgUiRouteSettings]:
    return [AgUiRouteSettings(previous_turn_wait_s=5.0, key_recheck_s=60.0)]


@pytest.fixture
def ag(worker, agui_settings):
    app = create_app(database_url=PG_URL)
    worker.app = app
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "agui6-test"), client=worker
    )
    app.dependency_overrides[get_stream_settings] = lambda: StreamSettings(poll_interval=0.02, heartbeat_interval=0.1)
    app.dependency_overrides[get_delta_source] = lambda: no_deltas
    app.dependency_overrides[get_agui_settings] = lambda: agui_settings[0]
    tenants: list[str] = []
    app.state.test_tenants = tenants
    with TestClient(app) as client:
        yield client, app
    for t in tenants:
        with app.state.database_engine.begin() as conn:
            conn.execute(text("DELETE FROM api_keys WHERE tenant_id = :t"), {"t": t})
        cleanup_tenant(app.state.database_engine, t)
    app.state.database_engine.dispose()


@pytest.fixture
def tenant(ag):
    t = f"t{uuid.uuid4().hex[:10]}"
    ag[1].state.test_tenants.append(t)
    return t


def body(thread: str, run: str, *messages: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "threadId": thread,
        "runId": run,
        "messages": list(messages),
        "tools": [],
        "context": [],
        "state": None,
        "forwardedProps": {},
        **extra,
    }


def user(mid: str, content: str) -> dict[str, Any]:
    return {"id": mid, "role": "user", "content": content}


def post(client: TestClient, agent_id: str, payload: dict[str, Any], headers: dict[str, str]):
    with client.stream("POST", f"/api/v1/ag-ui/agents/{agent_id}", json=payload, headers=headers) as response:
        if response.status_code != 200:
            response.read()
            return response.status_code, response.json(), []
        assert response.headers["content-type"].startswith("text/event-stream")
        return 200, None, list(iter_frames(response, deadline_s=30))


def types(frames) -> list[str]:
    out = []
    for f in frames:
        if f.data is None:
            continue
        t = f.data["type"]
        out.append(f"{t}:{f.data['name']}" if t == "CUSTOM" else t)
    return out


def runs_of(app, tenant: str) -> list[dict[str, Any]]:
    with app.state.database_engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            text("SELECT id, input, turn, conversation_id FROM runs WHERE tenant_id = :t ORDER BY created_at"),
            {"t": tenant},
        ).mappings()]


# --------------------------------------------------------------------------- new runs and D7


def test_new_run_records_hands_off_and_streams(ag, tenant, worker):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    status, _, frames = post(client, agent_id, body("thread-1", "r1", user("u1", "hello")), h)
    assert status == 200
    assert types(frames) == [
        "RUN_STARTED", "CUSTOM:pydanticos.run", "CUSTOM:pydanticos.status",
        "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED",
    ]
    assert frames[0].data["threadId"] == "thread-1" and frames[0].data["runId"] == "r1"
    native = derive_agent_run_id(tenant, agent_id, "thread-1", "r1")
    assert frames[1].data["value"]["run_id"] == native
    assert frames[1].data["value"]["conversation_id"] == derive_conversation_id(tenant, agent_id, "thread-1")
    assert worker.calls == [(native, "hello")]
    assert frames[-1].id == "r1:2"  # the run_completed event's seq, on its last frame only
    assert frames[3].data["messageId"] == "r1-m0"
    conv = RunStore(app.state.database_engine).get_conversation(tenant, frames[1].data["value"]["conversation_id"])
    assert conv["resource_id"] == "alice" and conv["agent_id"] == agent_id


def test_same_run_id_attaches_without_a_second_run_and_relaunches(ag, tenant, worker):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    payload = body("t", "r1", user("u1", "hello"))
    assert post(client, agent_id, payload, h)[0] == 200
    status, _, frames = post(client, agent_id, payload, h)
    assert status == 200
    assert types(frames)[:3] == ["RUN_STARTED", "CUSTOM:pydanticos.run", "MESSAGES_SNAPSHOT"]
    assert frames[2].data["messages"] == [{"id": "u1", "role": "user", "content": "hello"}]
    assert types(frames)[-1] == "RUN_FINISHED"
    assert len(runs_of(app, tenant)) == 1
    assert len(worker.calls) == 2  # the hand-off is sent again (DBOS attaches the same workflow id)


def test_enqueue_failure_is_503_and_the_same_run_id_heals(ag, tenant, worker):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    payload = body("t", "r1", user("u1", "hello"))
    worker.fail_times = 1
    status, detail, _ = post(client, agent_id, payload, h)
    assert status == 503
    status, _, frames = post(client, agent_id, payload, h)
    assert status == 200 and types(frames)[-1] == "RUN_FINISHED"
    assert len(runs_of(app, tenant)) == 1


def test_different_fingerprint_is_409(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    assert post(client, agent_id, body("t", "r1", user("u1", "hello")), h)[0] == 200
    status, detail, _ = post(client, agent_id, body("t", "r1", user("u1", "something else")), h)
    assert status == 409 and detail == {"detail": "runId was already used with a different input"}


def test_resume_continuation_and_malformed_last_event_id(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    payload = body("t", "r1", user("u1", "hello"))
    assert post(client, agent_id, payload, h)[0] == 200
    status, _, frames = post(client, agent_id, payload, {**h, "Last-Event-ID": "r1:0"})
    assert status == 200
    assert types(frames) == [
        "CUSTOM:pydanticos.run", "MESSAGES_SNAPSHOT", "CUSTOM:pydanticos.status",
        "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED",
    ]
    for bad in ("r1", "r2:0", "r1:-2", "r1:x", "r1:3", ":0"):
        status, detail, _ = post(client, agent_id, payload, {**h, "Last-Event-ID": bad})
        assert status == 400, bad
        assert detail == {"detail": "Last-Event-ID must be <runId>:<seq> for this run"}


def test_unknown_run_id_with_last_event_id_is_404_and_records_nothing(ag, tenant, worker):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    status, detail, _ = post(client, agent_id, body("t", "r9", user("u1", "hi")), {**h, "Last-Event-ID": "r9:-1"})
    assert status == 404 and detail == {"detail": "Unknown runId"}
    assert runs_of(app, tenant) == [] and worker.calls == []


def test_get_attach_and_thread_snapshot(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    assert post(client, agent_id, body("t", "r1", user("u1", "hello")), h)[0] == 200
    url = f"/api/v1/ag-ui/agents/{agent_id}/threads/t/runs/r1/stream"
    with client.stream("GET", url, headers=h) as response:
        assert response.status_code == 200
        frames = list(iter_frames(response))
    assert types(frames)[:3] == ["RUN_STARTED", "CUSTOM:pydanticos.run", "MESSAGES_SNAPSHOT"]
    with client.stream("GET", f"{url}?after=1", headers=h) as response:
        frames = list(iter_frames(response))
    assert types(frames)[:2] == ["CUSTOM:pydanticos.run", "MESSAGES_SNAPSHOT"]
    assert types(frames)[-1] == "RUN_FINISHED"
    assert "TEXT_MESSAGE_START" not in types(frames)  # seq 1 (the model message) was held already
    assert client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t/runs/nope/stream", headers=h).status_code == 404
    thread = client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t", headers=h).json()
    assert thread["messages"] == [
        {"id": "u1", "role": "user", "content": "hello"},
        {"id": "r1-m0", "role": "assistant", "content": "echo: hello"},
    ]
    assert thread["activeRunId"] is None and thread["runs"][0]["runId"] == "r1"
    empty = client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/never", headers=h).json()
    assert empty["messages"] == [] and empty["conversationId"] is None


# --------------------------------------------------------------------------- previous turn, dedupe


def test_previous_turn_wait_then_snapshot_then_own_turn(ag, tenant, worker):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    worker.hold = True
    with client.stream("POST", f"/api/v1/ag-ui/agents/{agent_id}", json=body("t", "r1", user("u1", "first")), headers=h) as r:
        assert r.status_code == 200  # closed at once: the turn stays running (held)
    first = derive_agent_run_id(tenant, agent_id, "t", "r1")
    worker.hold = False

    def finish_first() -> None:
        time.sleep(0.4)
        complete(app, tenant, first, "first answer")

    threading.Thread(target=finish_first).start()
    status, _, frames = post(client, agent_id, body("t", "r2", user("u1", "first"), user("u2", "second")), h)
    assert status == 200
    got = types(frames)
    assert got[:2] == ["RUN_STARTED", "CUSTOM:pydanticos.waiting"]
    assert got.count("RUN_STARTED") == 1
    snapshot = next(f.data for f in frames if f.data and f.data["type"] == "MESSAGES_SNAPSHOT")
    assert [m["id"] for m in snapshot["messages"]] == ["u1", "r1-m0", "u2"]
    assert got[-1] == "RUN_FINISHED"
    # The tail re-sent u1 (the client never saw its answer): deduped, the turn runs u2 only.
    assert [r["input"] for r in runs_of(app, tenant)] == ["first", "second"]


def test_previous_turn_wait_times_out(ag, tenant, worker, agui_settings):
    agui_settings[0] = AgUiRouteSettings(previous_turn_wait_s=0.3)
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    worker.hold = True
    with client.stream("POST", f"/api/v1/ag-ui/agents/{agent_id}", json=body("t", "r1", user("u1", "a")), headers=h) as r:
        assert r.status_code == 200  # left running (held)
    status, _, frames = post(client, agent_id, body("t", "r2", user("u2", "b")), h)
    assert types(frames)[0] == "RUN_STARTED" and frames[-1].data["code"] == "previous_turn_running"
    assert len(runs_of(app, tenant)) == 1


def test_regenerate_reruns_the_recorded_user_message(ag, tenant, worker):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    assert post(client, agent_id, body("t", "r1", user("u1", "hello")), h)[0] == 200
    status, _, frames = post(client, agent_id, body("t", "r2", user("u1", "hello")), h)
    assert status == 200 and types(frames)[-1] == "RUN_FINISHED"
    assert [r["input"] for r in runs_of(app, tenant)] == ["hello", "hello"]
    # The regenerate supersedes turn 1 in the thread: one u1, then the latest answer.
    assert "MESSAGES_SNAPSHOT" not in types(frames)  # a fresh run sends no snapshot
    thread = client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t", headers=h).json()
    assert [m["id"] for m in thread["messages"]] == ["u1", "r2-m0"]
    assert [r["superseded"] for r in thread["runs"]] == [True, False]


# --------------------------------------------------------------------------- refusals


def test_archived_agent_refuses_a_new_thread_but_attaches_a_known_run(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    payload = body("t", "r1", user("u1", "hello"))
    assert post(client, agent_id, payload, h)[0] == 200
    assert client.post(f"/api/v1/agents/{agent_id}/archive", headers=h).status_code == 200
    status, detail, _ = post(client, agent_id, body("t2", "r1", user("u1", "hello")), h)
    assert status == 409 and "archived" in detail["detail"]
    assert post(client, agent_id, payload, h)[0] == 200


def test_resource_rule(ag, tenant):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    props = {"pydanticos": {"resourceId": "customer-1"}}
    assert post(client, agent_id, body("t", "r1", user("u1", "a"), forwardedProps=props), h)[0] == 200
    conv = RunStore(app.state.database_engine).get_conversation(tenant, derive_conversation_id(tenant, agent_id, "t"))
    assert conv["resource_id"] == "customer-1"
    other = {"pydanticos": {"resourceId": "customer-2"}}
    status, detail, _ = post(client, agent_id, body("t", "r2", user("u2", "b"), forwardedProps=other), h)
    assert status == 409 and detail["detail"][0]["type"] == "resource_mismatch"


def test_cross_tenant_and_unknown_agent_are_404(ag, tenant):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    other = f"t{uuid.uuid4().hex[:10]}"
    app.state.test_tenants.append(other)
    oh = get_auth_header(other, "bob")
    for target in (agent_id, str(uuid.uuid4()), "not-a-uuid"):
        status, detail, _ = post(client, target, body("t", "r1", user("u1", "a")), oh)
        assert status == 404 and detail == {"detail": "Agent not found"}, target
    assert client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t", headers=oh).status_code == 404


def test_unstorable_string_is_422_and_limiter_is_429(ag, tenant):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    status, detail, _ = post(client, agent_id, body("t", "r1", user("u1", "a\u0000b")), h)
    assert status == 422 and detail["detail"][0]["type"] == "string_unstorable"
    app.dependency_overrides[get_stream_limiter] = lambda: StreamLimiter(max_global=0, max_per_principal=0)
    status, _, _ = post(client, agent_id, body("t", "r1", user("u1", "a")), h)
    assert status == 429
    assert runs_of(app, tenant) == []


# --------------------------------------------------------------------------- API keys


def _key(client, tenant: str, **extra: Any) -> tuple[str, str]:
    r = client.post("/api/v1/api-keys", json={"name": "k", **extra}, headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 201, r.text
    return r.json()["id"], r.json()["key"]


def test_api_key_runs_published_only_and_respects_the_allowlist(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    other_agent = make_agent(client, h)
    _, key = _key(client, tenant, agent_ids=[agent_id])
    kh = {"Authorization": f"Bearer {key}"}
    draft = {"pydanticos": {"version": "draft"}}
    status, detail, _ = post(client, agent_id, body("t", "r1", user("u1", "a"), forwardedProps=draft), kh)
    assert status == 403 and detail["detail"][0]["type"] == "draft_not_allowed"
    model = {"pydanticos": {"model": {"provider": "test", "name": "x"}}}
    status, detail, _ = post(client, agent_id, body("t", "r1", user("u1", "a"), forwardedProps=model), kh)
    assert status == 403 and detail["detail"][0]["type"] == "model_override_not_allowed"
    assert post(client, other_agent, body("t", "r1", user("u1", "a")), kh)[:2] == (404, {"detail": "Agent not found"})
    status, _, frames = post(client, agent_id, body("t", "r1", user("u1", "a")), kh)
    assert status == 200 and types(frames)[-1] == "RUN_FINISHED"


def test_revoked_key_ends_an_open_stream_and_cannot_reattach(ag, tenant, worker, agui_settings):
    agui_settings[0] = AgUiRouteSettings(key_recheck_s=0.2)
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    key_id, key = _key(client, tenant)
    kh = {"Authorization": f"Bearer {key}"}
    worker.hold = True
    seen = []
    with client.stream("POST", f"/api/v1/ag-ui/agents/{agent_id}", json=body("t", "r1", user("u1", "a")), headers=kh) as r:
        assert r.status_code == 200
        for frame in iter_frames(r, deadline_s=20):
            seen.append(frame)
            if len(seen) == 2:
                assert client.delete(f"/api/v1/api-keys/{key_id}", headers=h).status_code == 204
    assert seen[-1].data["type"] == "RUN_ERROR" and seen[-1].data["code"] == "unauthorized"
    url = f"/api/v1/ag-ui/agents/{agent_id}/threads/t/runs/r1/stream"
    assert client.get(url, headers=kh).status_code == 401


def test_tool_results_must_answer_pending_calls_and_client_tools_may_not_shadow(ag, tenant, worker):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    tool_msg = {"id": "tm1", "role": "tool", "toolCallId": "call-1", "content": "42"}
    status, detail, _ = post(client, agent_id, body("t", "r1", user("u1", "a"), tool_msg), h)
    assert status == 422 and detail["detail"][0]["type"] == "client_tool_result_not_pending"
    shadow = [{"name": "tickets__create", "description": "", "parameters": {"type": "object", "properties": {}}}]
    status, detail, _ = post(client, agent_id, body("t", "r1", user("u1", "a"), tools=shadow), h)
    assert status == 422 and detail["detail"][0]["type"] == "client_tool_shadows_server_tool"
    assert runs_of(app, tenant) == [] and worker.calls == []
