"""AC.9: workflow agent steps consume structured output (`steps.<id>.output` is the object)."""

from __future__ import annotations

import json

import pytest

from eap_worker.workflow_run import agent_step_output
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
)

TRIAGE = {
    "type": "object",
    "properties": {"priority": {"type": "string", "enum": ["high", "low"]}, "summary": {"type": "string"}},
    "required": ["priority", "summary"],
}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def structured_agent(schema: dict = TRIAGE) -> dict:
    spec = agent_spec(model="structured", name="Triage")
    spec["output"] = {"json_schema": schema}
    return spec


def triage_workflow(agent_id: str, text_agent_id: str) -> dict:
    return {
        "name": "Triage then branch",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        "start": "triage",
        "steps": [
            {"id": "triage", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}},
             "next": "route"},
            {
                "id": "route",
                "type": "branch",
                "cases": [{"when": {"op": "eq", "left": {"$ref": "steps.triage.output.priority"}, "right": "high"},
                           "next": "escalate"}],
                "default": "file",
            },
            {"id": "escalate", "type": "agent", "agent_id": text_agent_id,
             "input": {"prompt": {"$ref": "steps.triage.output.summary"}}},
            {"id": "file", "type": "agent", "agent_id": text_agent_id, "input": {"prompt": "filed"}},
        ],
    }


def _start(worker, client, tenant, spec, input_value):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    return enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION,
    )


def _completed(events, step_id):
    return next(d for _, k, _, d in events if k == f"step:{step_id}:completed")


def test_agent_step_output_prefers_the_object_and_keeps_old_results_as_text():
    assert agent_step_output({"status": "completed", "output": '{"a":1}', "output_json": {"a": 1}}) == {"a": 1}
    # A text agent (and every child recorded before AC.4) has no output_json key.
    assert agent_step_output({"status": "completed", "output": "Echo: hi"}) == "Echo: hi"
    assert agent_step_output({"status": "completed", "output": "x", "output_json": None}) == "x"


@pytest.mark.parametrize("priority,taken", [("high", "escalate"), ("low", "file")])
def test_branch_on_a_structured_field(worker, client, tenant, defs, priority, taken):
    agent_id = publish_agent(worker.engine, tenant, structured_agent())
    text_id = publish_agent(worker.engine, tenant, agent_spec())
    prompt = json.dumps({"output": {"priority": priority, "summary": "disk full"}})
    run_id = _start(worker, client, tenant, triage_workflow(agent_id, text_id), {"q": prompt})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    events = event_rows(worker.engine, run_id)
    assert _completed(events, "triage")["output"] == {"priority": priority, "summary": "disk full"}
    assert _completed(events, "route")["output"]["next"] == taken
    if taken == "escalate":
        # A string field of the object becomes the next agent's prompt; a text agent stays a string.
        assert _completed(events, "escalate")["output"] == "Echo: disk full"
        assert run["output"] == '"Echo: disk full"'
    else:
        assert _completed(events, "file")["output"] == "Echo: filed"


def test_an_over_cap_object_fails_step_output_too_large(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_STEP_OUTPUT_BYTES", "40")  # read once, at run start
    agent_id = publish_agent(worker.engine, tenant, structured_agent())
    text_id = publish_agent(worker.engine, tenant, agent_spec())
    prompt = json.dumps({"output": {"priority": "high", "summary": "y" * 60}})
    run_id = _start(worker, client, tenant, triage_workflow(agent_id, text_id), {"q": prompt})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    events = event_rows(worker.engine, run_id)
    failed = next(d for _, k, _, d in events if k == "step:triage:failed")
    assert failed["error_code"] == "step_output_too_large"
    assert "step:route:started" not in [k for _, k, _, _ in events]


def _branch_only(agent_id: str, ref: str) -> dict:
    return {
        "name": "Branch on a field",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        "start": "triage",
        "steps": [
            {"id": "triage", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}},
             "next": "route"},
            {"id": "route", "type": "branch",
             "cases": [{"when": {"op": "eq", "left": {"$ref": ref}, "right": "high"}, "next": "hot"},
                       {"when": {"op": "exists", "left": {"$ref": ref}}, "next": "hot"}],
             "default": "cold"},
            {"id": "hot", "type": "agent", "agent_id": agent_id, "input": {"prompt": "hot"}},
            {"id": "cold", "type": "agent", "agent_id": agent_id, "input": {"prompt": "cold"}},
        ],
    }


def test_a_renamed_field_takes_the_default_and_is_listed_as_unresolved(worker, client, tenant, defs):
    """Review fix: an unpinned agent whose schema no longer has the field. The branch takes its
    default as before, and its step_completed lists the path (no values) in `unresolved_refs`."""
    agent_id = publish_agent(worker.engine, tenant, structured_agent())
    prompt = json.dumps({"output": {"priority": "high", "summary": "SECRET-SUMMARY"}})
    run_id = _start(worker, client, tenant, _branch_only(agent_id, "steps.triage.output.urgency"), {"q": prompt})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    events = event_rows(worker.engine, run_id)
    route = _completed(events, "route")
    assert route["output"] == {"case": None, "next": "cold"}
    assert route["unresolved_refs"] == ["steps.triage.output.urgency"]  # once, though two cases read it
    assert "SECRET-SUMMARY" not in json.dumps(route)
    # No new event or step: the same keys as a branch that resolved.
    assert [k for _, k, _, _ in events].count("step:route:completed") == 1


def test_resolved_and_text_refs_add_no_unresolved_refs(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, structured_agent())
    prompt = json.dumps({"output": {"priority": "high", "summary": "s"}})
    run_id = _start(worker, client, tenant, _branch_only(agent_id, "steps.triage.output.priority"), {"q": prompt})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    route = _completed(event_rows(worker.engine, run_id), "route")
    assert route["output"]["next"] == "hot" and "unresolved_refs" not in route

    # A text agent's output is a string: a field ref into it is not reported (publish warns instead).
    text_id = publish_agent(worker.engine, tenant, agent_spec())
    run_id = _start(worker, client, tenant, _branch_only(text_id, "steps.triage.output.priority"), {"q": "hi"})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    route = _completed(event_rows(worker.engine, run_id), "route")
    assert route["output"]["next"] == "cold" and "unresolved_refs" not in route
