"""Crash recovery tests for tool test runs.

Following tests/recovery and worker_tests/test_crash_recovery.py patterns.
EAP_CRASH_AT=after_tool_run_call with side-effecting tools → needs_attention.
EAP_CRASH_AT=after_tool_run_call with read-only tools → completes.
No duplicate events after recovery.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from dbos import DBOSClient
from sqlalchemy import text

from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.tool_run_helpers import enqueue_tool_run


def _contiguous(events) -> None:
    assert [e[0] for e in events] == list(range(len(events)))
    keys = [e[1] for e in events]
    assert len(keys) == len(set(keys))


def test_crash_after_side_effecting_call_ends_in_needs_attention(worker, tenant, crash_schema, tmp_path):
    """EAP_CRASH_AT=after_tool_run_call with tickets.create → needs_attention, error starts with 'SideEffectOutcomeUnknown: '."""
    executor_id = f"tool-crash-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(
        _env(crash_schema, executor_id, EAP_CRASH_AT="after_tool_run_call"), tmp_path / "worker1.log"
    )
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_tool_run(
            dbos_client, worker.run_store, tenant_id=tenant, tool_id="tickets.create",
            args={"title": "Laptop", "body": "will not boot"}, app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()
        # The tool was called exactly once
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1

        # Recover in fresh process
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "needs_attention", (run["error"], (tmp_path / "worker2.log").read_text())
        assert run["error"].startswith("SideEffectOutcomeUnknown: ")

        # The tool was never called twice
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
        assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1

        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        assert [(e[1], e[2]) for e in events] == [
            ("start", "run_started"),
            ("tool:tool_run:call", "tool_call"),
            ("tool:tool_run:result", "tool_result"),
            ("end", "run_failed"),
        ]
        assert events[2][3]["status"] == "error"
        assert events[2][3]["error_code"] == "side_effect_outcome_unknown"
        assert events[3][3]["status"] == "needs_attention"
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_crash_after_read_only_call_completes(worker, tenant, crash_schema, tmp_path):
    """Same crash with a read-only tool (clock.now) → it completes (re-run is idempotent)."""
    executor_id = f"tool-crash-ro-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(
        _env(crash_schema, executor_id, EAP_CRASH_AT="after_tool_run_call"), tmp_path / "worker1.log"
    )
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_tool_run(
            dbos_client, worker.run_store, tenant_id=tenant, tool_id="clock.now",
            args={}, app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()

        # Recover in fresh process
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "worker2.log").read_text())

        # clock.now returns a bare ISO string, stored as JSON text.
        datetime.fromisoformat(json.loads(run["output"]))

        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        # Should have all four events
        assert [(e[1], e[2]) for e in events] == [
            ("start", "run_started"),
            ("tool:tool_run:call", "tool_call"),
            ("tool:tool_run:result", "tool_result"),
            ("end", "run_completed"),
        ]
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_no_duplicate_events_after_recovery(worker, tenant, crash_schema, tmp_path):
    """No duplicate events after recovery: event_keys are unique and seq is gap-free."""
    executor_id = f"tool-crash-no-dup-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(
        _env(crash_schema, executor_id, EAP_CRASH_AT="after_tool_run_call"), tmp_path / "worker1.log"
    )
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_tool_run(
            dbos_client, worker.run_store, tenant_id=tenant, tool_id="tickets.create",
            args={"title": "Laptop", "body": "will not boot"}, app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()

        # Recover in fresh process
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "needs_attention", (run["error"], (tmp_path / "worker2.log").read_text())

        events = event_rows(worker.engine, run_id)
        # No duplicate event_keys
        event_keys = [e[1] for e in events]
        assert len(event_keys) == len(set(event_keys)), f"Duplicate event keys found: {event_keys}"
        # Gap-free seq
        seqs = [e[0] for e in events]
        assert seqs == list(range(len(seqs))), f"Gap in seq numbers: {seqs}"
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()
