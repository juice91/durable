"""Run API: start runs, read runs and events, stream with replay (milestone 1, task R1).

The HTTP tests need Postgres (EAP_TEST_DATABASE_URL) and use a fake DBOSClient inside the real
DBOSRunLauncher, so they check the exact enqueue call of the hand-off contract without DBOS.
The stream-generator tests run everywhere.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher
import eap_api.sse as sse
from eap_api.sse import StreamSettings, get_delta_source, get_stream_settings, no_deltas, run_event_stream
from eap_schemas import RunEvent

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, iter_frames, read_all_frames, requires_pg

SPEC = {
    "name": "Support triage",
    "instructions": "Open a ticket.",
    "model": {"provider": "test", "name": "ticket"},
    "tools": ["tickets.create"],
}


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(60):
        yield


class FakeDBOSClient:
    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[dict[str, Any], tuple[Any, ...]]] = []
        self.fail_times = fail_times

    def enqueue(self, options: dict[str, Any], *args: Any) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("system database unreachable")
        self.calls.append((dict(options), args))


class FakeDeltas:
    """Injected delta source: yields the given payloads once subscribed, then waits forever."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.subscribed: list[str] = []

    @asynccontextmanager
    async def __call__(self, run_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        self.subscribed.append(run_id)

        async def gen() -> AsyncIterator[dict[str, Any]]:
            for p in self.payloads:
                yield p
            await asyncio.Event().wait()

        yield gen()


# --------------------------------------------------------------------------- fixtures (Postgres)


@pytest.fixture
def fake_dbos() -> FakeDBOSClient:
    return FakeDBOSClient()


@pytest.fixture
def stream_settings() -> list[StreamSettings]:
    return [StreamSettings(poll_interval=0.02, heartbeat_interval=30.0)]


@pytest.fixture
def deltas() -> list[Any]:
    return [no_deltas]


@pytest.fixture
def pg(fake_dbos, stream_settings, deltas):
    """(client, app) on Postgres with the fake DBOS client and fast stream settings."""
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake_dbos
    )
    app.dependency_overrides[get_stream_settings] = lambda: stream_settings[0]
    app.dependency_overrides[get_delta_source] = lambda: deltas[0]
    tenants: list[str] = []
    app.state.test_tenants = tenants
    with TestClient(app) as client:
        yield client, app
    for t in tenants:
        cleanup_tenant(app.state.database_engine, t)
    app.state.database_engine.dispose()


@pytest.fixture
def tenant_of(pg):
    def make() -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        pg[1].state.test_tenants.append(t)
        return t, get_auth_header(t, "alice")

    return make


def make_agent(client: TestClient, headers: dict[str, str], publish: bool = True) -> str:
    r = client.post("/api/v1/agents", json={"spec": SPEC}, headers=headers)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    if publish:
        assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    return agent_id


def start(client: TestClient, headers: dict[str, str], agent_id: str, run_id: str | None = None, **body: Any):
    payload = {"run_id": run_id or str(uuid.uuid4()), "input": "printer is on fire", "version": None, **body}
    return client.post(f"/api/v1/agents/{agent_id}/runs", json=payload, headers=headers)


def _tenant_of_run(app, run_id: str) -> str:
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT tenant_id FROM runs WHERE id = :r"), {"r": run_id}).scalar_one()


def seed(app, run_id: str, events: list[tuple[str, str, dict[str, Any]]]) -> None:
    from eap_run_store import EventLog

    log = EventLog(app.state.database_engine)
    tenant_id = _tenant_of_run(app, run_id)
    for key, type_, data in events:
        log.append(tenant_id, uuid.UUID(run_id), key, type_, data)
        # The worker commits the terminal run status right after the end event; the stream holds
        # its terminal frame for that commit, so a seeded end event comes with its status.
        if type_ in ("run_completed", "run_failed"):
            with app.state.database_engine.begin() as conn:
                conn.execute(text("UPDATE runs SET status = :s WHERE id = :i"),
                             {"s": "completed" if type_ == "run_completed" else "failed", "i": run_id})


