"""RM.19: GET /runs/{id}/trace shows a retrieve span with the agent version's knowledge base refs
(top_k), its query embedding nested under it, and the retrieve hits; other tenants get 404."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import text

from .run_helpers import requires_pg
from .test_runs import (  # noqa: F401 - fixtures
    SPEC,
    _no_hang,
    deltas,
    fake_dbos,
    pg,
    start,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

API = "/api/v1"


def test_retrieve_trace_has_top_k_hits_and_embedding(pg, tenant_of):
    from eap_run_store import EventLog

    client, app = pg
    tenant, h = tenant_of()
    _, h_other = tenant_of()
    try:
        _check(client, app, tenant, h, h_other, EventLog)
    finally:  # the shared tenant cleanup does not remove knowledge bases
        with app.state.database_engine.begin() as conn:
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})


def _check(client, app, tenant, h, h_other, EventLog) -> None:
    r = client.post(f"{API}/knowledge-bases", json={"name": "Docs", "embedding_model": "test:hash"}, headers=h)
    assert r.status_code == 201, r.text
    kb_id = r.json()["id"]
    r = client.post(f"{API}/agents", json={"spec": {**SPEC, "knowledge_bases": [{"id": kb_id, "top_k": 3}]}}, headers=h)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=h).status_code == 201
    run_id = str(uuid.uuid4())
    assert start(client, h, agent_id, run_id).status_code == 201

    source = {"n": 1, "title": "Install", "url": "https://docs.example/install", "ref": "https://docs.example/install",
              "kb_id": kb_id, "document_id": "d1", "document_key": "install", "chunk_id": "ch1", "doc_version": 1,
              "score": 0.88}
    log = EventLog(app.state.database_engine)
    rid = uuid.UUID(run_id)
    log.append(tenant, rid, "start", "run_started", {"input": "how do I install?"})
    log.append(tenant, rid, "tool:c1:call", "tool_call",
               {"tool_call_id": "c1", "tool_name": "retrieve", "args": {"search_query": "install"}})
    log.append(tenant, rid, "embed:retrieve:c1", "embedding",
               {"model_id": "test:hash", "inputs": 1, "input_tokens": 2, "cost_usd": "0.00002", "dimensions": 1536},
               {"request_key": "embed:retrieve:c1", "model_id": "test:hash", "provider_model_name": None,
                "input_tokens": 2, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "usage_measured": True, "cost_usd": Decimal("0.00002"), "price_source": "override",
                "operation": "embedding"})
    log.append(tenant, rid, "tool:c1:result", "tool_result",
               {"tool_call_id": "c1", "tool_name": "retrieve", "status": "ok", "content": "[1] Install",
                "sources": [source]})

    resp = client.get(f"{API}/runs/{run_id}/trace", headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    spans = {s["id"]: s for s in body["spans"]}
    retrieve = spans[f"{run_id}:tool:c1"]
    assert retrieve["kind"] == "retrieve"
    assert retrieve["attributes"]["top_k"] == 3
    assert retrieve["attributes"]["knowledge_bases"] == [{"id": kb_id, "top_k": 3, "min_score": None}]
    assert retrieve["attributes"]["hits"] == [source]
    embedding = spans[f"{run_id}:embed:retrieve:c1"]
    assert embedding["kind"] == "embedding" and embedding["parent_id"] == retrieve["id"]
    assert embedding["attributes"]["usage"]["input_tokens"] == 2
    assert body["totals"]["embedding_requests"] == 1

    assert client.get(f"{API}/runs/{run_id}/trace", headers=h_other).status_code == 404
