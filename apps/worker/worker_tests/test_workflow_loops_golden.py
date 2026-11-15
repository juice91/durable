"""LP.7: the DBOS step layout of loop-free workflow runs is unchanged by the loops runtime
(recorded on the pre-LP.7 code). A changed layout would break recovery of runs started before."""

from __future__ import annotations

import pytest

from worker_tests.conftest import APP_VERSION, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    demo_spec,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
)


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _run(worker, client, tenant, spec, input_value):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION,
    )
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


EVENT = "eap.append_event"
BEGIN = "eap.workflow_step_begin"
AGENT = ["eap.workflow_resolve_agent", "eap.agent_run", "DBOS.getResult"]
GOLDEN_DEMO = [
    "eap.start_workflow_run", EVENT,
    BEGIN, *AGENT, EVENT,                                   # ask
    BEGIN, "eap.check_tool", "eap.workflow_tool_call", EVENT,  # now
    BEGIN, EVENT,                                           # route (branch)
    BEGIN, *AGENT, EVENT,                                   # done
    EVENT, "eap.finish_run",
]
GOLDEN_PARALLEL = [
    "eap.start_workflow_run", EVENT,
    BEGIN, "eap.workflow_branch", "eap.workflow_branch", "DBOS.getResult", "DBOS.getResult", EVENT,  # fan
    BEGIN, *AGENT, EVENT,                                   # join
    EVENT, "eap.finish_run",
]


def test_loop_free_demo_keeps_its_steps(worker, client, worker_settings, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    run_id, run = _run(worker, client, tenant, demo_spec(agent_id), {"q": "hello"})
    assert run["status"] == "completed", run["error"]
    names = step_names(worker_settings.dbos_system_schema, run_id)
    print(names)
    assert names == GOLDEN_DEMO


def test_parallel_and_branch_keep_their_steps(worker, client, worker_settings, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {
        "name": "Fan out", "input_schema": {"type": "object"}, "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "join"},
            {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"p": "left"}, "next": "join"},
            {"id": "b", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "join"},
            {"id": "join", "type": "agent", "agent_id": agent_id, "input": {"p": {"$ref": "steps.a.output"}}},
        ],
    }
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "completed", run["error"]
    names = step_names(worker_settings.dbos_system_schema, run_id)
    print(names)
    assert names == GOLDEN_PARALLEL
    branch = step_names(worker_settings.dbos_system_schema, f"{run_id}:fan:a")
    print(branch)
    assert branch == [BEGIN, *AGENT, EVENT]


ITERATION = "eap.workflow_iteration"
GOLDEN_FOREACH = [  # LP.10: three items, concurrency 2 (a sliding window in item order)
    "eap.start_workflow_run", EVENT,
    BEGIN, ITERATION, ITERATION,                            # each: start items 0 and 1
    "DBOS.getResult", EVENT, ITERATION,                     # await 0, loop:each.0:completed, start 2
    "DBOS.getResult", EVENT, "DBOS.getResult", EVENT,       # await 1, await 2
    EVENT,                                                  # step:each:completed
    EVENT, "eap.finish_run",
]


def test_foreach_window_steps(worker, client, worker_settings, tenant, defs):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    spec = {
        "name": "Each", "input_schema": {"type": "object"}, "start": "each",
        "steps": [{"id": "each", "type": "foreach", "items": {"$ref": "input.items"}, "max_items": 3,
                   "concurrency": 2, "body": {"start": "x", "steps": [tool]}}],
    }
    run_id, run = _run(worker, client, tenant, spec, {"items": [1, 2, 3]})
    assert run["status"] == "completed", run["error"]
    schema = worker_settings.dbos_system_schema
    assert step_names(schema, run_id) == GOLDEN_FOREACH
    assert step_names(schema, f"{run_id}:loop:each.0") == [EVENT, BEGIN, "eap.check_tool", "eap.workflow_tool_call", EVENT]