FULL_RUN = [
    ("start", "run_started", {"input": "printer is on fire"}),
    ("model:0:final", "model_message", {"model_request": 0, "text": ""}),
    ("tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "tickets__create"}),
    ("tool:c1:result", "tool_result", {"tool_call_id": "c1", "status": "ok"}),
    ("end", "run_completed", {"output": "Created ticket TCK-1."}),
]


# --------------------------------------------------------------------------- POST /agents/{id}/runs


@requires_pg
class TestStartRun:
    def test_run_input_is_limited_by_utf8_bytes(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        assert start(client, h, agent_id, input="\u754c" * 10922).status_code == 201
        assert start(client, h, agent_id, input="\u754c" * 10923).status_code == 422

    def test_oversized_enqueue_payload_is_413_without_creating_run(self, pg, tenant_of, monkeypatch):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)

        with app.state.database_engine.connect() as conn:
            spec = conn.execute(
                text("SELECT spec FROM agent_versions WHERE agent_id = :agent_id AND version = 1"),
                {"agent_id": agent_id},
            ).scalar_one()
        spec_bytes = len(json.dumps(spec).encode("utf-8"))
        monkeypatch.setenv("EAP_MAX_RUN_PAYLOAD_BYTES", str(spec_bytes + 4))

        accepted_id = str(uuid.uuid4())
        rejected_id = str(uuid.uuid4())
        assert start(client, h, agent_id, accepted_id, input="1234").status_code == 201
        response = start(client, h, agent_id, rejected_id, input="12345")
        assert response.status_code == 413
        assert "payload" in response.json()["detail"].lower()
        with app.state.database_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM runs WHERE id = :id"), {"id": rejected_id}).scalar_one() == 0

    def test_created_and_enqueued_exactly_per_contract(self, pg, tenant_of, fake_dbos):
        client, _ = pg
        tenant, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        r = start(client, h, agent_id, run_id)
        assert r.status_code == 201, r.text
        assert r.json() == {
            "run_id": run_id, "status": "queued", "display_status": "queued", "agent_id": agent_id, "version": 1,
            "trace_id": None,  # telemetry off in tests (no OTEL_EXPORTER_OTLP_ENDPOINT)
            # Run options (P0.8): the effective model is recorded at insert.
            "draft": False, "model_id": "test:ticket", "model_overridden": False,
        }
        assert len(fake_dbos.calls) == 1
        options, args = fake_dbos.calls[0]
        assert options == {
            "queue_name": "eap-agent-runs",
            "workflow_name": "eap.agent_run",
            "workflow_id": run_id,
            "app_version": "m1-test",
        }
        assert args[:4] == (run_id, tenant, agent_id, 1)
        assert isinstance(args[4], str) and json.loads(args[4])["model"] == {"provider": "test", "name": "ticket"}
        assert args[5] == "printer is on fire"

    def test_retry_same_run_id_is_200_one_row_and_reenqueued(self, pg, tenant_of, fake_dbos):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id).status_code == 201
        r = start(client, h, agent_id, run_id)
        assert r.status_code == 200 and r.json()["run_id"] == run_id
        with app.state.database_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM runs WHERE id = :r"), {"r": run_id}).scalar_one() == 1
        assert [c[0]["workflow_id"] for c in fake_dbos.calls] == [run_id, run_id]
        assert fake_dbos.calls[0][1] == fake_dbos.calls[1][1]

    @pytest.mark.parametrize("fake_dbos", [FakeDBOSClient(fail_times=1)])
    def test_enqueue_failure_is_503_and_retry_heals(self, pg, tenant_of, fake_dbos):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id).status_code == 503
        r = start(client, h, agent_id, run_id)
        assert r.status_code == 200
        assert [c[0]["workflow_id"] for c in fake_dbos.calls] == [run_id]

    def test_latest_version_when_null_and_explicit_version(self, pg, tenant_of, fake_dbos):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        client.patch(f"/api/v1/agents/{agent_id}", json={"spec": {**SPEC, "instructions": "v2"}}, headers=h)
        assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201
        assert start(client, h, agent_id).json()["version"] == 2
        assert start(client, h, agent_id, version=1).json()["version"] == 1
        assert start(client, h, agent_id, version=9).status_code == 404

    def test_other_tenants_agent_is_404(self, pg, tenant_of, fake_dbos):
        client, _ = pg
        _, h1 = tenant_of()
        _, h2 = tenant_of()
        agent_id = make_agent(client, h1)
        assert start(client, h2, agent_id).status_code == 404
        assert start(client, h2, str(uuid.uuid4())).status_code == 404
        assert fake_dbos.calls == []

    def test_no_published_version_is_409(self, pg, tenant_of, fake_dbos):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, publish=False)
        assert start(client, h, agent_id).status_code == 409
        assert fake_dbos.calls == []

    def test_run_id_of_another_tenant_is_409(self, pg, tenant_of):
        client, _ = pg
        _, h1 = tenant_of()
        _, h2 = tenant_of()
        run_id = str(uuid.uuid4())
        assert start(client, h1, make_agent(client, h1), run_id).status_code == 201
        assert start(client, h2, make_agent(client, h2), run_id).status_code == 409

    def test_same_run_id_with_different_request_is_409(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id).status_code == 201
        assert start(client, h, agent_id, run_id, input="something else").status_code == 409


