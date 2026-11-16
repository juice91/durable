"""LP.8 acceptance: POST /runs/{id}/cancel against a real in-process worker and real DBOS.

The implementer's tests stay as they are. The API (TestClient) uses a DBOSClient on the worker's
private schema, so the cancel reaches the real DBOS tree. Models and tools are gated by patching
the scripted resolvers; nothing here uses sleeps as assertions except the "nothing follows" windows.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import text

from worker_tests.cancel_acceptance_helpers import (
    Gate, assert_cancelled_tree, ends, gated_echo_model, snapshot, stays, tree_ids, usage_rows,
)
from worker_tests.conftest import APP_VERSION, event_rows, wait_for
from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api
from worker_tests.workflow_run_helpers import (
    agent_spec, canonical, cleanup_definitions, publish_agent, publish_workflow, wait_event,
)


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


@pytest.fixture
def api(worker_settings):
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as c:
        yield c


@pytest.fixture
def gate(monkeypatch):
    g = Gate()
    monkeypatch.setattr("eap_worker.models.scripted_echo_model", gated_echo_model(g))
    yield g
    g.release.set()  # never leave a parked model behind


def _cancel(api, tenant, run_id, user="alice"):
    return api.post(f"/api/v1/runs/{run_id}/cancel", headers=auth_header(tenant, user))


def _start(api, tenant, workflow_id) -> str:
    run_id = str(uuid.uuid4())
    r = api.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {}, "run_id": run_id},
                 headers=auth_header(tenant))
    assert r.status_code == 202, r.text
    return run_id


def _publish(worker, tenant, steps, start) -> str:
    spec = {"name": "Cancel me", "input_schema": {"type": "object"}, "start": start, "steps": steps}
    return publish_workflow(worker.engine, tenant, spec)[0]


def _agent_step(agent_id, sid="a", nxt=None):
    step = {"id": sid, "type": "agent", "agent_id": agent_id, "input": {"prompt": "hi"}}
    if nxt:
        step["next"] = nxt
    return step


def _dbos_status(client, run_id) -> str:
    return client.retrieve_workflow(str(run_id)).get_status().status


def test_cancel_running_tree_with_child_and_waiting_approval(worker, client, api, tenant, defs, gate):
    """Parallel: a child agent run parked in its model call, and a top-level approval waiting."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    wf = _publish(worker, tenant, [
        {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "done"},
        _agent_step(agent_id, "a", "done"),
        {"id": "b", "type": "approval", "prompt": "OK?", "timeout_hours": 1, "next": "done"},
        {"id": "done", "type": "tool", "tool_id": "clock.now", "input": {}},
    ], "fan")
    run_id = _start(api, tenant, wf)
    wait_event(worker.engine, run_id, "step:b:waiting")
    assert gate.entered.wait(60)
    ids = tree_ids(worker.engine, run_id)
    assert len(ids) >= 2  # the root and the agent step's child run
    with worker.engine.connect() as conn:
        open_rows = conn.execute(text(
            "SELECT count(*) FROM pending_approvals WHERE tenant_id=:t AND decision IS NULL"), {"t": tenant}).scalar_one()
    assert open_rows == 1

    r = _cancel(api, tenant, run_id)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "failed" and body["error_code"] == "run_cancelled" and body["cancelled"] is True
    assert body["approvals_closed"] == 1
    assert_cancelled_tree(worker.engine, worker.run_store, tenant, ids)
    for rid in ids:
        assert _dbos_status(client, rid) == "CANCELLED", rid
    with worker.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT decision, decided_by FROM pending_approvals WHERE tenant_id=:t"), {"t": tenant}).fetchall()
    assert [tuple(x) for x in rows] == [("denied", "system:run_cancelled")]
    # approve after the cancel: the step is no longer waiting.
    late = api.post(f"/api/v1/workflows/runs/{run_id}/steps/b/approve", headers=auth_header(tenant))
    assert late.status_code == 409, late.text

    before = snapshot(worker.engine, ids)
    gate.release.set()  # the parked model request finishes after the cancel
    assert stays(lambda: snapshot(worker.engine, ids) == before
                 and all(worker.run_store.get_run(tenant, uuid.UUID(i))["status"] == "failed" for i in ids))
    assert all(len(ends(event_rows(worker.engine, i))) == 1 for i in ids)


