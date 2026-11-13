"""Tool test runs (`eap.tool_run`): one tool call per durable run; focused implementer tests."""

from __future__ import annotations

import uuid

from dbos import DBOSClient
from sqlalchemy import text

from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, event_rows, wait_for, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.tool_run_helpers import enqueue_tool_run


def _contiguous(events) -> None:
    assert [e[0] for e in events] == list(range(len(events)))
    keys = [e[1] for e in events]
    assert len(keys) == len(set(keys))


def test_side_effecting_tool_run_completes_with_events(worker, client, tenant):
    run_id = enqueue_tool_run(
        client, worker.run_store, tenant_id=tenant, tool_id="tickets.create",
        args={"title": "Printer", "body": "on fire"}, app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert (run["kind"], run["tool_id"], run["agent_id"], run["agent_version"]) == ("tool", "tickets.create", None, None)

    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    assert [(e[1], e[2]) for e in events] == [
        ("start", "run_started"),
        ("tool:tool_run:call", "tool_call"),
        ("tool:tool_run:result", "tool_result"),
        ("end", "run_completed"),
    ]
    assert events[0][3] == {"kind": "tool", "tool_id": "tickets.create", "input": {"title": "Printer", "body": "on fire"}}
    assert events[1][3]["args"] == {"title": "Printer", "body": "on fire"}
    assert events[2][3]["status"] == "ok" and events[2][3]["content"]["created"] is True
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
    with worker.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT key, state FROM side_effects WHERE tenant_id = :t ORDER BY key"), {"t": tenant}
        ).all()
    # The run's guard and tickets.create's own reservation, both completed.
    assert [tuple(r) for r in rows] == [(f"{run_id}:tool", "completed"), (f"{run_id}:tool_run", "completed")]


def test_disallowed_tool_is_refused_in_the_worker_without_a_call(worker, client, tenant, monkeypatch):
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset({"clock.now"}))
    run_id = enqueue_tool_run(
        client, worker.run_store, tenant_id=tenant, tool_id="tickets.create",
        args={"title": "x", "body": "y"}, app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "ToolNotAllowedError: tool 'tickets.create' is not allowed on this worker"
    assert [(e[1], e[2]) for e in event_rows(worker.engine, run_id)] == [("start", "run_started"), ("end", "run_failed")]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 0


def test_crash_after_side_effecting_call_ends_in_needs_attention_without_a_second_call(
    worker, tenant, crash_schema, tmp_path
):
    executor_id = f"toolcrash-{uuid.uuid4().hex[:8]}"
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
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1

        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "needs_attention", (run["error"], (tmp_path / "worker2.log").read_text())
        assert run["error"].startswith("SideEffectOutcomeUnknown: ")
        # Never called twice, although tickets.create honours an idempotency key.
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


def test_worker_clears_an_enqueue_failure_left_on_a_run_dbos_accepted(worker, client, tenant):
    """The API marked the run failed because its enqueue raised, yet DBOS had accepted it: the run
    completes with no error left behind."""
    from eap_run_store import ENQUEUE_FAILED_ERROR

    run_id = uuid.uuid4()
    worker.run_store.create_tool_run(tenant, "clock.now", "{}", run_id)
    assert worker.run_store.mark_enqueue_failed(tenant, run_id)
    assert worker.run_store.get_run(tenant, run_id)["error"] == ENQUEUE_FAILED_ERROR
    enqueue_tool_run(
        client, worker.run_store, tenant_id=tenant, tool_id="clock.now", args={}, app_version=APP_VERSION,
        run_id=run_id, create_row=False,
    )
    # `failed` is already terminal, so wait for the workflow's own end state.
    run = wait_for(
        lambda: (r := worker.run_store.get_run(tenant, run_id)) and r["status"] == "completed" and r,
        timeout=60, what="the accepted run to complete",
    )
    assert run["error"] is None


def test_mark_running_keeps_any_other_error(worker, tenant):
    run_id = uuid.uuid4()
    worker.run_store.create_tool_run(tenant, "clock.now", "{}", run_id)
    worker.run_store.set_status(tenant, run_id, "failed", None, "SomeError: kept")
    worker.run_store.mark_running(tenant, run_id)
    run = worker.run_store.get_run(tenant, run_id)
    assert (run["status"], run["error"]) == ("running", "SomeError: kept")


def test_crash_before_the_end_of_a_rejected_tool_run_keeps_its_error_on_recovery(worker, tenant, crash_schema, tmp_path):
    """eap.check_tool rejects the input (tool_input_invalid); the worker dies right after the `end`
    event is written and before that step is checkpointed (so the run's status is not recorded
    yet). A fresh worker replays the checked-in rejection and finishes with the same error code
    and tool id."""
    executor_id = f"rejcrash-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(
        _env(crash_schema, executor_id, EAP_CRASH_AT="after_event_write", EAP_CRASH_EVENT_KEY="end"),
        tmp_path / "worker1.log",
    )
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_tool_run(
            dbos_client, worker.run_store, tenant_id=tenant, tool_id="tickets.create",
            args={"title": "only a title"}, app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        assert [e[1] for e in event_rows(worker.engine, run_id)] == ["start", "end"]

        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert "Recovering" in (tmp_path / "worker2.log").read_text()
        assert run["status"] == "failed"
        assert run["error"] == "ToolInputInvalid: the input does not match tool 'tickets.create'"
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        assert [(e[1], e[2]) for e in events] == [("start", "run_started"), ("end", "run_failed")]
        assert events[1][3] == {"status": "failed", "error": run["error"], "error_code": "tool_input_invalid"}
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 0
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()
