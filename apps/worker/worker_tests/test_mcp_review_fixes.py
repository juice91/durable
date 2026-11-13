"""Task 3.5 review fixes: MCP replay needs neither the DB nor the key; secrets are scrubbed from
everything an MCP server returns; a tool error is final (no retry, so no second external call)."""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from dbos import DBOSClient
from sqlalchemy import text

from eap_worker.mcp import REDACTED, TOOL_ERROR_MESSAGE
from worker_tests.conftest import DBOS_DB_URL, drop_schema, enqueue_run, event_rows, new_schema, wait_terminal
from worker_tests.mcp_helpers import LocalMcpServer, mcp_spec, register_mcp_server
from worker_tests.test_crash_recovery import APP_VERSION, WorkerProcess, _env
from worker_tests.test_telemetry import telemetry  # noqa: F401 - fixture

SENTINEL = "sntl-review-5c1d9e-secret"
SENTINEL2 = "sntl-apikey-77ab02-secret"


def _stored_text(engine, tenant: str, run_id) -> str:
    """Every durable row of the run, as text: side effects, events, the run row."""
    with engine.connect() as conn:
        rows = [
            conn.execute(text("SELECT key, state, result::text FROM side_effects WHERE tenant_id = :t"), {"t": tenant}).all(),
            conn.execute(text("SELECT data::text FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}).all(),
            conn.execute(text("SELECT output, error, messages::text FROM runs WHERE id = :r"), {"r": str(run_id)}).all(),
        ]
    return repr(rows)


def _side_effects(engine, tenant: str) -> list[tuple[str, dict | None]]:
    with engine.connect() as conn:
        return [(r.state, r.result) for r in conn.execute(
            text("SELECT state, result FROM side_effects WHERE tenant_id = :t"), {"t": tenant})]


@pytest.mark.parametrize("change", ["delete", "disable"])
def test_recovered_reserved_call_needs_no_server_row_or_key(worker, tenant, mcp_env, tmp_path, change):
    """Crash after the MCP call; before recovery the server is deleted/disabled AND the platform key
    is rotated away. Recovery must end needs_attention without touching the server again."""
    schema = new_schema("eap_test_mcp_fix")
    executor_id = f"mcpfix-{uuid.uuid4().hex[:8]}"
    first = second = client = None
    with LocalMcpServer() as server:
        server_id = register_mcp_server(worker.engine, tenant, "orders", server.url,
                                        headers={"Authorization": f"Bearer {SENTINEL}"})
        try:
            first = WorkerProcess(_env(schema, executor_id, EAP_CRASH_AT="after_tool_call"), tmp_path / "w1.log")
            first.wait_ready()
            client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=schema)
            run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "cancel_order")),
                                 input_text=json.dumps({"args": {"order_id": "Z9"}}), app_version=APP_VERSION)
            assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
            assert server.call_count("cancel_order") == 1
            requests_before = len(server.seen_headers)

            with worker.engine.begin() as conn:
                if change == "delete":
                    conn.execute(text("DELETE FROM mcp_servers WHERE id = :s"), {"s": server_id})
                else:
                    conn.execute(text("UPDATE mcp_servers SET enabled = false WHERE id = :s"), {"s": server_id})
            from eap_secrets import generate_key

            second = WorkerProcess(_env(schema, executor_id, EAP_SECRETS_KEY=generate_key()), tmp_path / "w2.log")
            run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
            assert run["status"] == "needs_attention", (run["error"], (tmp_path / "w2.log").read_text())
            assert run["error"].startswith("SideEffectOutcomeUnknown:")
            assert server.call_count("cancel_order") == 1
            assert len(server.seen_headers) == requests_before  # no connection at all on recovery
            assert [s for s, _ in _side_effects(worker.engine, tenant)] == ["reserved"]
            logs = (tmp_path / "w1.log").read_text() + (tmp_path / "w2.log").read_text()
            assert SENTINEL not in logs
        finally:
            for proc in (first, second):
                if proc is not None:
                    proc.stop()
            if client is not None:
                client.destroy()
            drop_schema(schema)


def _run(worker, client, tenant, server, tool: str, headers: dict[str, str]):
    register_mcp_server(worker.engine, tenant, "echo", server.url, headers=headers)
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("echo", tool)),
                         input_text="{}", app_version=APP_VERSION_INPROC)
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


APP_VERSION_INPROC = "m1-test"  # the session worker's app version (conftest.APP_VERSION)


@pytest.mark.parametrize("tool", ["echo_headers", "fail_echo_headers"])
def test_echoed_secrets_never_stored_logged_or_traced(worker, client, tenant, mcp_env, telemetry, caplog, tool):  # noqa: F811
    _provider, finished = telemetry
    caplog.set_level(logging.DEBUG)
    headers = {"Authorization": f"Bearer {SENTINEL}", "X-Api-Key": SENTINEL2}
    with LocalMcpServer(echo_headers=True) as server:
        run_id, run = _run(worker, client, tenant, server, tool, headers)
        # The secrets did reach the server ...
        assert any(h.get("authorization") == f"Bearer {SENTINEL}" and h.get("x-api-key") == SENTINEL2
                   for h in server.seen_headers)
    assert run["status"] == "completed", run["error"]
    assert server.call_count(tool) == 1
    # ... and appear nowhere we store, log or trace.
    stored = _stored_text(worker.engine, tenant, run_id)
    spans, logs = finished()
    traced = repr([(s.name, dict(s.attributes or {}), [(e.name, dict(e.attributes or {})) for e in s.events])
                   for s in spans])
    logged = caplog.text + repr([(r.body, dict(r.attributes or {})) for r in logs])
    for secret in (SENTINEL, SENTINEL2):
        assert secret not in stored
        assert secret not in run["output"] and secret not in str(run["error"])
        assert secret not in traced
        assert secret not in logged
    result = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"][0]
    if tool == "echo_headers":
        assert result["status"] == "ok"
        assert result["content"]["authorization"] == REDACTED
        assert result["content"]["x-api-key"] == REDACTED
    else:
        assert result["status"] == "error"
        assert result["content"] == TOOL_ERROR_MESSAGE
        assert _side_effects(worker.engine, tenant) == [("completed", {"is_error": True, "content": None})]


def test_tool_error_is_final_and_the_server_sees_one_call(worker, client, tenant, mcp_env):
    with LocalMcpServer(failing={"cancel_order"}) as server:
        register_mcp_server(worker.engine, tenant, "orders", server.url)
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "cancel_order")),
                             input_text=json.dumps({"args": {"order_id": "Q1"}}), app_version=APP_VERSION_INPROC)
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        assert server.call_count("cancel_order") == 1
    events = event_rows(worker.engine, run_id)
    assert [e[2] for e in events].count("tool_call") == 1
    result = [e[3] for e in events if e[2] == "tool_result"][0]
    assert (result["status"], result["content"]) == ("error", TOOL_ERROR_MESSAGE)
    with worker.engine.connect() as conn:
        messages = conn.execute(text("SELECT messages FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar()
    parts = [p for m in messages for p in m["parts"]]
    assert not [p for p in parts if p["part_kind"] == "retry-prompt"]
    returns = [p for p in parts if p["part_kind"] == "tool-return"]
    assert [(p["outcome"], p["content"]) for p in returns] == [("failed", TOOL_ERROR_MESSAGE)]
    assert run["output"] == f"MCP error: {TOOL_ERROR_MESSAGE}"
