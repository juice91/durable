"""External runs in the worker (inbound design 4.4, 7.1 and 7.3): `run_started.source` names the external client, and
the scheduled `eap.external_run_sweep` cancels overdue runs (parked on an approval included), starts runs whose
hand-off was lost and forgets claims that never got a run."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

# Imported at collection, so the workflow is registered before the session worker launches DBOS.
from eap_worker.external_run_sweep import (
    EXTERNAL_SWEEP_SCHEDULE_NAME,
    ExternalSweepConfig,
    ExternalSweepConfigError,
    apply_external_sweep_schedule,
    external_run_sweep,
)
from eap_run_store.external_access import ClaimLimits, ExternalAccessStore
from worker_tests.conftest import APP_VERSION, event_rows, wait_for, wait_terminal
from worker_tests.workflow_run_helpers import canonical, cleanup_definitions, publish_workflow

SPEC = {
    "name": "External run", "input_schema": {"type": "object"},
    "start": "now", "steps": [{"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}}],
}
APPROVAL_SPEC = {
    "name": "Gated", "input_schema": {"type": "object"}, "start": "ok_gate",
    "steps": [{"id": "ok_gate", "type": "approval", "prompt": "go?", "timeout_hours": 24}],
}


class Ext:
    """One tenant's client and export, and claims made as the API makes them."""

    def __init__(self, worker, tenant: str) -> None:
        self.worker, self.tenant = worker, tenant
        self.store = ExternalAccessStore(worker.engine)
        self.client = self.store.create_client(tenant, "admin", name="claude")

    def export(self, workflow_id: str) -> dict:
        return self.store.create_export(self.tenant, "admin", {
            "name": f"x_{uuid.uuid4().hex[:8]}", "title": "t", "workflow_id": workflow_id, "workflow_version": None,
            "protocols": ["mcp"], "config_hash": "h",
        })

    def run(self, spec: dict = SPEC, *, deadline: timedelta = timedelta(hours=1), enqueue_with=None, create_run: bool = True):
        workflow_id, version_id = publish_workflow(self.worker.engine, self.tenant, spec)
        export = self.export(workflow_id)
        run_id = uuid.uuid4()
        self.store.claim_run(
            self.tenant, client_id=self.client["id"], key_id="b" * 24, export_id=export["id"], protocol="mcp",
            idempotency_hash=None, input_hash="i", run_id=str(run_id), workflow_version=1,
            deadline_at=datetime.now(timezone.utc) + deadline, context_label=None, limits=ClaimLimits(50, 50, 50),
        )
        input_json = canonical({})
        if create_run:
            self.worker.run_store.create_workflow_run(self.tenant, workflow_id, 1, input_json, run_id)
        if enqueue_with is not None:
            enqueue_with.enqueue(
                {"queue_name": "eap-agent-runs", "workflow_name": "eap.workflow_run", "workflow_id": str(run_id),
                 "app_version": APP_VERSION}, str(run_id), self.tenant, version_id, input_json,
            )
        return run_id

    def row(self, run_id: uuid.UUID) -> dict | None:
        with self.worker.engine.connect() as conn:
            found = conn.execute(text("SELECT * FROM external_runs WHERE run_id = :r"), {"r": str(run_id)}).mappings().first()
        return None if found is None else dict(found)


