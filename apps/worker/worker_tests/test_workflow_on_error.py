"""`on_error: continue` on a tool step: the tool's `{error: {...}}` result is kept as the output."""

from __future__ import annotations

import pytest

from worker_tests.conftest import event_rows, wait_terminal
from worker_tests.test_workflow_run import _register_fake, _start
from worker_tests.workflow_run_helpers import cleanup_definitions

TOOL = "test.on_error"
ERROR = {"error": {"code": "body_source_rejected", "message": "not an image"}}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _call(sid, value, nxt=None, **extra):
    return {"id": sid, "type": "tool", "tool_id": TOOL, "input": {"value": value}, "next": nxt, **extra}


def _spec(*steps, start=None):
    return {"name": "On error", "input_schema": {"type": "object"}, "start": start or steps[0]["id"], "steps": list(steps)}


def _completed(worker, run_id, step_id):
    return next(d for _, k, t, d in event_rows(worker.engine, run_id)
                if k == f"step:{step_id}:completed" and t == "step_completed")


def test_kept_error_continues_with_the_error_result_as_output(worker, client, tenant, defs, monkeypatch):
    _register_fake(worker, monkeypatch, TOOL)
    spec = _spec(_call("up", {"$ref": "input.value"}, "after", on_error="continue"), _call("after", "fine"))
    _, run_id = _start(worker, client, tenant, spec, {"value": ERROR})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    done = _completed(worker, run_id, "up")
    assert done["output"] == ERROR  # unchanged shape
    assert done["outcome"] == "error_kept" and done["error_code"] == "body_source_rejected"
    assert "outcome" not in _completed(worker, run_id, "after")


def test_default_still_fails(worker, client, tenant, defs, monkeypatch):
    _register_fake(worker, monkeypatch, TOOL)
    spec = _spec(_call("up", {"$ref": "input.value"}, "after"), _call("after", "fine"))
    _, run_id = _start(worker, client, tenant, spec, {"value": ERROR})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "failed"
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert "step:up:failed" in keys and "step:after:started" not in keys


def test_kept_success_has_no_marker(worker, client, tenant, defs, monkeypatch):
    _register_fake(worker, monkeypatch, TOOL)
    _, run_id = _start(worker, client, tenant, _spec(_call("up", {"$ref": "input.value"}, on_error="continue")),
                       {"value": {"ok": True}})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    assert "outcome" not in _completed(worker, run_id, "up")


@pytest.mark.parametrize(("value", "taken"), [(ERROR, "skip"), ({"ok": True}, "use")])
def test_branch_routes_on_exists_error(worker, client, tenant, defs, monkeypatch, value, taken):
    _register_fake(worker, monkeypatch, TOOL)
    branch = {"id": "gate", "type": "branch", "default": "use", "cases": [
        {"when": {"op": "exists", "left": {"$ref": "steps.up.output.error"}}, "next": "skip"}]}
    spec = _spec(_call("up", {"$ref": "input.value"}, "gate", on_error="continue"), branch,
                 _call("use", "u"), _call("skip", "s"), start="up")
    _, run_id = _start(worker, client, tenant, spec, {"value": value})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert f"step:{taken}:completed" in keys
    assert f"step:{'use' if taken == 'skip' else 'skip'}:started" not in keys


def test_kept_error_inside_a_foreach_iteration(worker, client, tenant, defs, monkeypatch):
    _register_fake(worker, monkeypatch, TOOL)
    each = {"id": "each", "type": "foreach", "items": {"$ref": "input.items"}, "max_items": 3,
            "body": {"start": "try", "steps": [_call("try", {"$ref": "loop.each.item"}, on_error="continue")]}}
    _, run_id = _start(worker, client, tenant, _spec(each), {"items": [ERROR, {"ok": 1}]})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    marked = [d for _, k, t, d in event_rows(worker.engine, run_id)
              if t == "step_completed" and d.get("step_id") == "try"]
    assert [d.get("outcome") for d in marked].count("error_kept") == 1
