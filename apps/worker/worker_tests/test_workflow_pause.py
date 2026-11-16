"""D3: a step-by-step run pauses after each top-level step (waiting under `{step}_pause`), once
after a whole loop node, and approve continues / reject fails `cancelled_by_user`."""

from __future__ import annotations

import pytest
from sqlalchemy import text

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


PARALLEL = {
    "name": "Par",
    "input_schema": {"type": "object"},
    "start": "fan",
    "steps": [
        {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "done"},
        {"id": "a", "type": "tool", "tool_id": "clock.now", "input": {}},
        {"id": "b", "type": "tool", "tool_id": "clock.now", "input": {}},
        {"id": "done", "type": "tool", "tool_id": "clock.now", "input": {}},
    ],
}


def _open_rows(worker, tenant):
    with worker.engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM pending_approvals WHERE tenant_id=:t AND decision IS NULL"), {"t": tenant}
        ).scalar_one()


def _assert_pause_failure(worker, tenant, run_id, code):
    rows = event_rows(worker.engine, run_id)
    end = rows[-1][3]
    assert end["error_code"] == code and end["step_id"] == "first_pause"
    failed = [r for r in rows if r[1] == "step:first_pause:failed"]
    assert failed and failed[0][3]["error_code"] == code
    assert _open_rows(worker, tenant) == 0
    assert worker.run_store.get_run(tenant, run_id)["display_status"] != "waiting_approval"


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
    keys_in_order = keys.index("step:lp_pause:waiting")
    assert len([k for k in keys[:keys_in_order] if k.endswith("step:tick:completed")]) == 3
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="lp_pause")
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"


def test_reject_at_pause_fails_run_cancelled_by_user(worker, client, tenant, defs):
    run_id = _start(worker, client, tenant, TWO)
    wait_event(worker.engine, run_id, "step:first_pause:waiting")
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="first_pause", approve=False)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    _assert_pause_failure(worker, tenant, run_id, "cancelled_by_user")


def test_pause_timeout_fails_run_pause_timed_out(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_PAUSE_TIMEOUT_HOURS", "1")
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "1")
    run_id = _start(worker, client, tenant, TWO)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    _assert_pause_failure(worker, tenant, run_id, "pause_timed_out")


def test_pause_after_parallel_node_not_inside_branches(worker, client, tenant, defs):
    run_id = _start(worker, client, tenant, PARALLEL)
    wait_event(worker.engine, run_id, "step:fan_pause:waiting")
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert not [k for k in keys if k.endswith(("a_pause:waiting", "b_pause:waiting"))]
    assert "step:fan:completed" in keys
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="fan_pause")
    wait_event(worker.engine, run_id, "step:done_pause:waiting")
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="done_pause")
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert sorted(k for k in keys if k.endswith("_pause:waiting")) == ["step:done_pause:waiting", "step:fan_pause:waiting"]


def test_cooperative_cancel_wakes_a_pause_within_one_poll_round(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "2")
    run_id = _start(worker, client, tenant, TWO)
    wait_event(worker.engine, run_id, "step:first_pause:waiting")
    assert _open_rows(worker, tenant) == 1
    # Only the cancel event; no DBOS cancel and no API cascade (the cancel did not land).
    worker.event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": "alice"})
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
    assert run["status"] == "failed"
    end = event_rows(worker.engine, run_id)[-1][3]
    assert end["error_code"] == "run_cancelled" and end["step_id"] == "first_pause"
    assert _open_rows(worker, tenant) == 0


def test_pause_cancel_check_cadence_is_its_own_knob_not_the_approval_poll(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "3600")
    monkeypatch.setenv("EAP_PAUSE_CANCEL_CHECK_SECONDS", "1")
    run_id = _start(worker, client, tenant, TWO)
    wait_event(worker.engine, run_id, "step:first_pause:waiting")
    worker.event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": "alice"})
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=15)
    assert run["status"] == "failed"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "run_cancelled"
    assert _open_rows(worker, tenant) == 0


def test_pause_cancel_check_returns_false_when_the_decision_was_not_created(monkeypatch):
    import asyncio

    from eap_worker import workflow_run as wr

    class Event:
        type = "cancel_requested"

    class Log:
        def __init__(self, outcome):
            self.outcome = outcome

        def get_event(self, tenant_id, run_id, key):
            return Event()

        def append_step_decision(self, *args, **kwargs):
            return self.outcome, None

    class Runtime:
        def __init__(self, outcome):
            self.event_log = Log(outcome)

    for outcome, expected in (("created", True), ("exists", False), ("not_waiting", False)):
        monkeypatch.setattr(wr, "get_runtime", lambda outcome=outcome: Runtime(outcome))
        assert asyncio.run(wr.pause_cancel_check_step("r", "t", "first_pause")) is expected


def test_approve_racing_a_cancel_stands_and_the_run_cancels_at_the_next_tick(worker, client, tenant, defs):
    spec = {
        "name": "Race", "input_schema": {"type": "object"}, "start": "first",
        "steps": [{"id": "first", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "lp"}, LOOP["steps"][0]],
    }
    run_id = _start(worker, client, tenant, spec)
    wait_event(worker.engine, run_id, "step:first_pause:waiting")
    worker.event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": "alice"})
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="first_pause")
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
    assert run["status"] == "failed"
    rows = event_rows(worker.engine, run_id)
    by_key = {k: (t, d) for _, k, t, d in rows}
    assert by_key["step:first_pause:decision"][0] == "step_approved"
    assert not [1 for _, _, t, _ in rows if t == "step_rejected"]
    end = rows[-1][3]
    assert end["error_code"] == "run_cancelled" and end["step_id"] == "lp"
