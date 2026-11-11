"""Worker-layer tenant ownership and model governance (review 2026-11-12).

These go through the worker's own steps, workflow and fake external service, not the API routes.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text

from eap_run_store import RunNotFound
from eap_schemas.models_policy import ModelNotAllowed, ModelPolicy
from worker_tests.conftest import APP_VERSION, cleanup_tenant, enqueue_run, event_rows, ticket_spec, wait_for, wait_terminal


@pytest.fixture
def run_store(worker):
    return worker.run_store


@pytest.fixture
def other_tenant(worker):
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    yield tenant_id
    cleanup_tenant(worker.engine, tenant_id)


def _run_row(engine, run_id) -> tuple:
    with engine.connect() as conn:
        return tuple(
            conn.execute(
                text("SELECT tenant_id, status, output, error, heartbeat_at, updated_at FROM runs WHERE id = :r"),
                {"r": str(run_id)},
            ).one()
        )


# --------------------------------------------------------------------------- tenant ownership


def test_worker_steps_refuse_another_tenants_run(worker, run_store, tenant, other_tenant):
    from eap_worker.workflow import append_event_step, finish_run_step

    run_id = uuid.uuid4()
    run_store.create_run(tenant, uuid.uuid4(), 1, "victim input", run_id)
    before = _run_row(worker.engine, run_id)

    with pytest.raises(RunNotFound):
        asyncio.run(append_event_step(str(run_id), other_tenant, "end", "run_completed", {"output": "forged"}))
    with pytest.raises(RunNotFound):
        asyncio.run(finish_run_step(str(run_id), other_tenant, "completed", "forged", None))
    with pytest.raises(RunNotFound):
        worker.run_store.heartbeat(other_tenant, run_id)

    assert _run_row(worker.engine, run_id) == before
    assert event_rows(worker.engine, run_id) == []


def test_forged_workflow_for_another_tenants_run_changes_nothing(worker, client, run_store, tenant, other_tenant):
    """A misrouted/forged queue call (tenant B, tenant A's run_id) must not touch A's run."""
    run_id = uuid.uuid4()
    run_store.create_run(tenant, uuid.uuid4(), 1, "victim input", run_id)
    before = _run_row(worker.engine, run_id)

    enqueue_run(
        client, run_store, tenant_id=other_tenant, spec=ticket_spec(), input_text="forged", app_version=APP_VERSION,
        run_id=run_id, create_row=False,
    )
    handle = client.retrieve_workflow(str(run_id))
    wait_for(lambda: handle.get_status().status in {"ERROR", "SUCCESS"}, timeout=30, what="forged workflow to end")
    assert handle.get_status().status == "ERROR"

    assert _run_row(worker.engine, run_id) == before
    assert event_rows(worker.engine, run_id) == []
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 0
    assert worker.tickets.call_count(other_tenant, f"{run_id}:") == 0
    assert run_store.get_run(other_tenant, run_id) is None


def test_events_and_tickets_carry_the_runs_tenant(worker, client, run_store, tenant):
    run_id = enqueue_run(
        client, run_store, tenant_id=tenant, spec=ticket_spec(), input_text="VPN is down", app_version=APP_VERSION,
    )
    run = wait_terminal(run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    with worker.engine.connect() as conn:
        event_tenants = conn.execute(
            text("SELECT DISTINCT tenant_id FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}
        ).scalars().all()
        ticket_tenants = conn.execute(
            text("SELECT DISTINCT tenant_id FROM fake_external_tickets WHERE idempotency_key LIKE :p"),
            {"p": f"{run_id}:%"},
        ).scalars().all()
    assert event_tenants == [tenant]
    assert ticket_tenants == [tenant]


def test_same_idempotency_key_under_two_tenants_creates_two_tickets(worker, tenant, other_tenant):
    key = f"{uuid.uuid4()}:call-1"
    a1 = worker.tickets.for_tenant(tenant).create("t", "b", idempotency_key=key)
    a2 = worker.tickets.for_tenant(tenant).create("t", "b", idempotency_key=key)
    b1 = worker.tickets.for_tenant(other_tenant).create("other", "x", idempotency_key=key)

    assert a1["ticket_id"] == a2["ticket_id"]  # deduped within a tenant
    assert b1["ticket_id"] != a1["ticket_id"]  # a separate ticket for the other tenant
    assert b1["title"] == "other"
    assert worker.tickets.ticket_count(tenant, key) == 1
    assert worker.tickets.call_count(tenant, key) == 2
    assert worker.tickets.ticket_count(other_tenant, key) == 1
    assert worker.tickets.call_count(other_tenant, key) == 1


def test_ticket_service_requires_tenant(worker):
    with pytest.raises(ValueError):
        worker.tickets.for_tenant("")
    with pytest.raises(ValueError):
        worker.tickets.create("t", "b", idempotency_key="k", tenant_id="")


# --------------------------------------------------------------------------- model governance


def test_worker_refuses_disallowed_model_published_under_an_older_policy(worker, client, run_store, tenant):
    """The version was published while `litellm:*` was allowed; the worker's policy is `test:*` now."""
    assert not worker.settings.allowed_models.allows("litellm", "qwen3.8-thinking")
    spec = ticket_spec()
    spec["model"] = {"provider": "litellm", "name": "qwen3.8-thinking"}
    run_id = enqueue_run(client, run_store, tenant_id=tenant, spec=spec, input_text="hi", app_version=APP_VERSION)

    run = wait_terminal(run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert "ModelNotAllowed" in run["error"] and "litellm:qwen3.8-thinking" in run["error"]
    # No model request was made: no model_message, and no litellm client was ever built (that would
    # have failed with "EAP_LITELLM_BASE_URL ... is not set" instead).
    events = event_rows(worker.engine, run_id)
    assert [(e[1], e[2]) for e in events] == [("start", "run_started"), ("end", "run_failed")]
    assert "litellm:qwen3.8-thinking" in events[1][3]["error"]
    assert "EAP_LITELLM_BASE_URL" not in run["error"]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 0


def test_resolver_refuses_disallowed_model(worker_settings):
    from eap_worker.models import make_resolver

    settings = replace(worker_settings, allowed_models=ModelPolicy.parse("test:ticket"))
    resolve = make_resolver(lambda: settings)
    assert resolve(None, "test:ticket") is not None
    with pytest.raises(ModelNotAllowed):
        resolve(None, "test:echo")
    with pytest.raises(ModelNotAllowed):
        resolve(None, "openai:gpt-4o")


def test_worker_settings_read_allowed_models_from_env(monkeypatch):
    from eap_worker.config import WorkerSettings

    monkeypatch.delenv("EAP_ALLOWED_MODELS", raising=False)
    assert WorkerSettings.from_env().allowed_models.entries == ("test:*",)
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,litellm:qwen3.8-thinking")
    policy = WorkerSettings.from_env().allowed_models
    assert policy.allows("litellm", "qwen3.8-thinking") and not policy.allows("litellm", "other")
