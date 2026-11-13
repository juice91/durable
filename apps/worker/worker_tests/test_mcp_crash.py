"""MCP crash recovery (milestone 3, task 3.5)."""

from __future__ import annotations

import json
import uuid

from dbos import DBOSClient
from sqlalchemy import text

from worker_tests.conftest import DBOS_DB_URL, enqueue_run, event_rows, wait_terminal
from worker_tests.mcp_helpers import LocalMcpServer, mcp_spec, register_mcp_server
from worker_tests.test_crash_recovery import APP_VERSION, WorkerProcess, _env, new_schema, drop_schema


def test_mcp_crash_after_tool_call(worker, tenant, mcp_env, tmp_path):
    """EAP_CRASH_AT=after_tool_call during a cancel_order call, then restart the worker.

    Assert:
    - the run ends needs_attention
    - local_mcp_server.call_count("cancel_order") == 1
    - the side-effect row for {run_id}:{tool_call_id} is still 'reserved'
    """
    schema = new_schema("eap_test_mcp_crash")
    executor_id = f"mcp-crash-{uuid.uuid4().hex[:8]}"
    first = second = client = None
    try:
        with LocalMcpServer() as server:
            register_mcp_server(worker.engine, tenant, "orders", server.url)
            first = WorkerProcess(
                _env(schema, executor_id, EAP_CRASH_AT="after_tool_call"),
                tmp_path / "w1.log",
            )
            first.wait_ready()
            client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=schema)
            run_id = enqueue_run(
                client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "cancel_order")),
                input_text=json.dumps({"args": {"order_id": "Z9"}}), app_version=APP_VERSION
            )
            assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
            assert server.call_count("cancel_order") == 1

            second = WorkerProcess(_env(schema, executor_id), tmp_path / "w2.log")
            run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
            assert run["status"] == "needs_attention", (run["error"], (tmp_path / "w2.log").read_text())
            assert run["error"].startswith("SideEffectOutcomeUnknown:")
            assert server.call_count("cancel_order") == 1
            call_ids = [e[3]["tool_call_id"] for e in event_rows(worker.engine, run_id) if e[2] == "tool_call"]
            assert len(call_ids) == 1
            key = f"{run_id}:{call_ids[0]}"
            with worker.engine.connect() as conn:
                state = conn.execute(
                    text("SELECT state FROM side_effects WHERE tenant_id = :t AND key = :k"),
                    {"t": tenant, "k": key},
                ).scalar()
            assert state == "reserved"
    finally:
        for proc in (first, second):
            if proc is not None:
                proc.stop()
        if client is not None:
            client.destroy()
        drop_schema(schema)
