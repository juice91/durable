"""A run that ends through `workflow.return {outcome: "skipped"}` has the terminal status `skipped`."""

from __future__ import annotations

import pytest

from worker_tests.conftest import event_rows, wait_terminal
from worker_tests.test_workflow_run import _start
from worker_tests.workflow_run_helpers import cleanup_definitions

RETURN = "workflow.return"
VALUE = {"status": "outside_slot"}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _allow(worker, monkeypatch):
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset(worker.registry.tool_ids()))


def _ret(sid, value, outcome=None, nxt=None):
    params = {"value": value, **({"outcome": outcome} if outcome else {})}
    return {"id": sid, "type": "tool", "tool_id": RETURN, "input": params, "next": nxt}


def _spec(*steps, start=None):
    return {"name": "Skip", "input_schema": {"type": "object"}, "start": start or steps[0]["id"], "steps": list(steps)}


def _end(worker, run_id):
    return next((t, d) for _, k, t, d in event_rows(worker.engine, run_id) if k == "end")


def test_skipped_return_ends_the_run_skipped_and_keeps_the_output(worker, client, tenant, defs, monkeypatch):
    _allow(worker, monkeypatch)
    _, run_id = _start(worker, client, tenant, _spec(_ret("stop", VALUE, "skipped")), {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "skipped", run["error"]
    assert run["output"] == '{"status":"outside_slot"}' or '"outside_slot"' in run["output"]
    assert run["finished_at"] is not None
    etype, data = _end(worker, run_id)
    assert etype == "run_completed" and data["outcome"] == "skipped" and data["result"] == VALUE


@pytest.mark.parametrize("outcome", [None, "completed"])
def test_default_return_is_completed(worker, client, tenant, defs, monkeypatch, outcome):
    _allow(worker, monkeypatch)
    _, run_id = _start(worker, client, tenant, _spec(_ret("done", VALUE, outcome)), {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed"
    etype, data = _end(worker, run_id)
    assert etype == "run_completed" and "outcome" not in data


def test_skipped_return_on_a_branch_path(worker, client, tenant, defs, monkeypatch):
    _allow(worker, monkeypatch)
    branch = {"id": "gate", "type": "branch", "default": "work", "cases": [
        {"when": {"op": "eq", "left": {"$ref": "input.slot"}, "right": "closed"}, "next": "stop"}]}
    spec = _spec(branch, _ret("work", {"posted": True}), _ret("stop", VALUE, "skipped"), start="gate")
    _, closed = _start(worker, client, tenant, spec, {"slot": "closed"})
    _, open_ = _start(worker, client, tenant, spec, {"slot": "open"})
    assert wait_terminal(worker.run_store, tenant, closed)["status"] == "skipped"
    assert wait_terminal(worker.run_store, tenant, open_)["status"] == "completed"
