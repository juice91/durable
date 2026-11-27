"""A run a verified webhook delivery started names its trigger and delivery in `run_started.source` (and only that run does)."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.workflow_run_helpers import canonical, cleanup_definitions, publish_workflow

SPEC = {
    "name": "Hook run", "input_schema": {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]},
    "start": "now", "steps": [{"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}}],
}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM webhook_deliveries WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM webhook_triggers WHERE tenant_id = :t"), {"t": tenant})


def _start(worker, client, tenant, workflow_id, version_id, *, trigger_id=None, run_id=None):
    run_id = run_id or uuid.uuid4()
    input_json = canonical({"repo": "acme/api"})
    worker.run_store.create_workflow_run(tenant, workflow_id, 1, input_json, run_id, trigger_id=trigger_id)
    client.enqueue(
        {"queue_name": "eap-agent-runs", "workflow_name": "eap.workflow_run", "workflow_id": str(run_id), "app_version": APP_VERSION},
        str(run_id), tenant, version_id, input_json,
    )
    return run_id


def test_run_started_names_the_trigger_and_the_delivery(worker, client, tenant, defs):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, SPEC)
    trigger_id, delivery_id, run_id = str(uuid.uuid4()), str(uuid.uuid4()), uuid.uuid4()
    with worker.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO webhook_triggers (id, tenant_id, name, workflow_id, preset, secrets, actions, config_hash, created_by)"
            " VALUES (:i, :t, 'gh-push', :w, 'github', CAST(:s AS JSONB), CAST('{}' AS JSONB), 'h', 'u')"
        ), {"i": trigger_id, "t": tenant, "w": workflow_id, "s": json.dumps([{"ref": "secret:x"}])})
        conn.execute(text(
            "INSERT INTO webhook_deliveries (id, tenant_id, trigger_id, outcome, state, run_id, dedupe_key)"
            " VALUES (:d, :t, :i, 'accepted', 'done', :r, 'body:abc')"
        ), {"d": delivery_id, "t": tenant, "i": trigger_id, "r": str(run_id)})
    _start(worker, client, tenant, workflow_id, version_id, trigger_id=trigger_id, run_id=run_id)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    started = next(e for e in event_rows(worker.engine, str(run_id)) if e[2] == "run_started")
    data = started[3] if isinstance(started[3], dict) else json.loads(started[3])
    assert data["source"] == {"kind": "webhook", "trigger_id": trigger_id, "trigger_name": "gh-push", "delivery_id": delivery_id}
    assert "payload" not in json.dumps(data["source"]) and worker.run_store.get_run(tenant, run_id)["trigger_id"] == trigger_id


def test_a_run_no_webhook_started_has_no_source(worker, client, tenant, defs):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, SPEC)
    run_id = _start(worker, client, tenant, workflow_id, version_id)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    started = next(e for e in event_rows(worker.engine, str(run_id)) if e[2] == "run_started")
    data = started[3] if isinstance(started[3], dict) else json.loads(started[3])
    assert "source" not in data