@pytest.fixture
def ext(worker, tenant):
    yield Ext(worker, tenant)
    with worker.engine.begin() as conn:
        for table in ("external_runs", "workflow_exports", "external_clients"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
    cleanup_definitions(worker.engine, tenant)


def sweep() -> dict:
    from dbos import DBOS

    return DBOS.start_workflow(external_run_sweep, datetime.now(timezone.utc), {"limit": 1000}).get_result()


def test_run_started_names_the_external_client_the_key_and_the_export(worker, client, ext):
    run_id = ext.run(enqueue_with=client)
    assert wait_terminal(worker.run_store, ext.tenant, run_id)["status"] == "completed"
    started = next(e for e in event_rows(worker.engine, str(run_id)) if e[2] == "run_started")
    data = started[3] if isinstance(started[3], dict) else json.loads(started[3])
    assert data["source"]["kind"] == "external_mcp" and data["source"]["client_name"] == "claude"
    assert data["source"]["client_id"] == ext.client["id"] and data["source"]["key_id"] == "b" * 24
    assert data["source"]["export"].startswith("x_") and set(data["source"]) == {
        "kind", "client_id", "client_name", "key_id", "export"}


def test_an_overdue_run_parked_on_an_approval_is_cancelled_with_the_deadline_as_its_reason(worker, client, ext):
    run_id = ext.run(APPROVAL_SPEC, enqueue_with=client)
    wait_for(
        lambda: (worker.run_store.get_run(ext.tenant, run_id) or {}).get("display_status") == "waiting_approval",
        timeout=60, what="the run to wait for its approval",
    )
    assert ext.row(run_id)["cancel_reason"] is None
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE external_runs SET deadline_at = now() - interval '1 second' WHERE run_id = :r"),
                     {"r": str(run_id)})
    summary = sweep()
    assert summary["cancelled"] >= 1 and summary["failed"] == 0
    run = worker.run_store.get_run(ext.tenant, run_id)
    assert run["status"] == "failed"
    ends = [e for e in event_rows(worker.engine, str(run_id)) if e[1] == "end"]
    assert len(ends) == 1 and ends[0][3]["error_code"] == "run_cancelled"
    assert ext.row(run_id)["cancel_reason"] == "deadline_exceeded"
    with worker.engine.connect() as conn:
        open_rows = conn.execute(text("SELECT count(*) FROM pending_approvals WHERE root_run_id = :r AND decided_at IS NULL"),
                                 {"r": str(run_id)}).scalar_one()
    assert open_rows == 0
    assert client.retrieve_workflow(str(run_id)).get_status().status == "CANCELLED"
    sweep()  # the next tick closes the ended claim and cancels nothing more
    assert ext.row(run_id)["closed_at"] is not None
    assert len([e for e in event_rows(worker.engine, str(run_id)) if e[1] == "cancel"]) == 1


def test_a_run_inside_its_deadline_is_left_alone(worker, client, ext):
    run_id = ext.run(APPROVAL_SPEC, enqueue_with=client)
    wait_for(
        lambda: (worker.run_store.get_run(ext.tenant, run_id) or {}).get("display_status") == "waiting_approval",
        timeout=60, what="the run to wait for its approval",
    )
    sweep()
    assert worker.run_store.get_run(ext.tenant, run_id)["status"] == "running"
    assert not [e for e in event_rows(worker.engine, str(run_id)) if e[1] == "cancel"]
    worker.run_store.cancel_run_tree(ext.tenant, run_id)  # leave nothing waiting
    from dbos import DBOS

    DBOS.cancel_workflow(str(run_id), cancel_children=True)


def test_a_run_whose_hand_off_was_lost_is_started_once(worker, ext):
    run_id = ext.run()  # claim and run row, never enqueued: the API died
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE runs SET created_at = now() - interval '10 minutes' WHERE id = :r"), {"r": str(run_id)})
    assert sweep()["started"] >= 1
    assert wait_terminal(worker.run_store, ext.tenant, run_id)["status"] == "completed"
    sweep()
    assert len([e for e in event_rows(worker.engine, str(run_id)) if e[2] == "run_started"]) == 1


def test_a_freshly_queued_run_is_not_started_by_the_sweep(worker, ext):
    run_id = ext.run()
    sweep()
    assert worker.run_store.get_run(ext.tenant, run_id)["status"] == "queued"


def test_a_claim_that_never_got_a_run_is_forgotten_after_its_deadline(worker, ext):
    dead = ext.run(create_run=False, deadline=timedelta(hours=-1))
    alive = ext.run(create_run=False)
    assert sweep()["forgotten"] >= 1
    assert ext.row(dead) is None and ext.row(alive) is not None


def test_config_and_schedule(worker):
    from dbos import DBOS

    assert ExternalSweepConfig.from_env({}) == ExternalSweepConfig("0 * * * * *", 100, False)
    assert ExternalSweepConfig.from_env({"EAP_EXTERNAL_SWEEP_SCHEDULE": "off"}).off is True
    with pytest.raises(ExternalSweepConfigError):
        ExternalSweepConfig.from_env({"EAP_EXTERNAL_SWEEP_SCHEDULE": "nonsense"})
    try:
        assert apply_external_sweep_schedule(ExternalSweepConfig(schedule="*/30 * * * * *", limit=10)) == "applied"
        stored = DBOS.get_schedule(EXTERNAL_SWEEP_SCHEDULE_NAME)
        assert stored["schedule"] == "*/30 * * * * *" and stored["context"] == {"limit": 10}
        assert apply_external_sweep_schedule(ExternalSweepConfig(off=True)) == "removed"
        assert DBOS.get_schedule(EXTERNAL_SWEEP_SCHEDULE_NAME) is None
    finally:
        DBOS.delete_schedule(EXTERNAL_SWEEP_SCHEDULE_NAME)
