"""AC.9 acceptance: structured agent output flows between steps, survives a crash, and old text
results are read unchanged. Worker subprocesses are always stopped in `finally`."""

from __future__ import annotations

import json
import uuid

import pytest
from dbos import DBOSClient

from eap_worker.workflow_run import agent_step_output
from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
    wait_event,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "priority": {"type": "string"},
        "score": {"type": "integer"},
        "owner": {"type": "object", "properties": {"name": {"type": "string"}}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["priority", "score", "owner", "tags"],
}
OBJ = {"priority": "high", "score": 7, "owner": {"name": "ada"}, "tags": ["disk", "db"]}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _structured() -> dict:
    spec = agent_spec(model="structured", name="Triage")
    spec["output"] = {"json_schema": SCHEMA}
    return spec


def _keys(events) -> list[str]:
    return [k for _, k, _, _ in events]


def _done(events, step_id):
    return next(d for _, k, _, d in events if k == f"step:{step_id}:completed")


def _flow_spec(agent_id: str, text_id: str) -> dict:
    return {
        "name": "Flow",
        "input_schema": {"type": "object"},
        "start": "triage",
        "steps": [
            {"id": "triage", "type": "agent", "agent_id": agent_id,
             "input": {"prompt": {"$ref": "input.q"}}, "next": "route"},
            {"id": "route", "type": "branch",
             "cases": [{"when": {"op": "eq", "left": {"$ref": "steps.triage.output.owner.name"}, "right": "ada"},
                        "next": "ticket"}],
             "default": "nope"},
            {"id": "ticket", "type": "tool", "tool_id": "tickets.create",
             "input": {"title": {"$ref": "steps.triage.output.priority"},
                       "body": {"$ref": "steps.triage.output.tags.1"}},
             "next": "echo"},
            {"id": "echo", "type": "agent", "agent_id": text_id,
             "input": {"prompt": {"$ref": "steps.triage.output.owner.name"}}},
            {"id": "nope", "type": "agent", "agent_id": text_id, "input": {"prompt": "no"}},
        ],
    }


def test_agent_step_output_edge_cases():
    # Old (pre-AC.4) result shapes: no output_json key, or an explicit null: text, unchanged.
    assert agent_step_output({"status": "completed", "output": ""}) == ""
    assert agent_step_output({"output": "5", "output_json": None}) == "5"
    assert agent_step_output({"output": '{"a":1}'}) == '{"a":1}'  # JSON-looking text stays text
    assert agent_step_output({}) is None
    # A structured result wins, including falsy objects.
    assert agent_step_output({"output": "{}", "output_json": {}}) == {}
    assert agent_step_output({"output": "[1]", "output_json": [1, 2]}) == [1, 2]
    assert agent_step_output({"output": "0", "output_json": 0}) == 0


def test_object_feeds_condition_nested_field_array_ref_and_tool_input(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, _structured())
    text_id = publish_agent(worker.engine, tenant, agent_spec())
    workflow_id, version_id = publish_workflow(worker.engine, tenant, _flow_spec(agent_id, text_id))
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={"q": json.dumps({"output": OBJ})}, app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    events = event_rows(worker.engine, run_id)
    assert _done(events, "triage")["output"] == OBJ
    assert _done(events, "route")["output"]["next"] == "ticket"
    assert _done(events, "echo")["output"] == "Echo: ada"
    assert "step:nope:started" not in _keys(events)
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1  # tool ran once with refs resolved
    assert [e[0] for e in events] == list(range(len(events)))


