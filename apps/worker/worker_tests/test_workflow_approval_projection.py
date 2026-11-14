"""AC.8: eap.workflow_approval_begin writes the approvals inbox row with the step_waiting event
(EventLog.append_approval_waiting), and the decision (person or timeout) closes it."""

from __future__ import annotations

import pytest

from eap_run_store import ApprovalStore
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
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


def _start(worker, client, tenant, spec, input_value):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION,
    )
    return workflow_id, run_id


def test_waiting_step_is_in_the_inbox_until_decided(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    workflow_id, run_id = _start(worker, client, tenant, demo_spec(agent_id), {"q": "urgent"})
    waiting = wait_event(worker.engine, run_id, "step:approve:waiting")
    store = ApprovalStore(worker.engine)
    rows, _ = store.list(tenant, "pending")
    assert len(rows) == 1
    row = rows[0]
    assert (row["run_id"], row["kind"], row["approval_key"], row["root_run_id"]) == (
        str(run_id), "step", "approve", str(run_id))
    assert row["workflow_id"] == str(workflow_id) and row["prompt"] == waiting["data"]["prompt"]
    assert row["deadline"].isoformat() == waiting["data"]["deadline"]
    assert worker.run_store.get_run(tenant, run_id)["display_status"] == "waiting_approval"

    assert decide(worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="approve") == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert store.list(tenant, "pending")[0] == []
    (decided,), _ = store.list(tenant, "decided")
    assert (decided["decision"], decided["decided_by"]) == ("approved", "alice")
    # Event keys and types are unchanged by the projection write.
    keys = [(k, t) for _, k, t, _ in event_rows(worker.engine, run_id)]
    assert ("step:approve:waiting", "step_waiting") in keys


def test_timed_out_step_closes_its_row(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "1")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.5")
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    _, run_id = _start(worker, client, tenant, demo_spec(agent_id), {"q": "urgent"})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    row = ApprovalStore(worker.engine).get(tenant, str(run_id), "step", "approve")
    assert row is not None and row["decision"] == "timed_out" and row["decided_by"] is None
