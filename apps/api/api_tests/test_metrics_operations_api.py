"""RM.19: GET /metrics/* `operation` filter, `by=operation` breakdown and the `kb_ingest` kind.

Seed (tool runs, window [BASE, BASE + 1 day)): one run with a chat, an embedding and an observer
request; another tenant's embedding request that must never be counted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from .run_helpers import requires_pg
from .test_runs import (  # noqa: F401 - fixtures
    _no_hang,
    deltas,
    fake_dbos,
    pg,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

BASE = datetime(2026, 5, 1, tzinfo=timezone.utc)
WINDOW = {"from": BASE.isoformat(), "to": (BASE + timedelta(days=1)).isoformat()}


def _seed_run(conn, tenant, requests):
    run_id = str(uuid.uuid4())
    at = BASE + timedelta(hours=1)
    conn.execute(
        text(
            "INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at, finished_at, tool_id,"
            " model_overridden) VALUES (:id, :t, 'tool', 'completed', :c, :c, :c, 'echo', false)"
        ),
        {"id": run_id, "t": tenant, "c": at},
    )
    for key, op, inp, out, cost in requests:
        conn.execute(
            text(
                "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source,"
                " created_at, operation) VALUES (:t, :r, :k, 'test:m', :i, :o, 0, 0, true, :c, 'override', :at, :op)"
            ),
            {"t": tenant, "r": run_id, "k": key, "op": op, "i": inp, "o": out, "c": cost, "at": at},
        )


@pytest.fixture
def seeded(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    other, h_other = tenant_of()
    with app.state.database_engine.begin() as conn:
        _seed_run(conn, tenant, [
            ("model:0", "chat", 10, 5, Decimal("0.001")),
            ("embed:retrieve:c1", "embedding", 7, 0, Decimal("0.00007")),
            ("memory:observe:1", "chat", 40, 8, Decimal("0.004")),
        ])
        _seed_run(conn, other, [("embed:batch:0", "embedding", 999, 0, Decimal("1"))])
    return client, h, h_other


def test_operation_filter_on_summary_and_echo(seeded):
    client, h, _ = seeded
    r = client.get("/api/v1/metrics/summary", params={**WINDOW, "operation": "embedding"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filters"]["operation"] == "embedding"
    assert (body["current"]["requests"], body["current"]["input_tokens"], body["current"]["cost_usd"]) == (
        1, 7, "0.0000700000"
    )
    memory = client.get("/api/v1/metrics/summary", params={**WINDOW, "operation": "memory"}, headers=h).json()
    assert memory["current"]["requests"] == 1 and memory["current"]["input_tokens"] == 40
    plain = client.get("/api/v1/metrics/summary", params={**WINDOW, "compare": "previous"}, headers=h).json()
    assert plain["filters"]["operation"] is None and plain["current"]["requests"] == 3
    ops = plain["current"]["operations"]
    assert ops["embedding"]["cost_usd"] == "0.0000700000" and ops["observer"]["input_tokens"] == 40
    assert ops["memory"]["requests"] == 1 and ops["moderation"]["requests"] == 0
    assert plain["previous"]["operations"]["chat"]["requests"] == 0


def test_breakdown_by_operation(seeded):
    client, h, h_other = seeded
    r = client.get("/api/v1/metrics/breakdown", params={**WINDOW, "by": "operation", "sort": "tokens"}, headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [(i["key"], i["label"], i["input_tokens"]) for i in items] == [
        ("observer", "observer", 40), ("chat", "chat", 10), ("embedding", "embedding", 7),
    ]
    other = client.get("/api/v1/metrics/breakdown", params={**WINDOW, "by": "operation"}, headers=h_other).json()
    assert [(i["key"], i["input_tokens"]) for i in other["items"]] == [("embedding", 999)]


def test_timeseries_with_operation(seeded):
    client, h, _ = seeded
    r = client.get(
        "/api/v1/metrics/timeseries", params={**WINDOW, "operation": "observer", "interval": "hour"}, headers=h
    )
    assert r.status_code == 200, r.text
    assert sum(b["requests"] for b in r.json()["buckets"]) == 1


@pytest.mark.parametrize("params", [{"operation": "nope"}, {"by": "tool"}])
def test_bad_values_are_422(seeded, params):
    client, h, _ = seeded
    path = "/api/v1/metrics/breakdown" if "by" in params else "/api/v1/metrics/summary"
    r = client.get(path, params={**WINDOW, **params}, headers=h)
    assert r.status_code == 422


def test_kind_kb_ingest_is_accepted(seeded):
    client, h, _ = seeded
    r = client.get("/api/v1/metrics/summary", params={**WINDOW, "kind": "kb_ingest"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["current"]["runs"] == 0
