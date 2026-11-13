"""P0.1: GET /runs/{id} and GET /runs return usage, cost and run timestamps."""

from __future__ import annotations

import uuid
from decimal import Decimal

from .run_helpers import requires_pg
from .test_runs import (  # noqa: F401 - fixtures
    _no_hang,
    deltas,
    fake_dbos,
    make_agent,
    pg,
    start,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

USAGE_FIELDS = {"model_id", "model_overridden", "input_tokens", "output_tokens", "cost_usd", "started_at", "finished_at"}


def test_run_json_usage_fields(pg, tenant_of):
    from eap_run_store import EventLog, RunStore

    client, app = pg
    tenant, h = tenant_of()
    agent_id = make_agent(client, h)
    run_id = str(uuid.uuid4())
    assert start(client, h, agent_id, run_id).status_code == 201

    # Queued, not recorded yet: nulls, except the effective model the API records at insert (P0.8).
    body = client.get(f"/api/v1/runs/{run_id}", headers=h).json()
    assert USAGE_FIELDS <= set(body)
    assert {k: body[k] for k in USAGE_FIELDS} == {
        "model_id": "test:ticket", "model_overridden": False, "input_tokens": None, "output_tokens": None,
        "cost_usd": None, "started_at": None, "finished_at": None,
    }

    engine = app.state.database_engine
    store, log = RunStore(engine), EventLog(engine)
    store.mark_running(tenant, uuid.UUID(run_id))
    log.append(tenant, uuid.UUID(run_id), "model:0:final", "model_message", {}, {
        "request_key": "model:0", "model_id": "test:ticket", "provider_model_name": "scripted-ticket",
        "input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "usage_measured": True, "cost_usd": Decimal("0.0000000001"), "price_source": "override",
    })
    store.set_status(tenant, uuid.UUID(run_id), "completed", output="done")

    body = client.get(f"/api/v1/runs/{run_id}", headers=h).json()
    assert (body["input_tokens"], body["output_tokens"], body["cost_usd"]) == (1, 2, "0.0000000001")
    assert body["started_at"] and body["finished_at"] and body["started_at"] <= body["finished_at"]
    listed = client.get("/api/v1/runs", headers=h).json()["items"]
    assert [i for i in listed if i["run_id"] == run_id][0] == body
