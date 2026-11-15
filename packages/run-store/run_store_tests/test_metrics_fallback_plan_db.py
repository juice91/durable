"""AC.17 review (2026-11-15): the summary's fallback block stays cheap for a large tenant.

Seed: one tenant with 40,000 agent runs and 160,000 chat requests spread over 80 days (plus failed
attempt rows and exhausted runs). The fallback queries must never read execution_events, and for a
one-hour window their plans must reach run_model_requests and runs through the tenant/created
indexes (and runs by primary key), never a sequential scan of either table.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from eap_run_store.metrics import FALLBACK_EXHAUSTED_RUN_ERROR, MetricsQuery, MetricsStore

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUNS = 40000
PER_RUN = 4


@pytest.fixture
def large_tenant(engine):
    tenant = f"test_tenant_{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at, agent_id, agent_version,"
                " model_id, model_overridden, input, error)"
                " SELECT gen_random_uuid()::text, :t, 'agent',"
                "  CASE WHEN g % 50 = 0 THEN 'failed' ELSE 'completed' END,"
                "  :base + (g * interval '173 seconds'), :base + (g * interval '173 seconds'),"
                "  gen_random_uuid()::text, 1, 'test:a', false, 'hi',"
                "  CASE WHEN g % 50 = 0 THEN :err ELSE NULL END"
                " FROM generate_series(1, :n) AS g"
            ),
            {"t": tenant, "base": BASE, "n": RUNS, "err": FALLBACK_EXHAUSTED_RUN_ERROR},
        )
        conn.execute(
            text(
                "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at, operation)"
                " SELECT r.tenant_id, r.id, 'model:' || k || CASE WHEN k = 3 AND r.created_at::text LIKE '%5:%' THEN ':failed:0' ELSE '' END,"
                "  CASE WHEN k = 1 THEN 'test:b' ELSE 'test:a' END, 10, 5, 0, 0, true, 0.001, 'override',"
                "  r.created_at + k * interval '1 second', 'chat'"
                " FROM runs r, generate_series(0, :per - 1) AS k WHERE r.tenant_id = :t"
            ),
            {"t": tenant, "per": PER_RUN},
        )
        conn.execute(text("ANALYZE runs"))
        conn.execute(text("ANALYZE run_model_requests"))
    yield tenant
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant})


def _capture(engine, fn):
    statements: list[tuple[str, object]] = []

    def listen(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", listen)
    try:
        result = fn()
    finally:
        event.remove(engine, "before_cursor_execute", listen)
    return result, statements


def test_fallback_never_reads_events_and_uses_indexes(engine, large_tenant):
    store = MetricsStore(engine)
    day = BASE + timedelta(days=40)
    # One hour of a tenant with 80 days of history: the requests come from their tenant/created
    # index and each run by its primary key, so the cost follows the window, not the history.
    q = MetricsQuery(tenant_id=large_tenant, start=day, end=day + timedelta(hours=1))

    def fallback_only():
        with store._session() as session:
            return store._fallback(session, q)

    result, statements = _capture(engine, fallback_only)
    assert result["chat_requests"] > 0 and result["fell_back"] > 0
    queries = [(s, p) for s, p in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(queries) == 2
    for statement, _params in queries:
        assert "execution_events" not in statement
    with engine.connect() as conn:
        for statement, params in queries:
            plan = "\n".join(row[0] for row in conn.exec_driver_sql("EXPLAIN " + statement, params))
            assert "execution_events" not in plan
            assert "Seq Scan on run_model_requests" not in plan, plan
            assert "Seq Scan on runs" not in plan, plan
            assert any(
                ix in plan for ix in ("ix_run_model_requests_tenant_created", "ix_runs_tenant_created", "ix_runs_tenant_kind_created")
            ), plan
    with engine.connect() as conn:
        requests_plan = "\n".join(row[0] for row in conn.exec_driver_sql("EXPLAIN " + queries[0][0], queries[0][1]))
    assert "ix_run_model_requests_tenant_created" in requests_plan and "runs_pkey" in requests_plan, requests_plan


def test_fallback_counts_on_the_large_tenant(engine, large_tenant):
    """Whole range: every 2nd-of-4 request fell back (k = 1 answered by test:b); 1 run in 50 exhausted."""
    store = MetricsStore(engine)
    q = MetricsQuery(tenant_id=large_tenant, start=BASE, end=BASE + timedelta(days=85))
    fb = store.summary(q)["current"]["fallback"]
    failed_rows = fb["billed_failed_attempts"]
    assert fb["chat_requests"] == RUNS * PER_RUN - failed_rows
    assert fb["fell_back"] == RUNS
    assert fb["exhausted_runs"] == RUNS // 50
    assert fb["fallback_rate"] == pytest.approx((RUNS + RUNS // 50) / (fb["chat_requests"] + RUNS // 50))
