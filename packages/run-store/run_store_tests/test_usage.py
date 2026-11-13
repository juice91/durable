"""P0.1: run_model_requests rows written with their event, run totals and timestamps."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(
            text("DELETE FROM execution_events WHERE run_id IN (SELECT id FROM runs WHERE tenant_id = :t)"),
            {"t": tenant_id},
        )
        conn.execute(text("UPDATE runs SET parent_run_id = NULL, parent_step_id = NULL WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})


def _usage(key: str, inp: int, out: int, cost: str | None, model: str = "test:ticket") -> dict:
    return {
        "request_key": key, "model_id": model, "provider_model_name": "scripted", "input_tokens": inp,
        "output_tokens": out, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None if cost is None else Decimal(cost), "price_source": None if cost is None else "override",
    }


def _rows(engine, run_id) -> list:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT request_key, input_tokens, cost_usd FROM run_model_requests WHERE run_id = :r ORDER BY request_key"),
            {"r": str(run_id)},
        ).all()


def test_append_writes_usage_row_once(run_store, event_log, engine, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    ev, created = event_log.append(tenant, run_id, "model:0:final", "model_message", {"n": 0}, _usage("model:0", 10, 5, "0.001"))
    assert created and ev.seq == 0
    # A repeated key (re-executed step) with different usage: no new seq, the first row stands.
    ev2, created2 = event_log.append(tenant, run_id, "model:0:final", "model_message", {"n": 0}, _usage("model:0", 99, 99, "9"))
    assert not created2 and ev2.seq == 0
    ev3, _ = event_log.append(tenant, run_id, "end", "run_completed", {})
    assert ev3.seq == 1
    assert [tuple(r) for r in _rows(engine, run_id)] == [("model:0", 10, Decimal("0.0010000000"))]


def test_totals_and_timestamps(run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    assert run_store.get_run(tenant, run_id)["started_at"] is None
    run_store.mark_running(tenant, run_id)
    started = run_store.get_run(tenant, run_id)["started_at"]
    assert started is not None
    run_store.mark_running(tenant, run_id)  # recovery: started_at kept
    assert run_store.get_run(tenant, run_id)["started_at"] == started

    event_log.append(tenant, run_id, "model:0:final", "model_message", {}, _usage("model:0", 10, 5, "0.0010000001"))
    event_log.append(tenant, run_id, "model:1:final", "model_message", {}, _usage("model:1", 20, 7, "0.002"))
    run_store.set_status(tenant, run_id, "completed", output="ok")
    run = run_store.get_run(tenant, run_id)
    assert (run["input_tokens"], run["output_tokens"], run["cost_usd"]) == (30, 12, Decimal("0.0030000001"))
    finished = run["finished_at"]
    assert finished is not None and finished >= started
    # A re-executed finish step: same totals, same finished_at.
    run_store.set_status(tenant, run_id, "completed", output="ok")
    again = run_store.get_run(tenant, run_id)
    assert again["finished_at"] == finished and again["cost_usd"] == Decimal("0.0030000001")


def test_unpriced_request_makes_cost_null_and_failed_runs_get_totals(run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    event_log.append(tenant, run_id, "model:0:final", "model_message", {}, _usage("model:0", 10, 5, "0.001"))
    event_log.append(tenant, run_id, "model:1:final", "model_message", {}, _usage("model:1", 3, 4, None))
    run_store.set_status(tenant, run_id, "needs_attention", error="x")
    run = run_store.get_run(tenant, run_id)
    assert (run["input_tokens"], run["output_tokens"], run["cost_usd"]) == (13, 9, None)
    assert run["finished_at"] is not None


def test_run_without_requests_and_non_terminal_status(run_store, tenant):
    run_id = uuid4()
    run_store.create_tool_run(tenant, "clock.now", "{}", run_id)
    run_store.set_status(tenant, run_id, "running")
    run = run_store.get_run(tenant, run_id)
    assert run["finished_at"] is None and run["input_tokens"] is None and run["cost_usd"] is None
    run_store.set_status(tenant, run_id, "failed", error="x")
    run = run_store.get_run(tenant, run_id)
    assert (run["input_tokens"], run["output_tokens"], run["cost_usd"]) == (0, 0, Decimal(0))
    assert run["model_id"] is None and run["model_overridden"] is False


def test_workflow_totals_sum_child_runs(run_store, event_log, tenant):
    wf = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", wf)
    c1, c2 = uuid4(), uuid4()
    run_store.create_child_agent_run(tenant, uuid4(), 1, "a", c1, wf, "s1")
    run_store.create_child_agent_run(tenant, uuid4(), 1, "b", c2, wf, "s2")
    event_log.append(tenant, c1, "model:0:final", "model_message", {}, _usage("model:0", 10, 1, "0.5"))
    event_log.append(tenant, c2, "model:0:final", "model_message", {}, _usage("model:0", 20, 2, "0.25"))
    # Another top-level run of the same tenant is not counted.
    other = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "x", other)
    event_log.append(tenant, other, "model:0:final", "model_message", {}, _usage("model:0", 1000, 1000, "100"))
    run_store.set_status(tenant, c1, "completed")
    run_store.set_status(tenant, c2, "completed")
    run_store.set_status(tenant, wf, "completed", output="{}")
    run = run_store.get_run(tenant, wf)
    assert (run["input_tokens"], run["output_tokens"], run["cost_usd"]) == (30, 3, Decimal("0.75"))


def test_usage_row_cannot_reference_another_tenants_run(run_store, engine, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    with pytest.raises(IntegrityError, match="fk_run_model_requests_tenant_run"):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, usage_measured, created_at)"
                " VALUES ('someone_else', :r, 'model:0', 'test:x', 1, 1, 0, 0, true, now())"
            ), {"r": str(run_id)})


def test_enqueue_failed_is_finished_with_zero_totals_until_revived(run_store, tenant):
    from eap_run_store import ENQUEUE_FAILED_ERROR

    keys = ("finished_at", "input_tokens", "output_tokens", "cost_usd")
    # Revived by the worker (DBOS accepted the enqueue although it raised): mark_running clears it.
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    assert run_store.mark_enqueue_failed(tenant, run_id)
    run = run_store.get_run(tenant, run_id)
    assert run["status"] == "failed" and run["error"] == ENQUEUE_FAILED_ERROR
    assert run["finished_at"] is not None and run["finished_at"] >= run["created_at"]
    assert (run["input_tokens"], run["output_tokens"], run["cost_usd"]) == (0, 0, Decimal(0))
    assert run_store.mark_running(tenant, run_id)
    run = run_store.get_run(tenant, run_id)
    assert run["status"] == "running" and run["error"] is None and run["started_at"] is not None
    assert {k: run[k] for k in keys} == dict.fromkeys(keys)

    # Revived by a client retry: requeue clears it too.
    retried = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", retried)
    run_store.mark_enqueue_failed(tenant, retried)
    assert run_store.requeue_after_enqueue_failure(tenant, retried)
    run = run_store.get_run(tenant, retried)
    assert run["status"] == "queued" and {k: run[k] for k in keys} == dict.fromkeys(keys)

    # A run that already started is not overwritten, and mark_running keeps another error's totals.
    started = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", started)
    run_store.mark_running(tenant, started)
    assert not run_store.mark_enqueue_failed(tenant, started)
    assert run_store.get_run(tenant, started)["finished_at"] is None
    run_store.set_status(tenant, started, "failed", error="other")
    finished = run_store.get_run(tenant, started)["finished_at"]
    run_store.mark_running(tenant, started)
    assert run_store.get_run(tenant, started)["finished_at"] == finished


def test_superseded_turn_stays_finished(run_store, tenant, engine):
    conv, _ = run_store.create_conversation(tenant, uuid4())
    first = uuid4()
    run_store.create_turn(tenant, conv["id"], 1, "one", first)
    run_store.mark_enqueue_failed(tenant, first)
    run_store.create_turn(tenant, conv["id"], 1, "two", uuid4())
    assert run_store.mark_running(tenant, first) is False
    run = run_store.get_run(tenant, first)
    assert run["status"] == "failed" and run["finished_at"] is not None and run["input_tokens"] == 0
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant})
