"""Crash acceptance for workflow runs. Each run finishes once: no duplicate events, one tool
call, one child agent run per agent step. Worker subprocesses are stopped in `finally`."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from dbos import DBOSClient

from worker_tests.conftest import DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_workflow_run_acceptance import (
    _contiguous,
    _keys,
    auth_header,
    child_runs,
    milestone_spec,
    stored_ticket,
    workflow_api,
)
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    demo_spec,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
    wait_event,
)
from eap_worker.workflow_run import child_run_id

import pytest


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _settle(worker, tenant, run_id, *, calls: int, children: list[tuple[str, str, str]]) -> None:
    """A second look after the run is terminal: recovery must not append another call or child."""
    time.sleep(0.4)
    assert worker.tickets.call_count(tenant, f"{run_id}:") == calls
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == calls
    assert child_runs(worker.engine, run_id) == children
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    assert _keys(events).count("end") == 1


def test_kill_mid_step_then_recover_finishes_exactly_once(worker, tenant, defs, crash_schema, tmp_path):
    executor_id = f"wf-mid-{uuid.uuid4().hex[:8]}"
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {
        "name": "Mid",
        "input_schema": {"type": "object"},
        "start": "ask",
        "steps": [
            {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": "ping"}, "next": "t"},
            {
                "id": "t",
                "type": "tool",
                "tool_id": "tickets.create",
                "input": {"title": "T", "body": "B"},
                "next": "done",
            },
            {"id": "done", "type": "agent", "agent_id": agent_id, "input": {"p": {"$ref": "steps.ask.output"}}},
        ],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    first = WorkerProcess(
        _env(crash_schema, executor_id, EAP_CRASH_AT="workflow_mid_step", EAP_CRASH_EVENT_KEY="step:t:started"),
        tmp_path / "w1.log",
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
        wait_event(worker.engine, run_id, "step:t:started")
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 0
        assert child_runs(worker.engine, run_id) == [(child_run_id(str(run_id), "ask"), "ask", "completed")]
        assert "end" not in _keys(event_rows(worker.engine, run_id))
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        assert run["output"] == '"Echo: Echo: ping"'
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        assert [(key, typ) for _s, key, typ, _d in events] == [
            ("start", "run_started"),
            ("step:ask:started", "step_started"),
            ("step:ask:completed", "step_completed"),
            ("step:t:started", "step_started"),
            ("step:t:completed", "step_completed"),
            ("step:done:started", "step_started"),
            ("step:done:completed", "step_completed"),
            ("end", "run_completed"),
        ]
        children = [
            (child_run_id(str(run_id), "ask"), "ask", "completed"),
            (child_run_id(str(run_id), "done"), "done", "completed"),
        ]
        assert child_runs(worker.engine, run_id) == children
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
        _settle(worker, tenant, run_id, calls=1, children=children)
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_kill_after_side_effecting_tool_call_needs_attention_once(
    worker, tenant, defs, crash_schema, tmp_path
):
    executor_id = f"wf-tool-{uuid.uuid4().hex[:8]}"
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {
        "name": "Ticket",
        "input_schema": {"type": "object"},
        "start": "ask",
        "steps": [
            {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": "open"}, "next": "t"},
            {"id": "t", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}},
        ],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    first = WorkerProcess(
        _env(
            crash_schema, executor_id,
            EAP_CRASH_AT="after_workflow_tool_call", EAP_CRASH_EVENT_KEY="step:t:call",
        ),
        tmp_path / "w1.log",
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
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "needs_attention", (run["error"], (tmp_path / "w2.log").read_text())
        assert run["error"].startswith("SideEffectOutcomeUnknown: ")
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        assert [(key, typ) for _s, key, typ, _d in events] == [
            ("start", "run_started"),
            ("step:ask:started", "step_started"),
            ("step:ask:completed", "step_completed"),
            ("step:t:started", "step_started"),
            ("step:t:failed", "step_failed"),
            ("end", "run_failed"),
        ]
        assert events[-2][3]["error_code"] == "side_effect_outcome_unknown"
        assert events[-1][3]["status"] == "needs_attention" and events[-1][3]["step_id"] == "t"
        children = [(child_run_id(str(run_id), "ask"), "ask", "completed")]
        assert child_runs(worker.engine, run_id) == children
        _settle(worker, tenant, run_id, calls=1, children=children)
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_kill_while_waiting_then_approve_via_api_finishes_exactly_once(
    worker, tenant, defs, crash_schema, tmp_path
):
    executor_id = f"wf-wait-{uuid.uuid4().hex[:8]}"
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = milestone_spec(agent_id)
    spec["steps"][1]["input"] = {"title": "T", "body": {"$ref": "input.q"}}
    # milestone_spec's tool id is already tickets.create; keep the urgent branch.
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    first = WorkerProcess(
        _env(crash_schema, executor_id, EAP_CRASH_AT="workflow_waiting", EAP_CRASH_EVENT_KEY="step:approve:waiting"),
        tmp_path / "w1.log",
    )
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
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        assert "step:approve:decision" not in _keys(event_rows(worker.engine, run_id))
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
        second = WorkerProcess(
            _env(crash_schema, executor_id, EAP_APPROVAL_POLL_SECONDS="1"), tmp_path / "w2.log"
        )
        second.wait_ready()
        with workflow_api(crash_schema, CRASH_APP_VERSION) as api:
            approved = api.post(
                f"/api/v1/workflows/runs/{run_id}/steps/approve/approve",
                json={"comment": "go"},
                headers=auth_header(tenant),
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["decision"] == "approved"
            assert approved.json()["decided_by"] == "alice"
            assert approved.json()["comment"] == "go"
            assert isinstance(approved.json()["notified"], bool)
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        assert run["output"] == '"Echo: Echo: urgent: disk"'
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        assert _keys(events).count("end") == 1
        assert _keys(events).count("step:approve:decision") == 1
        assert next(typ for _s, key, typ, _d in events if key == "step:approve:decision") == "step_approved"
        assert "step:done:completed" in _keys(events)
        children = [
            (child_run_id(str(run_id), "ask"), "ask", "completed"),
            (child_run_id(str(run_id), "done"), "done", "completed"),
        ]
        assert child_runs(worker.engine, run_id) == children
        _settle(worker, tenant, run_id, calls=1, children=children)
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_kill_inside_a_parallel_branch_finishes_exactly_once(worker, tenant, defs, crash_schema, tmp_path):
    executor_id = f"wf-par-{uuid.uuid4().hex[:8]}"
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {
        "name": "Fan crash",
        "input_schema": {"type": "object"},
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "join"},
            {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"p": "left"}, "next": "join"},
            {
                "id": "b",
                "type": "tool",
                "tool_id": "tickets.create",
                "input": {"title": "T", "body": "B"},
                "next": "join",
            },
            {
                "id": "join",
                "type": "agent",
                "agent_id": agent_id,
                "input": {"left": {"$ref": "steps.a.output"}, "right": {"$ref": "steps.b.output"}},
            },
        ],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    first = WorkerProcess(
        _env(crash_schema, executor_id, EAP_CRASH_AT="workflow_mid_step", EAP_CRASH_EVENT_KEY="step:b:started"),
        tmp_path / "w1.log",
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
        wait_event(worker.engine, run_id, "step:b:started")
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 0
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        echoed = json.loads(run["output"])
        assert isinstance(echoed, str)
        payload = json.loads(echoed.removeprefix("Echo: "))
        stored = stored_ticket(worker.engine, tenant, f"{run_id}:wf_b")
        assert payload["left"] == "Echo: left"
        assert payload["right"] == {"ticket_id": stored["ticket_id"], "created": True}
        assert stored["title"] == "T" and stored["body"] == "B"
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        keys = _keys(events)
        assert keys.count("end") == 1
        assert keys.count("step:b:started") == 1 and keys.count("step:b:completed") == 1
        assert keys.count("step:a:completed") == 1 and keys.count("step:join:completed") == 1
        fan_started = keys.index("step:fan:started")
        fan_completed = keys.index("step:fan:completed")
        assert fan_started < keys.index("step:b:started") < keys.index("step:b:completed") < fan_completed
        assert fan_started < keys.index("step:a:started") < keys.index("step:a:completed") < fan_completed
        assert fan_completed < keys.index("step:join:started")
        children = [
            (child_run_id(str(run_id), "a"), "a", "completed"),
            (child_run_id(str(run_id), "join"), "join", "completed"),
        ]
        assert child_runs(worker.engine, run_id) == children
        _settle(worker, tenant, run_id, calls=1, children=children)
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_crash_after_child_insert_then_publish_v2_runs_the_child_on_v1(
    worker, tenant, defs, crash_schema, tmp_path
):
    """Killed after the child row names v1, before that step checkpoints. v2 published meanwhile
    must not be what the recovered child runs."""
    executor_id = f"wf-child-{uuid.uuid4().hex[:8]}"
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    spec = {
        "name": "One agent",
        "input_schema": {"type": "object"},
        "start": "ask",
        "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": "hello"}}],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    first = WorkerProcess(
        _env(
            crash_schema, executor_id,
            EAP_CRASH_AT="after_child_run_insert", EAP_CRASH_EVENT_KEY="step:ask:child",
        ),
        tmp_path / "w1.log",
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
        child = child_run_id(str(run_id), "ask")
        assert worker.run_store.get_run(tenant, uuid.UUID(child))["agent_version"] == 1
        assert "end" not in _keys(event_rows(worker.engine, run_id))
        publish_agent(worker.engine, tenant, agent_spec("history"), agent_id=agent_id, version=2)
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        assert run["output"] == '"Echo: hello"'
        assert "Turn 1" not in run["output"]
        assert worker.run_store.get_run(tenant, uuid.UUID(child))["agent_version"] == 1
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        assert [(key, typ) for _s, key, typ, _d in events] == [
            ("start", "run_started"),
            ("step:ask:started", "step_started"),
            ("step:ask:completed", "step_completed"),
            ("end", "run_completed"),
        ]
        assert events[2][3]["output"] == "Echo: hello"
        children = [(child, "ask", "completed")]
        assert child_runs(worker.engine, run_id) == children
        _settle(worker, tenant, run_id, calls=0, children=children)
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_restart_with_shorter_approval_knobs_keeps_the_recorded_deadline(
    worker, tenant, defs, crash_schema, tmp_path
):
    """Killed while waiting under a one-hour timeout. The restarted worker's 0.1 s hour and 0.2 s
    poll must not time the step out; approval still resumes the same path exactly once."""
    executor_id = f"wf-knob-{uuid.uuid4().hex[:8]}"
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    workflow_id, version_id = publish_workflow(worker.engine, tenant, demo_spec(agent_id, timeout_hours=1))
    first = WorkerProcess(
        _env(
            crash_schema, executor_id,
            EAP_WORKFLOW_HOUR_SECONDS="3600", EAP_APPROVAL_POLL_SECONDS="3600",
            EAP_CRASH_AT="workflow_waiting", EAP_CRASH_EVENT_KEY="step:approve:waiting",
        ),
        tmp_path / "w1.log",
    )
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(
            dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
            input_value={"q": "urgent"}, app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        waiting = wait_event(worker.engine, run_id, "step:approve:waiting")
        deadline = waiting["data"]["deadline"]
        assert datetime.fromisoformat(deadline) > datetime.now(timezone.utc) + timedelta(minutes=30)
        second = WorkerProcess(
            _env(
                crash_schema, executor_id,
                EAP_WORKFLOW_HOUR_SECONDS="0.1", EAP_APPROVAL_POLL_SECONDS="0.2",
            ),
            tmp_path / "w2.log",
        )
        second.wait_ready()
        time.sleep(4.0)  # well past a 0.1 s hour and several 0.2 s polls
        again = wait_event(worker.engine, run_id, "step:approve:waiting")
        assert again["data"]["deadline"] == deadline
        assert datetime.fromisoformat(deadline) > datetime.now(timezone.utc) + timedelta(minutes=30)
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        assert "step:approve:decision" not in _keys(event_rows(worker.engine, run_id))
        with workflow_api(crash_schema, CRASH_APP_VERSION) as api:
            approved = api.post(
                f"/api/v1/workflows/runs/{run_id}/steps/approve/approve",
                json={"comment": "go"},
                headers=auth_header(tenant),
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["decision"] == "approved"
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        assert run["output"] == '"Echo: Echo: urgent"'
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        keys = _keys(events)
        assert keys.count("end") == 1 and keys.count("step:approve:decision") == 1
        assert keys.count("step:approve:waiting") == 1
        assert next(typ for _s, key, typ, _d in events if key == "step:approve:decision") == "step_approved"
        assert "step:done:completed" in keys
        children = [
            (child_run_id(str(run_id), "ask"), "ask", "completed"),
            (child_run_id(str(run_id), "done"), "done", "completed"),
        ]
        assert child_runs(worker.engine, run_id) == children
        _settle(worker, tenant, run_id, calls=0, children=children)
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()
