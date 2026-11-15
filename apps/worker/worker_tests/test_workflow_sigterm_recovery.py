"""Graceful worker shutdown (SIGTERM, a deploy) in the middle of a workflow run (LP.10 review).

`DBOS.destroy()` cancels every task of the worker's event loop with asyncio.CancelledError. A
foreach window or a parallel step must let that propagate untouched: before the fix, their
`except BaseException` handlers cancelled the started children durably and recorded
`DBOS.cancelWorkflow` operations in the parent, so recovery failed the run with
DBOSUnexpectedStepError. Each test stops the first worker gracefully mid-run, starts a second
one, and checks the run completes with exactly one execution per item, branch or iteration.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from dbos import DBOSClient

from worker_tests.conftest import DBOS_DB_URL, event_rows, wait_for, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION, WorkerProcess, _env, crash_schema  # noqa: F401
from worker_tests.workflow_run_helpers import cleanup_definitions, decide, enqueue_workflow_run, publish_workflow


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _ref(path):
    return {"$ref": path}


def _wf(*steps):
    return {"name": "Sigterm", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps)}


TICKET = {"id": "mk", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}}


def _has_key(worker, run_id, key):
    """Whether the run has the event `key` (or, for a tuple, every one of them)."""
    keys = {k for _, k, _, _ in event_rows(worker.engine, run_id)}
    return all(k in keys for k in key) if isinstance(key, tuple) else key in keys


def _sigterm_run(worker, tenant, crash_schema, tmp_path, spec, input_value, stop_at, after_stop=None):
    """Run `spec` on a worker process, SIGTERM it once event `stop_at` exists, recover on a second
    worker; returns (run_id, run). `after_stop(client, run_id)` runs before the second worker."""
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    executor_id = f"st-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w1.log")
    second = client = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                      version_id=version_id, input_value=input_value, app_version=APP_VERSION)
        wait_for(lambda: _has_key(worker, run_id, stop_at), timeout=90, what=str(stop_at))
        first.stop()  # SIGTERM: DBOS.destroy() cancels the in-flight tasks
        assert first.proc.returncode is not None
        assert not _has_key(worker, run_id, "end"), "the run finished before the SIGTERM; the test proves nothing"
        log = (tmp_path / "w1.log").read_text()
        assert "DBOSUnexpectedStepError" not in log
        if after_stop is not None:
            after_stop(client, run_id)
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=150)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        time.sleep(0.6)  # a second look: recovery adds nothing once terminal
        return run_id, run
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if client is not None:
            client.destroy()


def _assert_clean_log(worker, run_id):
    events = event_rows(worker.engine, run_id)
    keys = [e[1] for e in events]
    assert [e[0] for e in events] == list(range(len(events)))  # gap-free seq
    assert len(set(keys)) == len(keys) and keys.count("end") == 1 and keys[-1] == "end"
    return keys


ITEMS, INNER = 4, 15


def test_sigterm_mid_foreach_window_resumes_every_item_once(worker, tenant, defs, crash_schema, tmp_path):
    inner = {"id": "lp", "type": "loop", "max_iterations": INNER, "on_max_iterations": "exit",
             "until": {"op": "eq", "left": _ref("loop.lp.index"), "right": 1000},
             "body": {"start": "mk", "steps": [TICKET]}}
    spec = _wf({"id": "each", "type": "foreach", "items": _ref("input.items"), "max_items": ITEMS,
                "concurrency": ITEMS, "body": {"start": "lp", "steps": [inner]}})
    run_id, run = _sigterm_run(worker, tenant, crash_schema, tmp_path, spec, {"items": list(range(ITEMS))},
                               "loop:each.0/lp.3:started")
    keys = _assert_clean_log(worker, run_id)
    out = json.loads(run["output"])
    assert len(out) == ITEMS and all(o["iterations"] == INNER and o["exited"] == "max_iterations" for o in out)
    for i in range(ITEMS):
        assert keys.count(f"loop:each.{i}:started") == 1 and keys.count(f"loop:each.{i}:completed") == 1
    assert not any(k.endswith(":failed") for k in keys)
    assert worker.tickets.call_count(tenant, f"{run_id}:") == ITEMS * INNER  # one call per inner iteration
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == ITEMS * INNER


def test_sigterm_mid_parallel_resumes_every_branch_once(worker, client, tenant, defs, crash_schema, tmp_path):
    """Both branches wait for an approval while the parent awaits branch `a`: the shutdown must not
    cancel branch `b` (still running) or record a cancel in the parent. After recovery both are
    approved, and each branch's ticket is created once."""
    spec = _wf(
        {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "j"},
        {"id": "a", "type": "approval", "prompt": "OK?", "timeout_hours": 1, "next": "ta"},
        {**TICKET, "id": "ta", "next": "j"},
        {"id": "b", "type": "approval", "prompt": "OK?", "timeout_hours": 1, "next": "tb"},
        {**TICKET, "id": "tb", "next": "j"},
        {"id": "j", "type": "tool", "tool_id": "clock.now", "input": {}},
    )

    def approve(dbos_client, run_id):
        for step in ("a", "b"):
            outcome = decide(worker.event_log, dbos_client, tenant_id=tenant, run_id=run_id, step_id=step)
            assert outcome == "created", step

    run_id, _run = _sigterm_run(worker, tenant, crash_schema, tmp_path, spec, {}, ("step:a:waiting", "step:b:waiting"),
                                approve)
    keys = _assert_clean_log(worker, run_id)
    for step in ("fan", "a", "ta", "b", "tb", "j"):
        assert keys.count(f"step:{step}:started") == 1 and keys.count(f"step:{step}:completed") == 1, step
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 2


def test_sigterm_mid_while_loop_resumes_every_iteration_once(worker, tenant, defs, crash_schema, tmp_path):
    n = 25
    spec = _wf({"id": "lp", "type": "loop", "max_iterations": n, "on_max_iterations": "exit",
                "while": {"op": "lt", "left": _ref("loop.lp.index"), "right": n},
                "body": {"start": "mk", "steps": [TICKET]}})
    run_id, run = _sigterm_run(worker, tenant, crash_schema, tmp_path, spec, {}, "loop:lp.4:started")
    keys = _assert_clean_log(worker, run_id)
    assert json.loads(run["output"])["iterations"] == n
    for i in range(n):
        assert keys.count(f"loop:lp.{i}:started") == 1 and keys.count(f"loop:lp.{i}:completed") == 1
    assert worker.tickets.call_count(tenant, f"{run_id}:") == n