def _crash_after_triage(
    worker, tenant, crash_schema, tmp_path, agent_id, text_id, q,
    crash_at: str = "workflow_mid_step", crash_key: str = "step:route:started",
):
    """Kill the worker at `crash_at` for event `crash_key` (by default once step `route` has
    started, so the structured step is already recorded), then recover on a second worker."""
    executor_id = f"wf-ac9-{uuid.uuid4().hex[:8]}"
    workflow_id, version_id = publish_workflow(worker.engine, tenant, _flow_spec(agent_id, text_id))
    first = WorkerProcess(
        _env(crash_schema, executor_id, EAP_CRASH_AT=crash_at, EAP_CRASH_EVENT_KEY=crash_key),
        tmp_path / "w1.log",
    )
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(
            dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
            input_value={"q": q}, app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        wait_event(worker.engine, run_id, crash_key)
        before = event_rows(worker.engine, run_id)
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        return run_id, run, before, event_rows(worker.engine, run_id), (tmp_path / "w2.log")
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_crash_after_structured_step_replays_the_same_object_without_duplicates(
    worker, tenant, defs, crash_schema, tmp_path
):
    agent_id = publish_agent(worker.engine, tenant, _structured())
    text_id = publish_agent(worker.engine, tenant, agent_spec())
    run_id, run, before, events, log = _crash_after_triage(
        worker, tenant, crash_schema, tmp_path, agent_id, text_id, json.dumps({"output": OBJ})
    )
    assert run["status"] == "completed", (run["error"], log.read_text())
    assert _done(before, "triage")["output"] == OBJ
    assert _done(events, "triage") == _done(before, "triage")  # identical recorded step output
    assert [e[0] for e in events] == list(range(len(events)))
    assert len(set(_keys(events))) == len(events)  # no duplicate events
    assert _keys(events).count("step:triage:completed") == 1
    assert _keys(events)[:len(before)] == _keys(before)
    assert _done(events, "route")["output"]["next"] == "ticket"
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
    with worker.engine.connect() as conn:
        from sqlalchemy import text as sql
        n = conn.execute(sql("SELECT count(*) FROM runs WHERE parent_run_id = :r AND parent_step_id = 'triage'"),
                         {"r": str(run_id)}).scalar()
    assert n == 1


def test_crash_recovery_of_a_text_agent_keeps_a_json_looking_output_as_text(
    worker, tenant, defs, crash_schema, tmp_path
):
    """The old recorded shape: a text child (no output_json). Replay yields the same string."""
    text_id = publish_agent(worker.engine, tenant, agent_spec())
    # The 'triage' step is a plain echo agent: its output is the string "Echo: {...}".
    run_id, run, before, events, log = _crash_after_triage(
        worker, tenant, crash_schema, tmp_path, text_id, text_id, '{"owner": {"name": "ada"}}'
    )
    out = _done(before, "triage")["output"]
    assert isinstance(out, str) and out.startswith("Echo: ")
    assert _done(events, "triage")["output"] == out
    # Field refs into a text output never resolve, so the branch takes its default.
    assert run["status"] == "completed", (run["error"], log.read_text())
    assert _done(events, "route")["output"]["next"] == "nope"
    assert [e[0] for e in events] == list(range(len(events)))


def test_crash_between_child_completion_and_step_completed_checkpoint_replays_the_same_object(
    worker, tenant, defs, crash_schema, tmp_path
):
    """Review fix: the `step:triage:completed` event commits, then the worker dies before the
    append step's checkpoint. Recovery rebuilds the output from the recorded child result, re-runs
    the append (idempotent on its key) and the branch reads the same object."""
    agent_id = publish_agent(worker.engine, tenant, _structured())
    text_id = publish_agent(worker.engine, tenant, agent_spec())
    run_id, run, before, events, log = _crash_after_triage(
        worker, tenant, crash_schema, tmp_path, agent_id, text_id, json.dumps({"output": OBJ}),
        crash_at="after_event_write", crash_key="step:triage:completed",
    )
    assert run["status"] == "completed", (run["error"], log.read_text())
    assert _keys(before)[-1] == "step:triage:completed" and "step:route:started" not in _keys(before)
    assert _done(before, "triage")["output"] == OBJ
    assert _keys(events).count("step:triage:completed") == 1
    assert len(set(_keys(events))) == len(events) and [e[0] for e in events] == list(range(len(events)))
    assert _done(events, "route")["output"]["next"] == "ticket"
    assert _done(events, "echo")["output"] == "Echo: ada"
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
