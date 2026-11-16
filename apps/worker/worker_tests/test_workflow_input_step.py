"""W2.1: an input step waits with its resume_schema in the waiting event, and the submitted data is
the step's output (`data`) for later steps."""

from __future__ import annotations

import pytest

from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.workflow_run_helpers import (
    cleanup_definitions,
    decide,
    enqueue_workflow_run,
    publish_workflow,
    wait_event,
)

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
SPEC = {
    "name": "Ask",
    "input_schema": {"type": "object"},
    "start": "ask",
    "steps": [
        {"id": "ask", "type": "input", "prompt": "Where?", "timeout_hours": 1, "resume_schema": SCHEMA, "next": "now"},
        {"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}},
    ],
}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _start(worker, client, tenant):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, SPEC)
    return enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={}, app_version=APP_VERSION,
    )


def test_input_step_waits_with_its_schema_and_outputs_the_submitted_data(worker, client, tenant, defs):
    run_id = _start(worker, client, tenant)
    waiting = wait_event(worker.engine, run_id, "step:ask:waiting")
    assert waiting["data"]["resume_schema"] == SCHEMA
    event_log = worker.event_log
    outcome, _ = event_log.append_step_decision(
        tenant, run_id, "ask", "step_approved",
        {"step_id": "ask", "decided_by": "alice", "comment": None, "resume_data": {"city": "Oslo"}},
    )
    assert outcome == "created"
    client.send(waiting["data"]["workflow_id"], {"decision": "approved"}, "approval:ask")
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    completed = wait_event(worker.engine, run_id, "step:ask:completed")
    assert completed["data"]["output"] == {"data": {"city": "Oslo"}, "decided_by": "alice", "comment": None}
    assert decide(event_log, client, tenant_id=tenant, run_id=run_id, step_id="ask") == "exists"


def test_rejecting_an_input_step_fails_the_run(worker, client, tenant, defs):
    run_id = _start(worker, client, tenant)
    wait_event(worker.engine, run_id, "step:ask:waiting")
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="ask", approve=False)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "approval_rejected"
