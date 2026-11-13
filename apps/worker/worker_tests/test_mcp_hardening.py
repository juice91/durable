"""Task 3.5 second review, end to end: a failed MCP step's recorded exception replays after a crash;
a JSON-RPC rejection is a final tool failure; invalid arguments are retried before any call."""

from __future__ import annotations

import json
import uuid

import pytest
from dbos import DBOSClient
from sqlalchemy import text

from eap_worker.mcp import TOOL_ERROR_MESSAGE
from worker_tests.conftest import DBOS_DB_URL, drop_schema, enqueue_run, event_rows, new_schema, wait_terminal
from worker_tests.mcp_helpers import LocalMcpServer, mcp_spec, register_mcp_server
from worker_tests.test_crash_recovery import APP_VERSION, WorkerProcess, _env

APP_VERSION_INPROC = "m1-test"  # the session worker's app version (conftest.APP_VERSION)


def _side_effects(engine, tenant: str) -> list[tuple[str, dict | None]]:
    with engine.connect() as conn:
        return [(r.state, r.result) for r in conn.execute(
            text("SELECT state, result FROM side_effects WHERE tenant_id = :t"), {"t": tenant})]


def test_failed_mcp_step_replays_its_recorded_error_after_a_crash(worker, tenant, mcp_env, tmp_path):
    """get_tools fails with McpToolUnavailable (server disabled); the worker dies right after the
    `end` event is written. Recovery replays the recorded step error (unpickled), so the run ends
    failed with mcp_tool_unavailable, consistent with the end event, never internal_error."""
    schema = new_schema("eap_test_mcp_hard")
    executor_id = f"mcphard-{uuid.uuid4().hex[:8]}"
    first = second = client = None
    with LocalMcpServer() as server:
        register_mcp_server(worker.engine, tenant, "orders", server.url, enabled=False)
        try:
            first = WorkerProcess(
                _env(schema, executor_id, EAP_CRASH_AT="after_event_write", EAP_CRASH_EVENT_KEY="end"),
                tmp_path / "w1.log",
            )
            first.wait_ready()
            client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=schema)
            run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "lookup_order")),
                                 input_text="{}", app_version=APP_VERSION)
            assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
            assert "event_key=end" in (tmp_path / "w1.log").read_text()
            assert worker.run_store.get_run(tenant, run_id)["status"] == "running"

            second = WorkerProcess(_env(schema, executor_id), tmp_path / "w2.log")
            run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
            log2 = (tmp_path / "w2.log").read_text()
            assert run["status"] == "failed", (run["error"], log2)
            assert run["error"] == "McpToolUnavailable: MCP tool 'orders/lookup_order' is unavailable (server_disabled)"
            ends = [e for e in event_rows(worker.engine, run_id) if e[1] == "end"]
            assert len(ends) == 1
            assert ends[0][3]["error_code"] == "mcp_tool_unavailable"
            assert ends[0][3]["error"] == run["error"]
            assert "internal_error" not in log2
            assert server.call_count() == 0
        finally:
            for proc in (first, second):
                if proc is not None:
                    proc.stop()
            if client is not None:
                client.destroy()
            drop_schema(schema)


def test_jsonrpc_rejection_is_a_final_tool_failure(worker, client, tenant, mcp_env):
    with LocalMcpServer(reject={"cancel_order": -32602}) as server:
        register_mcp_server(worker.engine, tenant, "orders", server.url)
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "cancel_order")),
                             input_text=json.dumps({"args": {"order_id": "R1"}}), app_version=APP_VERSION_INPROC)
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        assert server.call_count("cancel_order") == 1
    result = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
    assert [(r["status"], r["content"]) for r in result] == [("error", TOOL_ERROR_MESSAGE)]
    assert _side_effects(worker.engine, tenant) == [("completed", {"is_error": True, "content": None})]
    assert run["output"] == f"MCP error: {TOOL_ERROR_MESSAGE}"


def test_internal_jsonrpc_error_stays_an_unknown_outcome(worker, client, tenant, mcp_env):
    with LocalMcpServer(reject={"cancel_order": -32603}) as server:
        register_mcp_server(worker.engine, tenant, "orders", server.url)
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "cancel_order")),
                             input_text=json.dumps({"args": {"order_id": "R2"}}), app_version=APP_VERSION_INPROC)
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "needs_attention", run["error"]
        assert server.call_count("cancel_order") == 1
    assert [s for s, _ in _side_effects(worker.engine, tenant)] == ["reserved"]


@pytest.mark.parametrize("bad_args", [{"order_id": 5}, {}], ids=["wrong-type", "missing"])
def test_invalid_arguments_are_retried_before_any_call(worker, client, tenant, mcp_env, bad_args):
    """Invalid arguments get a retry prompt (nothing sent); the corrected call reaches the server once."""
    with LocalMcpServer() as server:
        register_mcp_server(worker.engine, tenant, "orders", server.url)
        prompt = {"bad_args": bad_args, "args": {"order_id": "A1"}}
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "lookup_order")),
                             input_text=json.dumps(prompt), app_version=APP_VERSION_INPROC)
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        assert server.calls == [("lookup_order", {"order_id": "A1"})]
    events = event_rows(worker.engine, run_id)
    calls = [e[3] for e in events if e[2] == "tool_call"]
    results = [e[3] for e in events if e[2] == "tool_result"]
    assert [c["args"] for c in calls] == [bad_args, {"order_id": "A1"}]
    assert [r["status"] for r in results] == ["error", "ok"]
    # Only the call that was sent has a side effect.
    assert [s for s, _ in _side_effects(worker.engine, tenant)] == ["completed"]
