"""eap.workflow_run on the in-process worker: agent, tool, branch, parallel and approval steps."""

from __future__ import annotations

import uuid

import pytest

from worker_tests.conftest import APP_VERSION, event_rows, wait_for, wait_terminal
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


def _start(worker, client, tenant, spec, input_value):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION,
    )
    return workflow_id, run_id


def test_agent_tool_branch_default_path_completes(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    _, run_id = _start(worker, client, tenant, demo_spec(agent_id), {"q": "hello"})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert run["kind"] == "workflow" and run["workflow_version"] == 1
    assert run["output"] == '"Echo: Echo: hello"'
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    assert [(k, t) for _, k, t, _ in events] == [
        ("start", "run_started"),
        ("step:ask:started", "step_started"), ("step:ask:completed", "step_completed"),
        ("step:now:started", "step_started"), ("step:now:completed", "step_completed"),
        ("step:route:started", "step_started"), ("step:route:completed", "step_completed"),
        ("step:done:started", "step_started"), ("step:done:completed", "step_completed"),
        ("end", "run_completed"),
    ]
    assert events[6][3]["output"] == {"case": None, "next": "done"}
    # The agent step ran as a child agent run, linked and hidden from the top-level list.
    child_id = events[1][3]["child_run_id"]
    child = worker.run_store.get_run(tenant, uuid.UUID(child_id))
    assert child["parent_run_id"] == str(run_id) and child["parent_step_id"] == "ask"
    assert child["status"] == "completed" and child["input"] == "hello"
    top = {r["id"] for r in worker.run_store.list_runs(tenant)}
    assert str(run_id) in top and child_id not in top
    assert {r["id"] for r in worker.run_store.list_runs(tenant, parent_run_id=run_id)} == {
        child_id, events[7][3]["child_run_id"]}


def test_approval_waits_then_resumes_once_approved(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    _, run_id = _start(worker, client, tenant, demo_spec(agent_id), {"q": "urgent: printer"})
    waiting = wait_event(worker.engine, run_id, "step:approve:waiting")
    assert waiting["type"] == "step_waiting" and waiting["data"]["workflow_id"] == str(run_id)
    assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
    assert decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="approve", comment="ok") == "created"
    assert decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="approve") == "exists"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert keys[keys.index("step:approve:started"):keys.index("step:done:started")] == [
        "step:approve:started", "step:approve:waiting", "step:approve:decision", "step:approve:completed"]
    completed = wait_event(worker.engine, run_id, "step:approve:completed")
    assert completed["data"]["output"] == {"approved": True, "decided_by": "alice", "comment": "ok"}
    # Decided and finished: no longer waiting.
    assert worker.event_log.append_step_decision(tenant, run_id, "approve", "step_rejected", {})[0] == "exists"


def test_rejection_fails_the_run(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    _, run_id = _start(worker, client, tenant, demo_spec(agent_id), {"q": "urgent"})
    wait_event(worker.engine, run_id, "step:approve:waiting")
    decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="approve", approve=False)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "WorkflowStepError: the approval was rejected"
    events = event_rows(worker.engine, run_id)
    assert events[-2][1:3] == ("step:approve:failed", "step_failed")
    assert events[-1][3]["error_code"] == "approval_rejected" and events[-1][3]["step_id"] == "approve"


def test_approval_timeout_fails_the_run(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "1")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.5")
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    _, run_id = _start(worker, client, tenant, demo_spec(agent_id), {"q": "urgent"})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert wait_event(worker.engine, run_id, "step:approve:decision")["type"] == "step_timed_out"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "approval_timeout"
    assert decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="approve") == "exists"


