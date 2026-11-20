"""Real Postgres + DBOS fixtures for worker tests.

Each test session uses its own private DBOS system schema (dropped at the end), so it never sees
or recovers workflows from other sessions, the dev worker, or the API's default schema.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text

APP_DB_URL = os.environ.get(
    "EAP_TEST_DATABASE_URL",
    os.environ.get("EAP_DATABASE_URL", "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"),
)
DBOS_DB_URL = os.environ.get("EAP_TEST_DBOS_URL", "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos")
APP_VERSION = "m1-test"
# Usage cost (P0.1): price test:* at fixed non-zero values, so cost arithmetic is checkable. Set at
# import, so subprocess workers started by tests inherit it.
MODEL_PRICES_TEST_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "config", "model-prices.test.json"
)
os.environ.setdefault("EAP_MODEL_PRICES_FILE", os.path.abspath(MODEL_PRICES_TEST_FILE))
# The in-process worker must never schedule the hourly registry mirror (it would reach the real registry).
os.environ.setdefault("EAP_MCP_CATALOG_SCHEDULE", "off")
# ... nor sweep managed MCP runtimes (it would build the operator adapter and look for a cluster).
os.environ.setdefault("EAP_MCP_RUNTIME_SWEEP_SCHEDULE", "off")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "requires_k3s: needs the local colima-pydanticos k3s with the ToolHive operator"
    )


def new_schema(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def drop_schema(schema: str) -> None:
    eng = create_engine(DBOS_DB_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with eng.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        eng.dispose()


def wait_for(predicate: Callable[[], Any], timeout: float = 60.0, interval: float = 0.2, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what or predicate}")


@pytest.fixture(scope="session")
def worker_settings():
    from eap_worker.config import WorkerSettings

    schema = new_schema("eap_test_worker")
    settings = WorkerSettings(
        app_database_url=APP_DB_URL,
        dbos_system_database_url=DBOS_DB_URL,
        dbos_system_schema=schema,
        app_version=APP_VERSION,
        executor_id=f"test-{uuid.uuid4().hex[:8]}",
        allowed_tools=None,
        litellm_base_url=None,
        litellm_api_key=None,
    )
    yield settings
    drop_schema(schema)


@pytest.fixture(scope="session")
def worker(worker_settings) -> Iterator[Any]:
    """An in-process worker: DBOS launched in this pytest process against the private schema."""
    os.environ["EAP_QUEUE_POLL_SECONDS"] = "0.2"
    from dbos import DBOS

    from eap_worker.app import launch
    from eap_worker.runtime import get_runtime

    launch(worker_settings)
    yield get_runtime()
    DBOS.destroy(destroy_registry=False)


@pytest.fixture(scope="session")
def client(worker, worker_settings):
    from dbos import DBOSClient

    c = DBOSClient(
        system_database_url=worker_settings.dbos_system_database_url,
        dbos_system_schema=worker_settings.dbos_system_schema,
    )
    yield c
    c.destroy()


@pytest.fixture
def tenant(worker) -> Iterator[str]:
    """A fresh tenant per test; its runs, events, side effects and fake tickets are deleted after."""
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    yield tenant_id
    cleanup_tenant(worker.engine, tenant_id)


def cleanup_tenant(engine, tenant_id: str) -> None:
    with engine.begin() as conn:
        # FOR UPDATE: the run-row lock a model step's in-step usage write takes (LP.8), so a late
        # write of a cancelled run waits, then finds the run gone (no FK race with the deletes).
        run_ids = [
            r[0]
            for r in conn.execute(text("SELECT id FROM runs WHERE tenant_id = :t FOR UPDATE"), {"t": tenant_id})
        ]
        for run_id in run_ids:
            conn.execute(text("DELETE FROM execution_events WHERE run_id = :r"), {"r": run_id})
        conn.execute(text("DELETE FROM fake_external_tickets WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM fake_external_ticket_calls WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM spans WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM side_effects WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM mcp_server_secrets WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM mcp_servers WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def mcp_env(monkeypatch) -> str:
    """MCP tool runs against a local server: a fresh platform secrets key and 127.0.0.1 on the dev
    allowlist (the in-process worker reads both from the environment inside each tool step).
    Returns the key, for a worker subprocess's environment."""
    from eap_secrets import KEY_ENV, OLD_KEYS_ENV, generate_key
    from eap_secrets.outbound import DEV_ALLOW_HOSTS_ENV

    key = generate_key()
    monkeypatch.setenv(KEY_ENV, key)
    monkeypatch.delenv(OLD_KEYS_ENV, raising=False)
    monkeypatch.setenv(DEV_ALLOW_HOSTS_ENV, "127.0.0.1")
    return key


@pytest.fixture
def local_mcp_server():
    """A running LocalMcpServer (worker_tests.mcp_helpers); `.url`, `.call_count(tool)`, `.seen_headers`."""
    from worker_tests.mcp_helpers import LocalMcpServer

    with LocalMcpServer() as server:
        yield server


def ticket_spec(tools: tuple[str, ...] = ("tickets.create",), model: str = "ticket") -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": "Support triage",
        "instructions": "Open a ticket for the user's problem, then confirm it.",
        "model": {"provider": "test", "name": model},
        "tools": list(tools),
        "model_settings": {},
    }


def enqueue_run(client, run_store, *, tenant_id: str, spec: dict[str, Any], input_text: str, app_version: str,
                run_id: uuid.UUID | None = None, agent_id: uuid.UUID | None = None, version: int = 1,
                create_row: bool = True) -> uuid.UUID:
    """Do what the API does per the hand-off contract: insert the runs row, then enqueue."""
    import json

    run_id = run_id or uuid.uuid4()
    agent_id = agent_id or uuid.uuid4()
    if create_row:
        run_store.create_run(tenant_id, agent_id, version, input_text, run_id)
    client.enqueue(
        {
            "queue_name": "eap-agent-runs",
            "workflow_name": "eap.agent_run",
            "workflow_id": str(run_id),
            "app_version": app_version,
        },
        str(run_id),
        tenant_id,
        str(agent_id),
        version,
        json.dumps(spec),
        input_text,
    )
    return run_id


def wait_terminal(run_store, tenant_id: str, run_id, timeout: float = 60.0) -> dict[str, Any]:
    terminal = {"completed", "skipped", "failed", "needs_attention"}

    def done():
        run = run_store.get_run(tenant_id, run_id)
        return run if run and run["status"] in terminal else None

    return wait_for(done, timeout=timeout, what=f"run {run_id} to finish")


def event_rows(engine, run_id) -> list[tuple[int, str, str, dict]]:
    with engine.connect() as conn:
        return [
            (r.seq, r.event_key, r.type, r.data)
            for r in conn.execute(
                text("SELECT seq, event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq"),
                {"r": str(run_id)},
            )
        ]
