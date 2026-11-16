"""D3: a step-by-step run pauses after each top-level step (waiting under `{step}_pause`), once
after a whole loop node, and approve continues / reject fails `cancelled_by_user`."""

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

TWO = {
    "name": "Two",
    "input_schema": {"type": "object"},
    "start": "first",
    "steps": [
        {"id": "first", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "second"},
        {"id": "second", "type": "tool", "tool_id": "clock.now", "input": {}},
    ],
}
LOOP = {
    "name": "Loop",
    "input_schema": {"type": "object"},
    "start": "lp",
    "steps": [
        {
            "id": "lp", "type": "loop", "max_iterations": 5,
            "until": {"op": "eq", "left": {"$ref": "loop.lp.index"}, "right": 2},
            "body": {"start": "tick", "steps": [{"id": "tick", "type": "tool", "tool_id": "clock.now", "input": {}}]},
        },
    ],
}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _start(worker, client, tenant, spec, pause=True):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    return enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={}, app_version=APP_VERSION, pause_after_each_step=pause,
    )


def test_run_pauses_after_each_top_level_step_and_continues_on_approve(worker, client, tenant, defs):
    run_id = _start(worker, client, tenant, TWO)
    waiting = wait_event(worker.engine, run_id, "step:first_pause:waiting")
    assert waiting["type"] == "step_waiting"
    data = waiting["data"]
    assert data["pause"] is True and data["prompt"] == "Paused after first" and data["step_id"] == "first_pause"
    assert "resume_schema" not in data and "deadline" in data
    assert worker.run_store.get_run(tenant, run_id)["status"] != "completed"
    assert decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="first_pause") == "created"
    wait_event(worker.engine, run_id, "step:second_pause:waiting")
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert "step:second:completed" in keys
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="second_pause")
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"


def test_run_without_the_flag_never_pauses(worker, client, tenant, defs):
    run_id = _start(worker, client, tenant, TWO, pause=False)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    assert not [k for _, k, _, _ in event_rows(worker.engine, run_id) if k.endswith("_pause:waiting")]


def test_pause_after_loop_node_not_inside(worker, client, tenant, defs):
    run_id = _start(worker, client, tenant, LOOP)
    wait_event(worker.engine, run_id, "step:lp_pause:waiting")
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert not [k for k in keys if "tick_pause" in k]
    assert "step:lp:completed" in keys
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="lp_pause")
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"


def test_reject_at_pause_fails_run_cancelled_by_user(worker, client, tenant, defs):
    run_id = _start(worker, client, tenant, TWO)
    wait_event(worker.engine, run_id, "step:first_pause:waiting")
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="first_pause", approve=False)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "cancelled_by_user"


def test_pause_timeout_fails_run_pause_timed_out(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_PAUSE_TIMEOUT_HOURS", "1")
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "1")
    run_id = _start(worker, client, tenant, TWO)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "pause_timed_out"
