"""RM.19: the metrics `operation` dimension (filter on summary / time series, breakdown by operation).

Seeded with tool runs (no foreign rows needed) whose requests cover every operation:

  t1  completed  requests model:0 (chat, 10/5, $0.001), moderation:input:0 (chat, 3/0, $0.0003),
                 embed:retrieve:c1 (embedding, 7/0, $0.00007)
  t2  completed  requests memory:observe:1 (chat, 40/8, $0.004), memory:reflect:1 (chat, 20/4, unpriced),
                 embed:recall:1 (embedding, 5/0, $0.00005), embed:index:1 (embedding, 9/0, $0.00009)
  t3  failed     request  embed:batch:0 (embedding, 100/0, $0.001)
  other tenant: one embedding request that must never show up.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from eap_run_store.metrics import OPERATIONS, MetricsQuery, MetricsRangeError, MetricsStore

BASE = datetime(2026, 4, 1, tzinfo=timezone.utc)


def _run(conn, tenant, status, created):
    run_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at, finished_at, tool_id,"
            " model_overridden) VALUES (:id, :t, 'tool', :s, :c, :c, :f, 'echo', false)"
        ),
        {"id": run_id, "t": tenant, "s": status, "c": created, "f": created + timedelta(seconds=1)},
    )
    return run_id


def _req(conn, tenant, run_id, key, operation, inp, out, cost, created):
    conn.execute(
        text(
            "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at, operation)"
            " VALUES (:t, :r, :k, 'test:m', :i, :o, 0, 0, true, :c, :src, :created, :op)"
        ),
        {"t": tenant, "r": run_id, "k": key, "i": inp, "o": out, "c": cost, "op": operation,
         "src": None if cost is None else "override", "created": created},
    )


@pytest.fixture
def seeded(engine):
    tenant = f"test_tenant_{uuid.uuid4().hex[:8]}"
    other = f"test_tenant_{uuid.uuid4().hex[:8]}"
    at = BASE + timedelta(hours=1)
    with engine.begin() as conn:
        t1 = _run(conn, tenant, "completed", at)
        _req(conn, tenant, t1, "model:0", "chat", 10, 5, Decimal("0.001"), at)
        _req(conn, tenant, t1, "moderation:input:0", "chat", 3, 0, Decimal("0.0003"), at)
        _req(conn, tenant, t1, "embed:retrieve:c1", "embedding", 7, 0, Decimal("0.00007"), at)
        t2 = _run(conn, tenant, "completed", at)
        _req(conn, tenant, t2, "memory:observe:1", "chat", 40, 8, Decimal("0.004"), at)
        _req(conn, tenant, t2, "memory:reflect:1", "chat", 20, 4, None, at)
        _req(conn, tenant, t2, "embed:recall:1", "embedding", 5, 0, Decimal("0.00005"), at)
        _req(conn, tenant, t2, "embed:index:1", "embedding", 9, 0, Decimal("0.00009"), at)
        t3 = _run(conn, tenant, "failed", at)
        _req(conn, tenant, t3, "embed:batch:0", "embedding", 100, 0, Decimal("0.001"), at)
        o1 = _run(conn, other, "completed", at)
        _req(conn, other, o1, "embed:batch:0", "embedding", 999, 0, Decimal("1"), at)
    yield tenant, {"t1": t1, "t2": t2, "t3": t3}
    with engine.begin() as conn:
        for t in (tenant, other):
            conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})


def _q(tenant, **kw):
    return MetricsQuery(tenant_id=tenant, start=BASE, end=BASE + timedelta(days=1), **kw)


def test_operation_filter_values_are_validated():
    with pytest.raises(MetricsRangeError):
        _q("t", operation="nope")
    assert _q("t", operation="memory").operation == "memory"


@pytest.mark.parametrize(
    ("operation", "requests", "input_tokens", "cost", "runs"),
    [
        (None, 8, 194, "0.0065100000", 3),
        ("chat", 1, 10, "0.0010000000", 1),
        ("moderation", 1, 3, "0.0003000000", 1),
        ("observer", 1, 40, "0.0040000000", 1),
        ("reflector", 1, 20, "0.0000000000", 1),
        ("embedding", 4, 121, "0.0012100000", 3),
        ("memory", 4, 74, "0.0041400000", 1),
    ],
)
def test_summary_by_operation(engine, seeded, operation, requests, input_tokens, cost, runs):
    tenant, _ = seeded
    cur = MetricsStore(engine).summary(_q(tenant, operation=operation))["current"]
    assert cur["requests"] == requests
    assert cur["input_tokens"] == input_tokens
    assert format(cur["cost_usd"], "f") == cost
    assert cur["runs"] == runs


def test_breakdown_by_operation_partitions_the_totals(engine, seeded):
    tenant, _ = seeded
    store = MetricsStore(engine)
    items = store.breakdown(_q(tenant), "operation", sort="requests", limit=10)
    by_key = {i["key"]: i for i in items}
    # AC.17: OPERATIONS gained `judge`; this seed has no judge request, so no judge group.
    assert set(by_key) == set(OPERATIONS) - {"judge"}
    total = store.summary(_q(tenant))["current"]
    assert sum(i["requests"] for i in items) == total["requests"]
    assert sum(i["input_tokens"] for i in items) == total["input_tokens"]
    assert sum(i["output_tokens"] for i in items) == total["output_tokens"]
    assert sum(i["cost_usd"] for i in items) == total["cost_usd"]
    assert by_key["embedding"]["requests"] == 4
    assert by_key["embedding"]["runs"] == 3 and by_key["embedding"]["failed"] == 1
    assert by_key["reflector"]["unpriced_requests"] == 1
    assert items[0]["key"] == "embedding"  # sorted by requests


def test_breakdown_by_operation_with_filter(engine, seeded):
    tenant, _ = seeded
    items = MetricsStore(engine).breakdown(_q(tenant, operation="memory"), "operation", sort="cost", limit=10)
    assert {i["key"]: i["requests"] for i in items} == {"observer": 1, "reflector": 1, "embedding": 2}


def test_timeseries_with_operation(engine, seeded):
    tenant, _ = seeded
    buckets = MetricsStore(engine).timeseries(_q(tenant, operation="embedding"), "hour")
    assert sum(b["requests"] for b in buckets) == 4
    assert sum(b["input_tokens"] for b in buckets) == 121
    assert sum(b["runs"] for b in buckets) == 3


def test_other_tenant_never_counted(engine, seeded):
    tenant, _ = seeded
    items = MetricsStore(engine).breakdown(_q(tenant, operation="embedding"), "operation", limit=10)
    assert [i["input_tokens"] for i in items] == [121]


def test_summary_operations_split_sums_to_totals(engine, seeded):
    tenant, _ = seeded
    result = MetricsStore(engine).summary(_q(tenant), compare_previous=True)
    cur = result["current"]
    ops = cur["operations"]
    assert set(ops) == {*OPERATIONS, "memory"}
    for metric in ("requests", "input_tokens", "output_tokens", "cost_usd", "unpriced_requests"):
        assert sum(ops[op][metric] for op in OPERATIONS) == cur[metric]
    assert ops["embedding"]["input_tokens"] == 121 and ops["memory"]["input_tokens"] == 74
    assert ops["reflector"]["unpriced_requests"] == 1
    # The previous period is zero-filled with the same keys.
    assert result["previous"]["operations"]["embedding"]["requests"] == 0
    # Under an operation filter the other operations are zero.
    filtered = MetricsStore(engine).summary(_q(tenant, operation="chat"))["current"]["operations"]
    assert filtered["chat"]["requests"] == 1 and filtered["embedding"]["requests"] == 0
