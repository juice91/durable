"""P0.1 acceptance: crash recovery gives one usage row, unpriced models, workflow totals, tenant FK."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from dbos import DBOSClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eap_worker import pricing
from worker_tests.conftest import (
    DBOS_DB_URL, APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal,
)
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_VERSION, WorkerProcess, _env, crash_schema  # noqa: F401
from worker_tests.workflow_run_helpers import (
    agent_spec, cleanup_definitions, enqueue_workflow_run, publish_agent, publish_workflow,
)



def _usage_rows(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT request_key, input_tokens, output_tokens, cost_usd, price_source, usage_measured"
            " FROM run_model_requests WHERE run_id = :r ORDER BY request_key"), {"r": str(run_id)}).mappings().all()


def _cost(row) -> Decimal:
    return (Decimal(row["input_tokens"]) * 2 + Decimal(row["output_tokens"]) * 8) / Decimal(10**6)


# ----------------------------------------------------------------------------- crash recovery


def test_crash_after_model_final_event_leaves_exactly_one_usage_row(worker, tenant, crash_schema, tmp_path):
    """Kill the worker right after model:0:final committed (before the DBOS checkpoint); recovery must
    replay the append: one row per request, no doubled cost, no seq gap."""
    executor_id = f"usage-crash-{uuid.uuid4().hex[:8]}"
    marker = str(tmp_path / "marker")
    first = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_AT="after_event_write",
                               EAP_CRASH_EVENT_KEY="model:0:final", EAP_CRASH_MARKER=marker), tmp_path / "w1.log")
    second = None
    client = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=ticket_spec(),
                             input_text="Laptop will not boot", app_version=CRASH_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        # The event and its usage row are already durable; the run is not finished.
        assert [r["request_key"] for r in _usage_rows(worker.engine, run_id)] == ["model:0"]
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        before = _usage_rows(worker.engine, run_id)[0]

        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        assert "Recovering" in (tmp_path / "w2.log").read_text()

        rows = _usage_rows(worker.engine, run_id)
        assert [r["request_key"] for r in rows] == ["model:0", "model:1"]
        assert dict(rows[0]) == dict(before)  # the first write stands, untouched
        for r in rows:
            assert r["cost_usd"] == _cost(r) and r["price_source"] == "override" and r["usage_measured"]
        assert run["input_tokens"] == sum(r["input_tokens"] for r in rows)
        assert run["output_tokens"] == sum(r["output_tokens"] for r in rows)
        assert run["cost_usd"] == sum(r["cost_usd"] for r in rows)  # no double cost
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))
        keys = [e[1] for e in events]
        assert len(keys) == len(set(keys)) and keys.count("model:0:final") == 1
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if client is not None:
            client.destroy()


# ----------------------------------------------------------------------------- unpriced model


def test_unpriced_model_run_has_null_cost_but_counted_tokens(worker, client, tenant, tmp_path, monkeypatch):
    """No price for the model: cost null per request, in the events and for the run total; tokens kept."""
    other = tmp_path / "prices.json"
    other.write_text(json.dumps({"models": {"openai:*": [
        {"effective_from": "2026-01-01", "input_per_mtok": "1", "output_per_mtok": "1"}]}}))
    monkeypatch.setenv(pricing.PRICES_FILE_ENV, str(other))
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=ticket_spec(),
                         input_text="Unpriced please", app_version=APP_VERSION)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    rows = _usage_rows(worker.engine, run_id)
    assert len(rows) == 2
    for r in rows:
        assert r["cost_usd"] is None and r["price_source"] is None
        assert r["usage_measured"] is True and r["input_tokens"] > 0 and r["output_tokens"] > 0
    assert run["cost_usd"] is None
    assert run["input_tokens"] == sum(r["input_tokens"] for r in rows) > 0
    for _, key, etype, data in event_rows(worker.engine, run_id):
        if etype == "model_message":
            assert data["cost_usd"] is None and data["usage"]["input_tokens"] > 0
            assert data["usage"]["price_source"] is None


# ----------------------------------------------------------------------------- workflow totals


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def test_workflow_totals_are_the_children_sum_and_not_double_counted(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    sibling = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=ticket_spec(),
                          input_text="sibling", app_version=APP_VERSION)
    assert wait_terminal(worker.run_store, tenant, sibling)["status"] == "completed"
    spec = {"name": "Two agents", "input_schema": {"type": "object"}, "start": "a", "steps": [
        {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"p": "one"}, "next": "b"},
        {"id": "b", "type": "agent", "agent_id": agent_id, "input": {"p": "two"}}]}
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                  version_id=version_id, input_value={}, app_version=APP_VERSION)
    wf = wait_terminal(worker.run_store, tenant, run_id)
    assert wf["status"] == "completed", wf["error"]
    children = [r["id"] for r in worker.run_store.list_runs(tenant, parent_run_id=run_id)]
    assert len(children) == 2
    assert _usage_rows(worker.engine, run_id) == []  # the workflow run has no request rows of its own
    child_rows = [r for c in children for r in _usage_rows(worker.engine, c)]
    assert len(child_rows) >= 2 and all(r["cost_usd"] is not None for r in child_rows)
    assert wf["input_tokens"] == sum(r["input_tokens"] for r in child_rows)
    assert wf["output_tokens"] == sum(r["output_tokens"] for r in child_rows)
    assert wf["cost_usd"] == sum(r["cost_usd"] for r in child_rows)
    sib = worker.run_store.get_run(tenant, sibling)  # the sibling's usage is not in the workflow total
    assert sib["cost_usd"] is not None and wf["cost_usd"] != wf["cost_usd"] + sib["cost_usd"]
    for c in children:
        child = worker.run_store.get_run(tenant, uuid.UUID(c))
        assert child["cost_usd"] == sum(r["cost_usd"] for r in _usage_rows(worker.engine, c))


# ----------------------------------------------------------------------------- tenant FK


def test_usage_row_cannot_reference_another_tenants_run(worker, tenant):
    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, uuid.uuid4(), 1, "hi", run_id)
    insert = text(
        "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at)"
        " VALUES (:t, :r, 'model:0', 'test:x', 1, 1, 0, 0, true, 0.001, 'override', now())")
    with pytest.raises(IntegrityError, match="foreign key"):
        with worker.engine.begin() as conn:
            conn.execute(insert, {"t": f"{tenant}_other", "r": str(run_id)})
    with worker.engine.begin() as conn:  # same-tenant insert works: the FK is the only thing in the way
        conn.execute(insert, {"t": tenant, "r": str(run_id)})
