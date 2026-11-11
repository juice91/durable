"""End to end: API -> DBOS queue -> real `python -m eap_worker` subprocess -> events -> SSE.

Needs the dev Postgres (EAP_TEST_DATABASE_URL for eap_app, EAP_TEST_DBOS_URL or the dev default for
the DBOS system DB). The worker gets a private DBOS system schema and app version, so it never
recovers or dequeues anything but this test's run; both are removed afterwards.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from eap_api.app import create_app
from eap_api.sse import StreamSettings, get_stream_settings

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, read_all_frames, requires_pg

DBOS_URL = os.environ.get("EAP_TEST_DBOS_URL", "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos")
REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = requires_pg


def _drop_schema(schema: str) -> None:
    eng = create_engine(DBOS_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with eng.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        eng.dispose()


@pytest.fixture
def worker_env() -> Iterator[dict[str, str]]:
    """Start `python -m eap_worker` on a private schema; yield the env the API must share with it."""
    suffix = uuid.uuid4().hex[:10]
    env = {
        "EAP_DATABASE_URL": PG_URL or "",
        "EAP_DBOS_URL": DBOS_URL,
        "EAP_DBOS_SYSTEM_SCHEMA": f"eap_test_api_{suffix}",
        "EAP_APP_VERSION": f"m1-e2e-{suffix}",
        "EAP_EXECUTOR_ID": f"api-e2e-{suffix}",
        "EAP_QUEUE_POLL_SECONDS": "0.2",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "eap_worker"],
        cwd=REPO_ROOT,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    output: list[str] = []

    def drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            output.append(line)
            lines.put(line)

    threading.Thread(target=drain, daemon=True).start()
    try:
        deadline = time.monotonic() + 60
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or proc.poll() is not None:
                raise AssertionError("worker did not become ready:\n" + "".join(output[-40:]))
            try:
                if lines.get(timeout=min(remaining, 0.5)).strip() == "EAP_WORKER_READY":
                    break
            except queue.Empty:
                continue
        yield env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        _drop_schema(env["EAP_DBOS_SYSTEM_SCHEMA"])


def _cleanup_tickets(engine, run_id: str) -> None:
    with engine.begin() as conn:
        for table in ("fake_external_tickets", "fake_external_ticket_calls"):
            exists = conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar()
            if exists:
                conn.execute(text(f"DELETE FROM {table} WHERE idempotency_key LIKE :p"), {"p": f"{run_id}:%"})


def test_run_through_real_worker_and_stream(worker_env, monkeypatch):
    with hard_timeout(150):
        for key, value in worker_env.items():
            monkeypatch.setenv(key, value)
        app = create_app(database_url=PG_URL)
        app.dependency_overrides[get_stream_settings] = lambda: StreamSettings(poll_interval=0.1, heartbeat_interval=10.0)
        tenant = f"t{uuid.uuid4().hex[:10]}"
        h = get_auth_header(tenant, "alice")
        run_id = str(uuid.uuid4())
        engine = app.state.database_engine
        try:
            with TestClient(app) as client:
                spec = {
                    "name": "Support triage",
                    "instructions": "Open a ticket for the user's problem, then confirm it.",
                    "model": {"provider": "test", "name": "ticket"},
                    "tools": ["tickets.create"],
                }
                agent_id = client.post("/api/v1/agents", json={"spec": spec}, headers=h).json()["id"]
                assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201

                body = {"run_id": run_id, "input": "The printer is on fire", "version": None}
                assert client.post(f"/api/v1/agents/{agent_id}/runs", json=body, headers=h).status_code == 201
                # A client retry re-enqueues the same workflow id; DBOS attaches instead of erroring.
                assert client.post(f"/api/v1/agents/{agent_id}/runs", json=body, headers=h).status_code == 200

                url = f"/api/v1/runs/{run_id}/stream"
                frames = read_all_frames(client, url, h, deadline_s=90)
                events = [f for f in frames if f.event == "run_event"]
                assert [int(f.id) for f in events] == list(range(len(events)))
                assert [f.data["seq"] for f in events] == list(range(len(events)))
                types = [f.data["type"] for f in events]
                assert types[0] == "run_started" and types[-1] == "run_completed", types
                assert "tool_call" in types and "tool_result" in types
                assert frames[-1] is events[-1]
                assert "ticket" in events[-1].data["data"]["output"].lower()

                # The end event is written before the terminal status; allow the status step to land.
                deadline = time.monotonic() + 30
                while (run := client.get(f"/api/v1/runs/{run_id}", headers=h).json())["status"] != "completed":
                    assert time.monotonic() < deadline, run
                    time.sleep(0.1)
                assert run["display_status"] == "completed"

                replay = read_all_frames(client, url, {**h, "Last-Event-ID": "1"}, deadline_s=30)
                replayed = [f for f in replay if f.event == "run_event"]
                assert [int(f.id) for f in replayed] == list(range(2, len(events)))
                assert [f.data for f in replayed] == [f.data for f in events[2:]]
        finally:
            _cleanup_tickets(engine, run_id)
            cleanup_tenant(engine, tenant)
            engine.dispose()
