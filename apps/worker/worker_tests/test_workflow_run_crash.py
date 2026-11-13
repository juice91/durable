"""Workflow runs survive a worker kill: while waiting for approval, and between a side-effecting
tool call and its recorded result. Worker subprocesses are always stopped in `finally`."""

from __future__ import annotations

import uuid

import pytest
from dbos import DBOSClient

from worker_tests.conftest import DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    decide,
    demo_spec,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
    wait_event,
)


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _contiguous(events) -> None:
    assert [e[0] for e in events] == list(range(len(events)))
    assert len({e[1] for e in events}) == len(events)


def test_kill_while_waiting_then_approve_finishes_exactly_once(worker, tenant, defs, crash_schema, tmp_path):
    executor_id = f"wf-wait-{uuid.uuid4().hex[:8]}"
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    # tickets.create needs title/body: give the tool step a literal input.
    spec = demo_spec(agent_id, tool_id="tickets.create")
    spec["steps"][1]["input"] = {"title": "T", "body": {"$ref": "input.q"}}
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    first = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_AT="workflow_waiting"), tmp_path / "w1.log")
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(
            dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
            input_value={"q": "urgent: disk"}, app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        wait_event(worker.engine, run_id, "step:approve:waiting")
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        assert decide(worker.event_log, dbos_client, tenant_id=tenant, run_id=run_id, step_id="approve") == "created"
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        assert [k for _, k, _, _ in events].count("end") == 1
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1  # the tool step ran once
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_kill_after_side_effecting_tool_call_needs_attention(worker, tenant, defs, crash_schema, tmp_path):
    executor_id = f"wf-tool-{uuid.uuid4().hex[:8]}"
    spec = {
        "name": "Ticket",
        "input_schema": {"type": "object"},
        "start": "t",
        "steps": [{"id": "t", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}}],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    first = WorkerProcess(
        _env(crash_schema, executor_id, EAP_CRASH_AT="after_workflow_tool_call"), tmp_path / "w1.log"
    )
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(
            dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
            input_value={}, app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "needs_attention", (run["error"], (tmp_path / "w2.log").read_text())
        assert run["error"].startswith("SideEffectOutcomeUnknown: ")
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        assert [(k, t) for _, k, t, _ in events] == [
            ("start", "run_started"), ("step:t:started", "step_started"),
            ("step:t:failed", "step_failed"), ("end", "run_failed"),
        ]
        assert events[2][3]["error_code"] == "side_effect_outcome_unknown"
        assert events[3][3]["status"] == "needs_attention"
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def _recover(crash_schema, executor_id, tmp_path, crash_env: dict[str, str], restart_env: dict[str, str],
             start, after_crash=None, while_recovered=None):
    """Run `start(dbos_client)` on a worker that crashes per `crash_env`, call `after_crash()`, then
    restart with `restart_env` and call `while_recovered(dbos_client)`. Workers stopped in finally."""
    first = WorkerProcess(_env(crash_schema, executor_id, **crash_env), tmp_path / "w1.log")
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        result = start(dbos_client)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        if after_crash is not None:
            after_crash()
        second = WorkerProcess(_env(crash_schema, executor_id, **restart_env), tmp_path / "w2.log")
        second.wait_ready()
        if while_recovered is not None:
            while_recovered(dbos_client)
        return result
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_recovered_agent_step_keeps_the_version_it_resolved(worker, tenant, defs, crash_schema, tmp_path):
    """Killed after the child row (v1) was inserted but before the resolve step's checkpoint; v2 is
    published meanwhile. Recovery must run the child on v1 (its row), never re-resolve 'latest'."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    spec = {"name": "One agent", "input_schema": {"type": "object"}, "start": "ask",
            "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": "hello"}}]}
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    state: dict = {}

    def start(dbos_client):
        state["run_id"] = enqueue_workflow_run(
            dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
            input_value={}, app_version=CRASH_APP_VERSION,
        )

    _recover(
        crash_schema, f"wf-child-{uuid.uuid4().hex[:8]}", tmp_path,
        {"EAP_CRASH_AT": "after_child_run_insert"}, {}, start,
        after_crash=lambda: publish_agent(worker.engine, tenant, agent_spec("history"), agent_id=agent_id, version=2),
        while_recovered=lambda _c: state.update(run=wait_terminal(worker.run_store, tenant, state["run_id"], timeout=90)),
    )
    run = state["run"]
    assert run["status"] == "completed", run["error"]
    assert run["output"] == '"Echo: hello"'  # v1 (test:echo); v2 (test:history) would answer "Turn 1. ..."
    child_id = event_rows(worker.engine, state["run_id"])[1][3]["child_run_id"]
    assert worker.run_store.get_run(tenant, uuid.UUID(child_id))["agent_version"] == 1


def test_restart_with_other_approval_knobs_keeps_the_recorded_deadline(worker, tenant, defs, crash_schema, tmp_path):
    """Killed while waiting (default one-hour timeout); the restarted worker has a 0.1 s 'hour' and a
    0.2 s poll. The run must keep its recorded deadline: no timeout, and approval still resumes it."""
    import time

    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    workflow_id, version_id = publish_workflow(worker.engine, tenant, demo_spec(agent_id))
    state: dict = {}

    def start(dbos_client):
        state["run_id"] = enqueue_workflow_run(
            dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
            input_value={"q": "urgent"}, app_version=CRASH_APP_VERSION,
        )

    def recovered(dbos_client):
        run_id = state["run_id"]
        time.sleep(4.0)  # far past the new knobs' 0.1 s deadline and several 0.2 s polls
        assert worker.event_log.get_event(tenant, run_id, "step:approve:decision") is None
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        assert decide(worker.event_log, dbos_client, tenant_id=tenant, run_id=run_id, step_id="approve") == "created"
        state["run"] = wait_terminal(worker.run_store, tenant, run_id, timeout=90)

    _recover(
        crash_schema, f"wf-knob-{uuid.uuid4().hex[:8]}", tmp_path,
        {"EAP_CRASH_AT": "workflow_waiting"},
        {"EAP_WORKFLOW_HOUR_SECONDS": "0.1", "EAP_APPROVAL_POLL_SECONDS": "0.2"},
        start, while_recovered=recovered,
    )
    assert state["run"]["status"] == "completed", state["run"]["error"]


def test_restart_with_a_smaller_output_cap_keeps_the_recorded_limit(worker, tenant, defs, crash_schema, tmp_path):
    """Killed mid-step; the restarted worker caps step outputs at 1 byte. The run keeps the limits
    recorded when it started and completes."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    workflow_id, version_id = publish_workflow(worker.engine, tenant, demo_spec(agent_id))
    state: dict = {}

    def start(dbos_client):
        state["run_id"] = enqueue_workflow_run(
            dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
            input_value={"q": "calm"}, app_version=CRASH_APP_VERSION,
        )

    _recover(
        crash_schema, f"wf-size-{uuid.uuid4().hex[:8]}", tmp_path,
        {"EAP_CRASH_AT": "workflow_mid_step", "EAP_CRASH_EVENT_KEY": "step:ask:started"},
        {"EAP_WORKFLOW_MAX_STEP_OUTPUT_BYTES": "1", "EAP_WORKFLOW_MAX_CONTEXT_BYTES": "1"},
        start,
        while_recovered=lambda _c: state.update(run=wait_terminal(worker.run_store, tenant, state["run_id"], timeout=90)),
    )
    assert state["run"]["status"] == "completed", state["run"]["error"]
    _contiguous(event_rows(worker.engine, state["run_id"]))