# --------------------------------------------------------------------------- GET run / events


@requires_pg
class TestReadRun:
    def test_get_run_and_tenant_scoping(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        _, other = tenant_of()
        agent_id = make_agent(client, h)
        run_id = start(client, h, agent_id).json()["run_id"]
        r = client.get(f"/api/v1/runs/{run_id}", headers=h)
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] == run_id and body["agent_id"] == agent_id and body["version"] == 1
        assert body["status"] == "queued" and body["display_status"] == "queued"
        assert body["input"] == "printer is on fire"
        assert client.get(f"/api/v1/runs/{run_id}", headers=other).status_code == 404
        assert client.get(f"/api/v1/runs/{uuid.uuid4()}", headers=h).status_code == 404

    def test_events_after_seq(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        _, other = tenant_of()
        run_id = start(client, h, make_agent(client, h)).json()["run_id"]
        seed(app, run_id, FULL_RUN)
        all_events = client.get(f"/api/v1/runs/{run_id}/events", headers=h).json()
        assert [e["seq"] for e in all_events] == [0, 1, 2, 3, 4]
        assert set(all_events[0]) == {"seq", "type", "data", "ts"}
        assert all_events[-1]["type"] == "run_completed"
        tail = client.get(f"/api/v1/runs/{run_id}/events?after_seq=2", headers=h).json()
        assert [(e["seq"], e["type"]) for e in tail] == [(3, "tool_result"), (4, "run_completed")]
        assert client.get(f"/api/v1/runs/{run_id}/events", headers=other).status_code == 404


# --------------------------------------------------------------------------- SSE over HTTP


@requires_pg
class TestStream:
    def test_replays_with_ids_and_closes_after_run_completed(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        run_id = start(client, h, make_agent(client, h)).json()["run_id"]
        seed(app, run_id, FULL_RUN)
        frames = read_all_frames(client, f"/api/v1/runs/{run_id}/stream", h)
        assert frames[0].event == "status" and frames[0].data["display_status"] == "completed"
        events = [f for f in frames if f.event == "run_event"]
        assert [f.id for f in events] == ["0", "1", "2", "3", "4"]
        assert [f.data["seq"] for f in events] == [0, 1, 2, 3, 4]
        assert frames[-1].event == "run_event" and frames[-1].data["type"] == "run_completed"

    def test_last_event_id_wins_over_after_seq(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        run_id = start(client, h, make_agent(client, h)).json()["run_id"]
        seed(app, run_id, FULL_RUN)
        url = f"/api/v1/runs/{run_id}/stream"
        by_query = read_all_frames(client, url + "?after_seq=3", h)
        assert [f.id for f in by_query if f.event == "run_event"] == ["4"]
        by_header = read_all_frames(client, url + "?after_seq=0", {**h, "Last-Event-ID": "2"})
        assert [f.id for f in by_header if f.event == "run_event"] == ["3", "4"]

    @pytest.mark.parametrize("stream_settings", [[StreamSettings(poll_interval=0.02, heartbeat_interval=0.05)]])
    def test_live_events_deltas_heartbeat_then_close(self, pg, tenant_of, deltas):
        client, app = pg
        _, h = tenant_of()
        run_id = start(client, h, make_agent(client, h)).json()["run_id"]
        delta = {"run_id": run_id, "model_request": 0, "attempt": "a1", "delta": "Hel"}
        deltas[0] = FakeDeltas([{**delta, "run_id": str(uuid.uuid4()), "delta": "not mine"}, delta])
        seed(app, run_id, FULL_RUN[:1])
        seen: list[Any] = []
        written = False
        with client.stream("GET", f"/api/v1/runs/{run_id}/stream", headers=h) as response:
            assert response.status_code == 200
            for frame in iter_frames(response):
                seen.append(frame)
                got_delta = any(f.event == "delta" for f in seen)
                got_heartbeat = any("heartbeat" in f.comments for f in seen)
                if not written and got_delta and got_heartbeat:
                    seed(app, run_id, FULL_RUN[1:])  # live: written while the stream is open
                    written = True
        deltas_seen = [f.data for f in seen if f.event == "delta"]
        assert deltas_seen and all(d == delta for d in deltas_seen)
        assert any("heartbeat" in f.comments for f in seen)
        assert [f.id for f in seen if f.event == "run_event"] == ["0", "1", "2", "3", "4"]
        assert all(f.id is None for f in seen if f.event in ("delta", "status"))
        assert seen[-1].data["type"] == "run_completed"

    def test_status_frame_on_change_and_close_on_terminal_status(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        run_id = start(client, h, make_agent(client, h)).json()["run_id"]
        seed(app, run_id, FULL_RUN)
        from eap_run_store import RunStore

        RunStore(app.state.database_engine).finish_with_end(_tenant_of_run(app, run_id), uuid.UUID(run_id), "completed", "done")
        # Resuming after the final event: only a status frame, then the stream ends.
        frames = read_all_frames(client, f"/api/v1/runs/{run_id}/stream", {**h, "Last-Event-ID": "4"})
        assert [(f.event, f.data["display_status"]) for f in frames] == [("status", "completed")]

    def test_cross_tenant_and_bad_last_event_id(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        _, other = tenant_of()
        run_id = start(client, h, make_agent(client, h)).json()["run_id"]
        assert client.get(f"/api/v1/runs/{run_id}/stream", headers=other).status_code == 404
        assert client.get(f"/api/v1/runs/{uuid.uuid4()}/stream", headers=h).status_code == 404
        r = client.get(f"/api/v1/runs/{run_id}/stream", headers={**h, "Last-Event-ID": "x"})
        assert r.status_code == 400


# --------------------------------------------------------------------------- everywhere


def _event(seq: int, type_: str) -> RunEvent:
    return RunEvent(run_id=uuid.uuid4(), seq=seq, type=type_, data={}, ts=datetime.now(timezone.utc))


def _collect(gen: AsyncIterator[str], timeout: float = 5.0) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in gen]

    return asyncio.run(asyncio.wait_for(run(), timeout))


class TestStreamGenerator:
    """The generator alone, with fake loaders: fast, and bounded by asyncio.wait_for."""

    def test_heartbeat_delta_and_termination(self):
        events = [_event(0, "run_started")]
        polls = {"n": 0}
        run = {"id": "r1", "status": "running", "display_status": "running"}

        def load_events(after: int) -> list[RunEvent]:
            polls["n"] += 1
            if polls["n"] == 8:
                events.extend([_event(1, "model_message"), _event(2, "run_failed"), _event(3, "tool_call")])
                run.update(status="failed", display_status="failed")  # the status commit follows the end event
            return [e for e in events if e.seq > after]

        chunks = _collect(
            run_event_stream(
                run_id="r1",
                after_seq=-1,
                load_events=load_events,
                load_run=lambda: run,
                delta_source=FakeDeltas([{"run_id": "r1", "model_request": 0, "attempt": "a", "delta": "x"}]),
                settings=StreamSettings(poll_interval=0.02, heartbeat_interval=0.03),
            )
        )
        text_ = "".join(chunks)
        assert chunks[0].startswith("event: status\n")
        assert ": heartbeat\n\n" in chunks
        assert 'event: delta\ndata: {"run_id":"r1","model_request":0,"attempt":"a","delta":"x"}\n\n' in text_
        assert text_.count("event: run_event") == 3  # stops at run_failed; seq 3 is never sent
        assert chunks[-1].startswith("id: 2\nevent: run_event\n")

    def test_terminal_status_without_end_event_closes(self):
        run = {"id": "r1", "status": "failed", "display_status": "failed"}
        chunks = _collect(
            run_event_stream(
                run_id="r1", after_seq=-1, load_events=lambda after: [], load_run=lambda: run,
                delta_source=no_deltas, settings=StreamSettings(0.01, 0.01),
            )
        )
        assert len(chunks) == 1 and chunks[0].startswith("event: status\n")

    def test_maximum_lifetime_closes_nonterminal_stream_cleanly(self):
        run = {"id": "r1", "status": "running", "display_status": "running"}
        started = time.monotonic()
        chunks = _collect(
            run_event_stream(
                run_id="r1", after_seq=-1, load_events=lambda after: [], load_run=lambda: run,
                delta_source=no_deltas,
                settings=StreamSettings(poll_interval=0.01, heartbeat_interval=30.0, max_lifetime=0.04),
            )
        )
        assert len(chunks) == 1 and chunks[0].startswith("event: status\n")
        assert time.monotonic() - started < 0.5


def test_stream_limiter_enforces_caps_and_releases_slots():
    limiter_type = getattr(sse, "StreamLimiter", None)
    assert limiter_type is not None, "SSE admission limiter is missing"
    limiter = limiter_type(max_global=2, max_per_principal=1)

    first = limiter.acquire("tenant:alice")
    assert first is not None
    assert limiter.acquire("tenant:alice") is None
    second = limiter.acquire("tenant:bob")
    assert second is not None
    assert limiter.acquire("tenant:carol") is None
    first.release()
    replacement = limiter.acquire("tenant:alice")
    assert replacement is not None
    replacement.release()
    second.release()
    assert limiter.active == 0


def test_stream_slot_is_released_on_end_and_disconnect():
    async def scenario():
        limiter = sse.StreamLimiter(max_global=1, max_per_principal=1)

        async def finite():
            yield "done"

        lease = limiter.acquire("tenant:alice")
        assert [chunk async for chunk in sse.release_stream_slot(finite(), lease)] == ["done"]
        assert limiter.active == 0

        async def ongoing():
            yield "first"
            await asyncio.Event().wait()

        lease = limiter.acquire("tenant:alice")
        stream = sse.release_stream_slot(ongoing(), lease)
        assert await anext(stream) == "first"
        await stream.aclose()
        assert limiter.active == 0

    asyncio.run(scenario())


@requires_pg
@pytest.mark.parametrize(
    ("max_global", "max_per_principal", "holder"),
    [(2, 1, "same"), (1, 10, "other")],
)
def test_stream_caps_return_429_before_opening_stream(
    pg, tenant_of, max_global, max_per_principal, holder
):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    run_id = start(client, h, agent_id).json()["run_id"]
    limiter_type = getattr(sse, "StreamLimiter", None)
    assert limiter_type is not None, "SSE admission limiter is missing"
    app.state.stream_limiter = limiter_type(
        max_global=max_global, max_per_principal=max_per_principal
    )
    principal = h["Authorization"].removeprefix("Bearer dev:")
    held = app.state.stream_limiter.acquire(principal if holder == "same" else "other:bob")
    try:
        response = client.get(f"/api/v1/runs/{run_id}/stream", headers=h)
        assert response.status_code == 429
        assert "stream" in response.json()["detail"].lower()
    finally:
        held.release()


def test_concurrent_delta_subscribers_share_one_connection_and_route_by_run(monkeypatch):
    class Notify:
        def __init__(self, payload):
            self.payload = payload

    class FakeConnection:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def execute(self, statement):
            assert statement == "LISTEN eap_run_deltas"

        async def close(self):
            pass

        async def notifies(self):
            while True:
                yield await self.queue.get()

    connections = []

    class AsyncConnection:
        @staticmethod
        async def connect(conninfo, autocommit):
            connection = FakeConnection()
            connections.append(connection)
            return connection

    monkeypatch.setitem(sys.modules, "psycopg", type("Psycopg", (), {"AsyncConnection": AsyncConnection}))

    async def scenario():
        source = sse.PostgresDeltaSource("postgresql://unused")
        async with source("run-a") as a, source("run-b") as b:
            for _ in range(100):
                if connections:
                    break
                await asyncio.sleep(0.001)
            assert len(connections) == 1
            await connections[0].queue.put(Notify(json.dumps({"run_id": "run-a", "delta": "A"})))
            assert await asyncio.wait_for(anext(a), 0.2) == {"run_id": "run-a", "delta": "A"}
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(anext(b), 0.02)
        await source.close()

    asyncio.run(scenario())