def test_parallel_branches_then_join_reads_branch_outputs(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {
        "name": "Fan out",
        "input_schema": {"type": "object"},
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "join"},
            {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"p": "left"}, "next": "join"},
            {"id": "b", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "join"},
            {"id": "join", "type": "agent", "agent_id": agent_id, "input": {"p": {"$ref": "steps.a.output"}}},
        ],
    }
    _, run_id = _start(worker, client, tenant, spec, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert run["output"] == '"Echo: Echo: left"'
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    keys = [k for _, k, _, _ in events]
    for sid in ("fan", "a", "b", "join"):
        assert f"step:{sid}:started" in keys and f"step:{sid}:completed" in keys
    assert keys.index("step:fan:completed") < keys.index("step:join:started")
    assert wait_event(worker.engine, run_id, "step:fan:completed")["data"]["output"] is None


def test_parallel_merge_over_the_context_cap_fails_the_parallel_step(worker, client, tenant, defs, monkeypatch):
    """Each branch output fits the run's context cap on its own; merged into the parent they do
    not. The parallel step fails workflow_context_too_large before the join (or anything else) runs."""
    monkeypatch.setenv("EAP_WORKFLOW_MAX_CONTEXT_BYTES", "60")  # read once, at run start
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {
        "name": "Fan out, too much",
        "input_schema": {"type": "object"},
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "join"},
            {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"p": "x" * 30}, "next": "join"},
            {"id": "b", "type": "agent", "agent_id": agent_id, "input": {"p": "y" * 30}, "next": "join"},
            {"id": "join", "type": "agent", "agent_id": agent_id, "input": {"p": "z"}},
        ],
    }
    _, run_id = _start(worker, client, tenant, spec, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    keys = [k for _, k, _, _ in events]
    # Both branches completed (each output is 38 bytes of JSON, under the 60-byte cap) ...
    assert "step:a:completed" in keys and "step:b:completed" in keys
    # ... but their merge (76 bytes) is over it: the parallel step fails, the join never starts.
    failed = next(e for e in events if e[1] == "step:fan:failed")
    assert failed[3]["error_code"] == "workflow_context_too_large"
    assert "step:fan:completed" not in keys and "step:join:started" not in keys
    assert events[-1][3]["error_code"] == "workflow_context_too_large"
    assert events[-1][3]["step_id"] == "fan"


def test_tool_step_input_rejected_and_missing_ref(worker, client, tenant, defs):
    spec = {
        "name": "Bad tool",
        "input_schema": {"type": "object"},
        "start": "t",
        "steps": [{"id": "t", "type": "tool", "tool_id": "tickets.create", "input": {"title": {"$ref": "input.title"}}}],
    }
    _, run_id = _start(worker, client, tenant, spec, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "input_ref_missing"
    _, run_id = _start(worker, client, tenant, spec, {"title": "only a title"})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "tool_input_invalid"
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 0


def test_side_effecting_tool_step_uses_step_key(worker, client, tenant, defs):
    spec = {
        "name": "Ticket",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object", "required": ["ticket_id"]},
        "start": "t",
        "steps": [{"id": "t", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}}],
    }
    _, run_id = _start(worker, client, tenant, spec, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
    from sqlalchemy import text

    with worker.engine.connect() as conn:
        keys = set(conn.execute(text("SELECT key FROM side_effects WHERE tenant_id = :t"), {"t": tenant}).scalars())
    assert f"{run_id}:t" in keys and f"{run_id}:wf_t" in keys


def test_agent_not_found_and_output_schema_mismatch(worker, client, tenant, defs):
    spec = {
        "name": "Ghost",
        "input_schema": {"type": "object"},
        "start": "a",
        "steps": [{"id": "a", "type": "agent", "agent_id": str(uuid.uuid4()), "input": {}}],
    }
    _, run_id = _start(worker, client, tenant, spec, {})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "failed"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "agent_not_found"

    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {**spec, "output_schema": {"type": "integer"},
            "steps": [{"id": "a", "type": "agent", "agent_id": agent_id, "input": {}}]}
    _, run_id = _start(worker, client, tenant, spec, {})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert events[-2][1] == "step:a:completed"  # the step finished; the run output did not validate
    assert events[-1][3]["error_code"] == "output_invalid" and events[-1][3]["step_id"] is None


def test_agent_step_with_a_required_context_field_fails_request_context_invalid(worker, client, tenant, defs):
    """Workflow agent steps run with an empty request context: an agent with a required context
    field fails the step (and the run) with request_context_invalid before any model request."""
    spec = {**agent_spec(), "context_fields": [{"name": "customer", "type": "string", "required": True}]}
    agent_id = publish_agent(worker.engine, tenant, spec)
    wf = {
        "name": "Needs context",
        "input_schema": {"type": "object"},
        "start": "a",
        "steps": [{"id": "a", "type": "agent", "agent_id": agent_id, "input": {"q": "hi"}}],
    }
    _, run_id = _start(worker, client, tenant, wf, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    failed = [e for e in events if e[1] == "step:a:failed"][0][3]
    assert failed["error_code"] == "request_context_invalid"
    assert events[-1][3]["error_code"] == "request_context_invalid"
    child_id = [e for e in events if e[1] == "step:a:started"][0][3]["child_run_id"]
    child = worker.run_store.get_run(tenant, uuid.UUID(child_id))
    assert child["status"] == "failed"
    assert child["error"] == "RequestContextInvalid: the request context does not match the agent's context fields"
    child_events = event_rows(worker.engine, child_id)
    assert [e[1] for e in child_events] == ["start", "end"]
    assert child_events[-1][3]["error_code"] == "request_context_invalid"


def test_archived_agent_is_agent_archived_when_the_child_would_be_created(worker, client, tenant, defs):
    """The archived check still guards the create path: a fresh step never creates a child run for
    an archived agent, and fails with `agent_archived` (not `agent_not_found`: the agent exists).
    Recovery of an already-created child is the W5b crash test."""
    from sqlalchemy import text

    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE agents SET archived = TRUE WHERE id = :a"), {"a": agent_id})
    spec = {
        "name": "Archived",
        "input_schema": {"type": "object"},
        "start": "a",
        "steps": [{"id": "a", "type": "agent", "agent_id": agent_id, "input": {}}],
    }
    _, run_id = _start(worker, client, tenant, spec, {})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "failed"
    last = event_rows(worker.engine, run_id)[-1][3]
    assert last["error_code"] == "agent_archived" and last["step_id"] == "a"
    run = worker.run_store.get_run(tenant, run_id)
    assert "archived" in (run["error"] or "")
    with worker.engine.connect() as conn:
        children = conn.execute(text("SELECT id FROM runs WHERE parent_run_id = :r"), {"r": str(run_id)}).fetchall()
    assert children == []


def test_non_finite_input_fails_before_any_step(worker, client, tenant, defs):
    spec = {"name": "N", "input_schema": {"type": "object"}, "start": "t",
            "steps": [{"id": "t", "type": "tool", "tool_id": "clock.now", "input": {}}]}
    # enqueue_workflow_run's canonical JSON keeps NaN (what a broken caller could enqueue).
    _, run_id = _start(worker, client, tenant, spec, {"n": [1, float("nan")]})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert [(k, t) for _, k, t, _ in events] == [("start", "run_started"), ("end", "run_failed")]
    assert events[0][3]["input"] is None and events[1][3]["error_code"] == "input_invalid"


PARALLEL_APPROVALS = {
    "name": "Two approvals",
    "input_schema": {"type": "object"},
    "start": "fan",
    "steps": [
        {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "done"},
        {"id": "a", "type": "approval", "prompt": "A?", "timeout_hours": 1, "next": "done"},
        {"id": "b", "type": "approval", "prompt": "B?", "timeout_hours": 1, "next": "done"},
        {"id": "done", "type": "tool", "tool_id": "clock.now", "input": {}},
    ],
}


def test_approval_inside_a_parallel_branch_targets_the_branch_workflow(worker, client, tenant, defs):
    _, run_id = _start(worker, client, tenant, PARALLEL_APPROVALS, {})
    for branch in ("a", "b"):
        waiting = wait_event(worker.engine, run_id, f"step:{branch}:waiting")
        assert waiting["data"]["workflow_id"] == f"{run_id}:fan:{branch}"
    # decide() sends to the waiting event's workflow_id, exactly as the API does.
    assert decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="b") == "created"
    wait_event(worker.engine, run_id, "step:b:completed")
    assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
    assert decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="a") == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    _contiguous(event_rows(worker.engine, run_id))


def test_parent_settles_every_branch_before_finishing(worker, client, tenant, defs, monkeypatch):
    """A branch whose result raises (cancelled) must not let the run finish while another branch
    is still running: the run stays running until the other branch settles, then fails.

    A cancelled DBOS workflow notices the cancellation at its next step, so the approval poll is 1 s
    (recorded when the run starts): branch a leaves its recv within a second and its result raises."""
    import time

    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "1")
    _, run_id = _start(worker, client, tenant, PARALLEL_APPROVALS, {})
    wait_event(worker.engine, run_id, "step:a:waiting")
    wait_event(worker.engine, run_id, "step:b:waiting")
    client.cancel_workflow(f"{run_id}:fan:a")
    wait_for(lambda: client.retrieve_workflow(f"{run_id}:fan:a").get_status().status == "CANCELLED",
             what="branch a cancelled")
    time.sleep(3.0)  # branch a has left its recv and its result has raised in the parent
    assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
    assert decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="b") == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert keys.index("step:b:completed") < keys.index("step:fan:failed") < keys.index("end")
    assert "step:done:started" not in keys