def test_cancel_before_start_never_starts_or_calls_a_model(worker, client, api, tenant, defs, gate):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    wf = _publish(worker, tenant, [_agent_step(agent_id)], "a")
    run_id = str(uuid.uuid4())
    worker.run_store.create_workflow_run(tenant, wf, 1, canonical({}), uuid.UUID(run_id))
    with worker.engine.connect() as conn:
        version_id = conn.execute(text("SELECT id FROM workflow_versions WHERE workflow_id=:w"), {"w": wf}).scalar_one()
    client.enqueue(
        {"queue_name": "eap-agent-runs", "workflow_name": "eap.workflow_run", "workflow_id": run_id,
         "app_version": APP_VERSION, "delay_seconds": 6},
        run_id, tenant, str(version_id), canonical({}),
    )
    r = _cancel(api, tenant, run_id)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "failed"
    assert _dbos_status(client, run_id) == "CANCELLED"
    time.sleep(9)  # past the delay: a worker would have started it by now
    assert [e[1] for e in event_rows(worker.engine, run_id)] == ["cancel", "end"]
    assert worker.run_store.get_run(tenant, uuid.UUID(run_id))["status"] == "failed"
    assert gate.calls == 0 and tree_ids(worker.engine, run_id) == [run_id]


def test_cancel_mid_model_call_then_after_end_is_409(worker, client, api, tenant, defs, gate):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    wf = _publish(worker, tenant, [_agent_step(agent_id)], "a")
    run_id = _start(api, tenant, wf)
    assert gate.entered.wait(60)
    ids = tree_ids(worker.engine, run_id)
    assert _cancel(api, tenant, run_id).status_code == 202
    assert_cancelled_tree(worker.engine, worker.run_store, tenant, ids)
    gate.release.set()
    before = snapshot(worker.engine, ids)
    assert stays(lambda: snapshot(worker.engine, ids) == before, 3)
    # An ordinary run that finished on its own: 409, nothing appended.
    done_id = _start(api, tenant, wf)
    wait_for(lambda: worker.run_store.get_run(tenant, uuid.UUID(done_id))["status"] == "completed", 60, what="run")
    after = event_rows(worker.engine, done_id)
    r = _cancel(api, tenant, done_id)
    assert r.status_code == 409 and r.json() == {"detail": "the run has already ended"}
    assert event_rows(worker.engine, done_id) == after


