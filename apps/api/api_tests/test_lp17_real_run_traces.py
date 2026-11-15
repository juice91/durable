"""LP.17: a real-worker trace for a loop and for a foreach, read through GET /runs/{id}/trace.

A real `python -m eap_worker` subprocess runs each workflow against a private DBOS schema (dropped
afterwards); the API in this process enqueues the run and then serves the trace. Each trace's
span tree shape is asserted: the exact set of span kinds, their parents, and their status, from
the real event log the worker wrote.
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

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg

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
        "EAP_DBOS_SYSTEM_SCHEMA": f"eap_test_lp17_{suffix}",
        "EAP_APP_VERSION": f"m1-lp17-{suffix}",
        "EAP_EXECUTOR_ID": f"api-lp17-{suffix}",
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


def _publish_agent(client: TestClient, headers: dict[str, str]) -> str:
    spec = {"name": "Echo", "instructions": "Repeat.", "model": {"provider": "test", "name": "echo"}}
    r = client.post("/api/v1/agents", json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    return agent_id


def _publish_workflow(client: TestClient, headers: dict[str, str], spec: dict) -> str:
    r = client.post("/api/v1/workflows", json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    workflow_id = r.json()["id"]
    assert client.post(f"/api/v1/workflows/{workflow_id}/versions", headers=headers).status_code == 201
    return workflow_id


def _wait_terminal(client: TestClient, run_id: str, headers: dict[str, str], *, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        if run["status"] in ("completed", "failed", "needs_attention"):
            return run
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s: last={run!r}")


def _loop_spec(agent_id: str, iterations: int = 3) -> dict:
    """A 3-iteration loop whose body is one agent step. Exits by max_iterations (`exit`)."""
    return {
        "name": "Loop trace",
        "input_schema": {"type": "object"},
        "start": "lp",
        "steps": [{
            "id": "lp", "type": "loop", "max_iterations": iterations, "on_max_iterations": "exit",
            "until": {"op": "eq", "left": {"$ref": "loop.lp.index"}, "right": iterations + 1},
            "body": {"start": "say", "steps": [
                {"id": "say", "type": "agent", "agent_id": agent_id, "input": {"prompt": "hi"}},
            ]},
        }],
    }


def _foreach_spec(agent_id: str, count: int = 3) -> dict:
    """A foreach of `count` items, each item going through one agent step."""
    return {
        "name": "Foreach trace",
        "input_schema": {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]},
        "start": "each",
        "steps": [{
            "id": "each", "type": "foreach", "items": {"$ref": "input.items"}, "max_items": count,
            "concurrency": 2,
            "body": {"start": "say", "steps": [
                {"id": "say", "type": "agent", "agent_id": agent_id,
                 "input": {"prompt": {"$ref": "loop.each.item"}}},
            ]},
        }],
    }


def _start(client: TestClient, workflow_id: str, input_value: dict, headers: dict[str, str]) -> str:
    run_id = str(uuid.uuid4())
    r = client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={"input": input_value, "run_id": run_id},
        headers=headers,
    )
    assert r.status_code == 202, r.text
    return run_id


def _trace(client: TestClient, run_id: str, headers: dict[str, str]) -> dict:
    r = client.get(f"/api/v1/runs/{run_id}/trace", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_real_loop_runs_trace_shows_one_loop_span_and_one_iteration_span_each_ok(worker_env, monkeypatch):
    with hard_timeout(180):
        for key, value in worker_env.items():
            monkeypatch.setenv(key, value)
        app = create_app(database_url=PG_URL)
        tenant = f"t{uuid.uuid4().hex[:10]}"
        headers = get_auth_header(tenant, "alice")
        engine = app.state.database_engine
        try:
            with TestClient(app) as client:
                agent_id = _publish_agent(client, headers)
                workflow_id = _publish_workflow(client, headers, _loop_spec(agent_id, iterations=3))
                run_id = _start(client, workflow_id, {}, headers)
                run = _wait_terminal(client, run_id, headers)
                assert run["status"] == "completed", run

                body = _trace(client, run_id, headers)
                spans = {s["id"]: s for s in body["spans"]}

                # 1. One run span, one loop step span, three loop_iteration spans — all ok, all parented correctly.
                run_span_id = f"run:{run_id}"
                assert run_span_id in spans, list(spans)[:5]
                loop_span_id = f"{run_id}:step:lp"
                assert loop_span_id in spans and spans[loop_span_id]["kind"] == "loop"
                assert spans[loop_span_id]["parent_id"] == spans[run_span_id]["id"]
                assert spans[loop_span_id]["attributes"]["iterations_completed"] == 3
                assert spans[loop_span_id]["attributes"]["exited"] == "max_iterations"
                assert spans[loop_span_id]["status"] == "ok"

                iteration_ids = [f"{run_id}:loop:lp.{i}" for i in range(3)]
                for ix, iid in enumerate(iteration_ids):
                    assert iid in spans, (iid, list(spans))
                    it = spans[iid]
                    assert it["kind"] == "loop_iteration"
                    assert it["parent_id"] == spans[loop_span_id]["id"]
                    assert it["status"] == "ok"
                    # Each iteration has an agent step span and a child agent run.
                    agent_span = f"{run_id}:step:lp.{ix}~say"
                    assert agent_span in spans, (agent_span, list(spans))
                    assert spans[agent_span]["parent_id"] == it["id"]

                # 2. Exactly one child run per iteration, each 'ok' (agent runs).
                kinds = [s["kind"] for s in body["spans"]]
                assert kinds.count("loop") == 1
                assert kinds.count("loop_iteration") == 3
                assert kinds.count("child_run") == 3
                assert body["truncated"]["iterations"] == 0
        finally:
            cleanup_tenant(engine, tenant)
            engine.dispose()


def test_a_real_foreach_runs_trace_shows_one_foreach_span_and_one_iteration_span_per_item(worker_env, monkeypatch):
    with hard_timeout(180):
        for key, value in worker_env.items():
            monkeypatch.setenv(key, value)
        app = create_app(database_url=PG_URL)
        tenant = f"t{uuid.uuid4().hex[:10]}"
        headers = get_auth_header(tenant, "alice")
        engine = app.state.database_engine
        try:
            with TestClient(app) as client:
                agent_id = _publish_agent(client, headers)
                workflow_id = _publish_workflow(client, headers, _foreach_spec(agent_id, count=3))
                run_id = _start(client, workflow_id, {"items": ["a", "b", "c"]}, headers)
                run = _wait_terminal(client, run_id, headers)
                assert run["status"] == "completed", run

                body = _trace(client, run_id, headers)
                spans = {s["id"]: s for s in body["spans"]}

                run_span_id = f"run:{run_id}"
                foreach_id = f"{run_id}:step:each"
                assert foreach_id in spans and spans[foreach_id]["kind"] == "loop"
                assert spans[foreach_id]["parent_id"] == spans[run_span_id]["id"]
                assert spans[foreach_id]["status"] == "ok"
                assert spans[foreach_id]["attributes"]["iterations_completed"] == 3

                # Three loop_iteration spans under the foreach, one per item, all ok.
                iteration_ids = [f"{run_id}:loop:each.{i}" for i in range(3)]
                for ix, iid in enumerate(iteration_ids):
                    assert iid in spans, (iid, list(spans))
                    it = spans[iid]
                    assert it["kind"] == "loop_iteration"
                    assert it["parent_id"] == spans[foreach_id]["id"]
                    assert it["status"] == "ok"
                    assert f"{run_id}:step:each.{ix}~say" in spans

                kinds = [s["kind"] for s in body["spans"]]
                assert kinds.count("loop") == 1
                assert kinds.count("loop_iteration") == 3
                assert kinds.count("child_run") == 3
                assert body["truncated"]["iterations"] == 0
        finally:
            cleanup_tenant(engine, tenant)
            engine.dispose()
