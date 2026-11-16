"""Retrieval runtime, API side (RM.11): publish checks and the knowledge base search tester.

Publish refuses reserved platform tool names, hidden platform tools and knowledge base refs that do
not exist in the principal's tenant. The search tester records a tool run of the hidden
`knowledge.search` tool and enqueues it to a fake DBOSClient (no worker here), then long-polls.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from .run_helpers import requires_pg
from .test_runs import fake_dbos, pg, tenant_of, stream_settings, deltas  # noqa: F401 - fixtures

pytestmark = requires_pg

API = "/api/v1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")


@pytest.fixture
def kb_cleanup(pg):
    yield
    _client, app = pg
    with app.state.database_engine.begin() as conn:
        for t in app.state.test_tenants:
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})


def _kb(client, headers, name="Docs"):
    r = client.post(f"{API}/knowledge-bases", json={"name": name, "embedding_model": "test:hash"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _spec(**extra):
    return {
        "spec_version": 1,
        "name": "RAG agent",
        "instructions": "",
        "model": {"provider": "test", "name": "retrieve"},
        "tools": [],
        "model_settings": {},
        **extra,
    }


def _publish(client, headers, spec):
    r = client.post(f"{API}/agents", json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    return client.post(f"{API}/agents/{r.json()['id']}/versions", headers=headers)


# ----------------------------------------------------------------------------- publish


def test_publish_refuses_reserved_names_and_hidden_tools(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    # `retrieve` as a built-in tool id is the model-facing name `retrieve`: reserved for every spec
    # (AgentSpec alone only checks specs that use knowledge or memory).
    r = _publish(client, h, _spec(tools=["retrieve"]))
    assert r.status_code == 422 and r.json()["detail"] == "tool name 'retrieve' is reserved for a platform tool"
    r = _publish(client, h, _spec(tools=["update_working_memory"]))
    assert r.status_code == 422 and "update_working_memory" in r.json()["detail"]
    r = _publish(client, h, _spec(tools=["knowledge.search"]))
    assert r.status_code == 422
    assert r.json()["detail"] == "tool 'knowledge.search' is a platform tool and cannot be added to an agent"


def test_publish_checks_knowledge_bases_exist_in_the_tenant(pg, tenant_of, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    _, other = tenant_of()
    mine = _kb(client, h)
    foreign = _kb(client, other)
    unknown = str(uuid.uuid4())
    for kb_id in (unknown, foreign["id"]):
        r = _publish(client, h, _spec(knowledge_bases=[{"id": kb_id}]))
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == f"knowledge base '{kb_id}' does not exist in this tenant"
    r = _publish(client, h, _spec(knowledge_bases=[{"id": mine["id"], "top_k": 3, "filter": {"lang": "en"}}]))
    assert r.status_code == 201, r.text
    assert r.json()["spec"]["knowledge_bases"] == [{"id": mine["id"], "top_k": 3, "filter": {"lang": "en"}}]
    # A spec without knowledge bases still has no `knowledge_bases` key (hash stability).
    r = _publish(client, h, _spec())
    assert r.status_code == 201 and "knowledge_bases" not in r.json()["spec"]


# ----------------------------------------------------------------------------- search tester


def test_search_validates_and_scopes_by_tenant(pg, tenant_of, kb_cleanup, fake_dbos):
    client, _ = pg
    _, h = tenant_of()
    _, other = tenant_of()
    kb = _kb(client, h)
    url = f"{API}/knowledge-bases/{kb['id']}/search?wait=0"
    assert client.post(url, json={"query": "x"}, headers=other).status_code == 404
    assert client.post(f"{API}/knowledge-bases/{uuid.uuid4()}/search", json={"query": "x"}, headers=h).status_code == 404
    for body in ({"query": ""}, {"query": "   "}, {"query": "x", "top_k": 0}, {"query": "x", "top_k": 21},
                 {"query": "x", "min_score": 1.5}, {"query": "x", "filter": {"bad key!": 1}}, {"query": "x", "extra": 1},
                 {"query": "secret-value", "top_k": "many"}):
        r = client.post(url, json=body, headers=h)
        assert r.status_code == 422, body
        assert "secret-value" not in r.text  # rejected values are never echoed
    assert fake_dbos.calls == []


def test_search_records_a_tool_run_and_enqueues_it(pg, tenant_of, kb_cleanup, fake_dbos):
    client, app = pg
    tenant, h = tenant_of()
    kb = _kb(client, h)
    run_id = str(uuid.uuid4())
    body = {"query": "printer floor", "top_k": 3, "min_score": 0.1, "filter": {"lang": ["en", "de"]}, "run_id": run_id}
    r = client.post(f"{API}/knowledge-bases/{kb['id']}/search?wait=0", json=body, headers=h)
    assert r.status_code == 202, r.text
    assert r.json() == {
        "run_id": run_id, "kind": "tool", "tool_id": "knowledge.search", "knowledge_base_id": kb["id"],
        "status": "queued", "display_status": "queued", "trace_id": None,
    }
    expected_input = {"filter": {"lang": ["en", "de"]}, "knowledge_base_id": kb["id"], "min_score": 0.1,
                      "query": "printer floor", "top_k": 3}
    [(options, args)] = fake_dbos.calls
    assert options["workflow_name"] == "eap.tool_run" and options["workflow_id"] == run_id
    assert args[:3] == (run_id, tenant, "knowledge.search") and json.loads(args[3]) == expected_input
    row = app.state.run_stores[0].get_run(tenant, uuid.UUID(run_id))
    assert row["kind"] == "tool" and row["tool_id"] == "knowledge.search" and str(row["knowledge_base_id"]) == kb["id"]

    # Same run_id and body: the same run, enqueued again (idempotent in DBOS). Another body: 409.
    assert client.post(f"{API}/knowledge-bases/{kb['id']}/search?wait=0", json=body, headers=h).status_code == 202
    assert len(fake_dbos.calls) == 2
    r = client.post(f"{API}/knowledge-bases/{kb['id']}/search?wait=0", json={**body, "query": "other"}, headers=h)
    assert r.status_code == 409

    # The worker finishing the run: the long-poll answers 200 with the parsed result.
    result = {"hits": [], "text": "No matching sections were found in the knowledge base."}
    app.state.run_stores[0].mark_running(tenant, uuid.UUID(run_id))
    app.state.run_stores[0].finish_with_end(tenant, uuid.UUID(run_id), "completed", json.dumps(result), None)
    r = client.post(f"{API}/knowledge-bases/{kb['id']}/search?wait=1", json=body, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "completed" and r.json()["result"] == result
    assert len(fake_dbos.calls) == 2  # a finished search is answered as recorded, not re-enqueued


def test_search_hand_off_failure_is_503_and_marks_the_run(pg, tenant_of, kb_cleanup, fake_dbos):
    client, app = pg
    tenant, h = tenant_of()
    kb = _kb(client, h)
    fake_dbos.fail_times = 1
    run_id = str(uuid.uuid4())
    r = client.post(f"{API}/knowledge-bases/{kb['id']}/search?wait=0", json={"query": "q", "run_id": run_id}, headers=h)
    assert r.status_code == 503 and r.json()["run_id"] == run_id
    assert app.state.run_stores[0].get_run(tenant, uuid.UUID(run_id))["status"] == "failed"
    r = client.post(f"{API}/knowledge-bases/{kb['id']}/search?wait=0", json={"query": "q", "run_id": run_id}, headers=h)
    assert r.status_code == 202 and r.json()["status"] == "queued"