def test_usage_of_a_model_request_finishing_after_the_cancel_is_kept(worker, client, api, tenant, defs, gate):
    """LP.8: the model request that finishes after the cancel writes nothing visible, but its usage
    row is kept (the cost is real)."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    wf = _publish(worker, tenant, [_agent_step(agent_id)], "a")
    run_id = _start(api, tenant, wf)
    assert gate.entered.wait(60)
    ids = tree_ids(worker.engine, run_id)
    assert _cancel(api, tenant, run_id).status_code == 202
    gate.release.set()
    child = [i for i in ids if i != run_id][0]
    time.sleep(5)
    assert usage_rows(worker.engine, child) >= 1, "the finished request's usage row was not kept"


def test_cancel_mid_tool_call_with_a_side_effect_needs_attention(worker, client, api, tenant, defs, monkeypatch):
    """D12: the side effect is still `reserved`, so the run that owns it ends needs_attention."""
    from eap_worker.tickets import FakeExternalTicketService

    gate = Gate()
    real = FakeExternalTicketService.create

    def parked(self, *args, **kwargs):
        gate.calls += 1
        gate.entered.set()
        gate.release.wait(60)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(FakeExternalTicketService, "create", parked)
    agent_id = publish_agent(worker.engine, tenant, agent_spec("ticket", ("tickets.create",)))
    wf = _publish(worker, tenant, [_agent_step(agent_id)], "a")
    run_id = _start(api, tenant, wf)
    try:
        assert gate.entered.wait(60)
        ids = tree_ids(worker.engine, run_id)
        r = _cancel(api, tenant, run_id)
        assert r.status_code == 202, r.text
        child = [i for i in ids if i != run_id][0]
        got = {i: worker.run_store.get_run(tenant, uuid.UUID(i))["status"] for i in ids}
        assert got[child] == "needs_attention", got
        assert r.json()["status"] == got[run_id] and got[run_id] in {"failed", "needs_attention"}
        for i in ids:
            assert len(ends(event_rows(worker.engine, i))) == 1
            assert event_rows(worker.engine, i)[-1][1] == "end"
        before = snapshot(worker.engine, ids)
    finally:
        gate.release.set()
    assert stays(lambda: snapshot(worker.engine, ids) == before
                 and {i: worker.run_store.get_run(tenant, uuid.UUID(i))["status"] for i in ids} == got)


def _gated_tree(worker, api, tenant, gate):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    wf = _publish(worker, tenant, [_agent_step(agent_id)], "a")
    run_id = _start(api, tenant, wf)
    assert gate.entered.wait(60)
    return run_id, tree_ids(worker.engine, run_id)


def test_api_crash_between_cascade_and_end_is_finished_by_the_reconciler(
    worker, client, api, tenant, defs, gate, monkeypatch
):
    from dbos import DBOS

    from eap_api.routes import workflow_runs
    from eap_worker.cancel_reconcile import (
        CANCEL_RECONCILE_SCHEDULE_NAME, ReconcileConfig, apply_cancel_reconcile_schedule,
    )

    run_id, ids = _gated_tree(worker, api, tenant, gate)

    def crash(*_a):
        raise RuntimeError("API died after the cascade")

    monkeypatch.setattr(workflow_runs, "_finish_cancel", crash)
    assert _cancel(api, tenant, run_id).status_code == 503
    # The DBOS tree is cancelled but no end was written: the run is stuck without the reconciler.
    assert _dbos_status(client, run_id) == "CANCELLED"
    assert [e[1] for e in event_rows(worker.engine, run_id) if e[1] in ("cancel", "end")] == ["cancel"]
    assert worker.run_store.get_run(tenant, uuid.UUID(run_id))["status"] == "running"
    try:
        assert apply_cancel_reconcile_schedule(
            ReconcileConfig(schedule="*/2 * * * * *", after_seconds=1, limit=1000)) == "applied"
        wait_for(lambda: all(len(ends(event_rows(worker.engine, i))) == 1 for i in ids), 60,
                 what="the reconciler to finish the tree")
    finally:
        DBOS.delete_schedule(CANCEL_RECONCILE_SCHEDULE_NAME)
    assert_cancelled_tree(worker.engine, worker.run_store, tenant, ids)
    gate.release.set()
    before = snapshot(worker.engine, ids)
    assert stays(lambda: snapshot(worker.engine, ids) == before, 3)
    assert len([e for e in event_rows(worker.engine, run_id) if e[1] == "cancel"]) == 1


def test_a_retried_cancel_after_the_crash_finishes_at_once(worker, client, api, tenant, defs, gate, monkeypatch):
    from eap_api.routes import workflow_runs

    run_id, ids = _gated_tree(worker, api, tenant, gate)
    real = workflow_runs._finish_cancel
    monkeypatch.setattr(workflow_runs, "_finish_cancel", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert _cancel(api, tenant, run_id).status_code == 503
    monkeypatch.setattr(workflow_runs, "_finish_cancel", real)
    r = _cancel(api, tenant, run_id)
    assert r.status_code == 202 and r.json()["status"] == "failed"
    assert_cancelled_tree(worker.engine, worker.run_store, tenant, ids)


def test_double_and_concurrent_cancels_produce_one_end_each(worker, client, api, tenant, defs, gate):
    run_id, ids = _gated_tree(worker, api, tenant, gate)
    results: list[int] = []
    bodies: list[dict] = []

    def post():
        r = _cancel(api, tenant, run_id)
        results.append(r.status_code)
        bodies.append(r.json())

    threads = [threading.Thread(target=post) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert results == [202] * 6, bodies
    assert {b["status"] for b in bodies} == {"failed"}
    assert sum(b["approvals_closed"] for b in bodies) == 0
    assert _cancel(api, tenant, run_id).status_code == 202  # a later double cancel
    assert_cancelled_tree(worker.engine, worker.run_store, tenant, ids)
    root = event_rows(worker.engine, run_id)
    assert len([e for e in root if e[1] == "cancel"]) == 1
    assert [e[0] for e in root] == list(range(len(root)))  # seq stays gap-free


def test_cross_tenant_and_unsupported_kinds(worker, client, api, tenant, defs, gate):
    run_id, ids = _gated_tree(worker, api, tenant, gate)
    assert _cancel(api, "t_other_" + uuid.uuid4().hex[:6], run_id).status_code == 404
    assert [e[1] for e in event_rows(worker.engine, run_id) if e[1] in ("cancel", "end")] == []
    child = [i for i in ids if i != run_id][0]
    r = _cancel(api, tenant, child)  # a workflow's child agent run is not cancellable on its own
    assert r.status_code == 409 and r.json()["code"] == "cancel_not_supported"
    assert worker.run_store.get_run(tenant, uuid.UUID(child))["status"] == "running"


def test_a_dropped_append_keeps_its_usage_row(worker, tenant):
    """Store level: an event passed after the end is dropped, the usage row is written (D12)."""
    import asyncio

    from eap_worker.workflow import append_event_step

    run_id = uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, uuid.uuid4(), 1, "{}", run_id)
    worker.event_log.append(tenant, run_id, "start", "run_started", {})
    worker.run_store.cancel_run_tree(tenant, run_id)
    usage = {"model_id": "test:echo", "provider_model_name": "scripted-echo", "input_tokens": 10,
             "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True}
    seq = asyncio.run(append_event_step(
        str(run_id), tenant, "model:0:final", "model_message", {"model_request": 0}, usage, "model:0"))
    events = event_rows(worker.engine, run_id)
    assert [e[1] for e in events] == ["start", "end"] and seq == events[-1][0]
    assert usage_rows(worker.engine, run_id) == 1
    # And again: the usage row is first-write-wins, not duplicated.
    asyncio.run(append_event_step(
        str(run_id), tenant, "model:0:final", "model_message", {"model_request": 0}, usage, "model:0"))
    assert usage_rows(worker.engine, run_id) == 1
