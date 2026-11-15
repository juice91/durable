"""LP.11 acceptance (tester): iteration spans (LP.9 wiring) when a cancel or the cost cap stops a loop.

One in-memory exporter; a stopped iteration ends its span non-OK, with the error code and no
exception text (a DBOS cancel message names the workflow id; a cost message names the spend).
"""

from __future__ import annotations

import uuid

from opentelemetry.trace import StatusCode

from worker_tests.conftest import APP_VERSION, wait_for, wait_terminal
from worker_tests.test_workflow_loop_cancel_acceptance import _body, gate  # noqa: F401 - fixture
from worker_tests.test_workflow_loop_tick import (  # noqa: F401
    _ask, _iteration_spans, _loop, _start, _wf, defs, spans,
)
from worker_tests.workflow_run_helpers import agent_spec, publish_agent, publish_workflow


def _no_exception_text(span):
    assert not [e for e in span.events if e.name == "exception"], span.events
    assert not span.status.description or "cost limit" not in span.status.description
    blob = " ".join(str(v) for v in span.attributes.values()) + (span.status.description or "")
    assert "RunBudgetExceeded" not in blob and "WorkflowStepError" not in blob


def test_one_span_per_iteration_and_a_cost_cap_stop_adds_none(worker, client, tenant, defs, spans):
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    spec = _wf(_loop([_ask(agent_id)], max_iterations=9), limits={"max_cost_usd": "0.05"})
    run_id = _start(worker, client, tenant, spec, {})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "failed"
    found = _iteration_spans(spans, run_id)
    assert [s.attributes["eap.loop_scope"] for s in found] == ["lp.0", "lp.1", "lp.2"]  # none for the stopped one
    for s in found:
        assert s.status.status_code == StatusCode.OK
        _no_exception_text(s)


def test_a_nested_cooperative_cancel_ends_the_outer_iteration_span_non_ok_without_exception_text(
        worker, client, tenant, defs, spans, gate):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    inner = _loop(_body(agent_id), lid="in", max_iterations=30)
    gate["cancel_at"] = 2  # the cooperative cancel lands during inner iteration 1
    run_id = _start(worker, client, tenant, _wf(_loop([inner], lid="out", max_iterations=3)), {}, before=gate["bind"])
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=90)["status"] == "failed"
    by_scope = {s.attributes["eap.loop_scope"]: s for s in _iteration_spans(spans, run_id)}
    outer = by_scope["out.0"]
    assert outer.status.status_code == StatusCode.ERROR
    assert (outer.attributes["eap.iteration_status"], outer.attributes["eap.error_code"]) == ("failed", "run_cancelled")
    _no_exception_text(outer)
    assert not [k for k in by_scope if k.startswith("out.1")]


def test_an_api_cancel_ends_the_running_iteration_span_non_ok_without_exception_text(
        worker, client, worker_settings, tenant, defs, spans, gate):
    from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api

    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    wf, _ = publish_workflow(worker.engine, tenant, _wf(_loop(_body(agent_id), max_iterations=50)))
    run_id = str(uuid.uuid4())
    gate["hold_at"] = 2  # iteration 1's agent request is held while the API cancels the run
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        api.post(f"/api/v1/workflows/{wf}/runs", headers=auth_header(tenant), json={"input": {}, "run_id": run_id})
        assert gate["entered"].wait(60), "iteration 1's agent request never started"
        assert api.post(f"/api/v1/runs/{run_id}/cancel", headers=auth_header(tenant, "alice")).status_code == 202
        gate["release"].set()
    # Wait for the condition itself (iteration 1's span ends once its workflow unwinds), not a sleep.
    by_scope = wait_for(
        lambda: {s.attributes["eap.loop_scope"]: s for s in _iteration_spans(spans, run_id)}
        if any(s.attributes["eap.loop_scope"] == "lp.1" for s in _iteration_spans(spans, run_id)) else None,
        timeout=60, what="iteration lp.1's span to end",
    )
    found = list(by_scope.values())
    assert by_scope["lp.0"].status.status_code == StatusCode.OK
    stopped = [s for s in found if s.status.status_code != StatusCode.OK]
    assert [s.attributes["eap.loop_scope"] for s in stopped] == ["lp.1"], [
        (s.attributes["eap.loop_scope"], s.status.status_code) for s in found]
    for s in stopped:
        _no_exception_text(s)
        # A DBOS cancel of the running iteration still records the stable code, never only a
        # class name in the status description.
        assert (s.attributes.get("eap.iteration_status"), s.attributes.get("eap.error_code")) == (
            "failed", "run_cancelled"), (s.attributes, s.status.description)
