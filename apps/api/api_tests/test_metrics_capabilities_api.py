"""AC.17: GET /metrics/* accepts `operation=judge` and `kind=eval`, and the summary has `fallback`.

Seed: one tool run with a chat request and a judge-keyed request (window [BASE, BASE + 1 day)).
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


@pytest.fixture
def seeded(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    at = BASE + timedelta(hours=1)
    run_id = str(uuid.uuid4())
    with app.state.database_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at, finished_at, tool_id,"
                " model_overridden) VALUES (:id, :t, 'tool', 'completed', :c, :c, :c, 'echo', false)"
            ),
            {"id": run_id, "t": tenant, "c": at},
        )
        for key, inp in (("model:0", 10), ("judge:k1:0", 30)):
            conn.execute(
                text(
                    "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens,"
                    " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source,"
                    " created_at, operation) VALUES (:t, :r, :k, 'test:m', :i, 0, 0, 0, true, :c, 'override', :at, 'chat')"
                ),
                {"t": tenant, "r": run_id, "k": key, "i": inp, "c": Decimal("0.001"), "at": at},
            )
    return client, h


def test_judge_operation_and_fallback_block(seeded):
    client, h = seeded
    body = client.get("/api/v1/metrics/summary", params=WINDOW, headers=h).json()
    ops = body["current"]["operations"]
    assert ops["judge"]["requests"] == 1 and ops["judge"]["input_tokens"] == 30
    assert ops["chat"]["requests"] == 1 and ops["chat"]["input_tokens"] == 10
    assert body["current"]["fallback"] == {
        "chat_requests": 0, "fell_back": 0, "billed_failed_attempts": 0, "exhausted_runs": 0, "fallback_rate": None,
    }
    judge = client.get("/api/v1/metrics/summary", params={**WINDOW, "operation": "judge"}, headers=h)
    assert judge.status_code == 200, judge.text
    assert judge.json()["current"]["requests"] == 1
    items = client.get("/api/v1/metrics/breakdown", params={**WINDOW, "by": "operation"}, headers=h).json()["items"]
    assert {i["key"] for i in items} == {"chat", "judge"}


def test_kind_eval_is_accepted(seeded):
    client, h = seeded
    r = client.get("/api/v1/metrics/summary", params={**WINDOW, "kind": "eval"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["current"]["runs"] == 0
