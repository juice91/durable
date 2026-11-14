"""AC.8 acceptance: the step_waiting event and its inbox row are written exactly once, also when
the approval_begin step is re-executed (DBOS replay), with the event key unchanged."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from eap_run_store import ApprovalStore
from worker_tests.conftest import event_rows
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent

DATA = {"step_id": "approve", "prompt": "OK?", "timeout_hours": 1, "workflow_id": "wf-1"}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _begin(run_id, tenant, timeout):
    from eap_worker.workflow_run import approval_begin_step

    return asyncio.run(approval_begin_step(str(run_id), tenant, "approve", dict(DATA), timeout))


def _make_run(worker, tenant):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, uuid.UUID(str(agent_id)), 1, "x", run_id)
    assert worker.run_store.mark_running(tenant, run_id)
    return run_id


def _pa_count(engine, tenant, run_id) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM pending_approvals WHERE tenant_id=:t AND run_id=:r"),
            {"t": tenant, "r": str(run_id)},
        ).scalar_one()


def test_replayed_begin_step_writes_one_event_one_row_and_keeps_the_first_deadline(worker, tenant, defs):
    run_id = _make_run(worker, tenant)
    first = _begin(run_id, tenant, 3600)
    row1 = ApprovalStore(worker.engine).get(tenant, str(run_id), "step", "approve")
    assert row1 is not None and row1["decision"] is None and row1["prompt"] == "OK?"
    assert row1["deadline"].timestamp() == pytest.approx(first, abs=0.001)

    second = _begin(run_id, tenant, 99999)  # a replay computes a later deadline; the event's stands
    third = _begin(run_id, tenant, 5)
    assert second == pytest.approx(first, abs=0.001) and third == pytest.approx(first, abs=0.001)

    events = [(k, t) for _, k, t, _ in event_rows(worker.engine, run_id) if k == "step:approve:waiting"]
    assert events == [("step:approve:waiting", "step_waiting")]
    assert _pa_count(worker.engine, tenant, run_id) == 1
    row2 = ApprovalStore(worker.engine).get(tenant, str(run_id), "step", "approve")
    assert row2["requested_at"] == row1["requested_at"] and row2["deadline"] == row1["deadline"]


def test_replay_after_the_decision_does_not_reopen_the_row(worker, tenant, defs):
    run_id = _make_run(worker, tenant)
    _begin(run_id, tenant, 3600)
    outcome, _ = worker.event_log.append_step_decision(
        tenant, run_id, "approve", "step_approved", {"step_id": "approve", "decided_by": "alice", "comment": None})
    assert outcome == "created"
    _begin(run_id, tenant, 3600)
    row = ApprovalStore(worker.engine).get(tenant, str(run_id), "step", "approve")
    assert row["decision"] == "approved" and row["decided_by"] == "alice"
    assert _pa_count(worker.engine, tenant, run_id) == 1
    assert ApprovalStore(worker.engine).list(tenant, "pending")[0] == []
