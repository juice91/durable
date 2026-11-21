"""Knowledge base API (RM.7): focused implementer tests of the route contract and the hand-off.

The enqueue goes to a fake DBOSClient inside the real DBOSRunLauncher (as in test_tool_runs_api.py),
so these check the exact `eap.kb_ingest` enqueue call without a worker.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from .test_runs import SPEC, fake_dbos, pg, tenant_of, stream_settings, deltas  # noqa: F401 - fixtures
from .run_helpers import requires_pg

pytestmark = requires_pg

API = "/api/v1"
MD = "# Guide\n\nThe printer lives on floor 3.\n\n## Fixing\n\nTurn it off and on again.\n"


@pytest.fixture(autouse=True)
def _knowledge_env(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*,litellm:emb-small=1024")
    monkeypatch.delenv("EAP_KB_MAX_DOCUMENT_BYTES", raising=False)


@pytest.fixture
def kb_cleanup(pg):
    yield
    _client, app = pg
    with app.state.database_engine.begin() as conn:
        for t in app.state.test_tenants:
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})


def _create_kb(client, headers, name="Docs", model="test:hash"):
    r = client.post(f"{API}/knowledge-bases", json={"name": name, "embedding_model": model}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _put(client, headers, kb_id, key="guide.md", **body):
    payload = {"content": MD, "mime_type": "text/markdown", **body}
    return client.put(f"{API}/knowledge-bases/{kb_id}/documents/{key}", json=payload, headers=headers)


# ----------------------------------------------------------------------------- knowledge bases


def test_knowledge_base_crud_and_model_policy(pg, tenant_of, kb_cleanup):
    client, _ = pg
    _, headers = tenant_of()
    kb = _create_kb(client, headers)
    assert kb["embedding_model"] == "test:hash" and kb["dimensions"] == 1536
    assert kb["status"] == "ready" and kb["document_count"] == 0
    assert kb["chunking"]["max_tokens"] == 512

    dup = client.post(f"{API}/knowledge-bases", json={"name": "Docs", "embedding_model": "test:hash"}, headers=headers)
    assert dup.status_code == 409
    refused = client.post(f"{API}/knowledge-bases", json={"name": "X", "embedding_model": "openai:nope"}, headers=headers)
    assert refused.status_code == 422 and "not allowed" in refused.json()["detail"]
    assert _create_kb(client, headers, "Other", "litellm:emb-small")["dimensions"] == 1024

    r = client.patch(f"{API}/knowledge-bases/{kb['id']}", json={"description": "Printer docs"}, headers=headers)
    assert r.status_code == 200 and r.json()["description"] == "Printer docs" and r.json()["name"] == "Docs"
    assert client.patch(f"{API}/knowledge-bases/{kb['id']}", json={"name": "Other"}, headers=headers).status_code == 409
    assert client.patch(f"{API}/knowledge-bases/{kb['id']}", json={"name": None}, headers=headers).status_code == 422
    names = [k["name"] for k in client.get(f"{API}/knowledge-bases", headers=headers).json()["items"]]
    assert names == ["Docs", "Other"]
    assert client.get(f"{API}/knowledge-bases/not-a-uuid", headers=headers).status_code == 404


def test_embedding_models(pg, tenant_of):
    client, _ = pg
    _, headers = tenant_of()
    items = client.get(f"{API}/embedding-models", headers=headers).json()["items"]
    assert {i["id"]: i["dimensions"] for i in items} == {"litellm:emb-small": 1024, "test:hash": 1536, "test:hash768": 768}
    assert client.get(f"{API}/embedding-models").status_code == 401


def test_cross_tenant_is_404_everywhere(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, a = tenant_of()
    _, b = tenant_of()
    kb = _create_kb(client, a)
    assert _put(client, a, kb["id"]).status_code == 202
    base = f"{API}/knowledge-bases/{kb['id']}"
    calls = [
        ("get", base, None),
        ("patch", base, {"name": "stolen"}),
        ("delete", base, None),
        ("get", f"{base}/documents", None),
        ("get", f"{base}/documents/guide.md", None),
        ("put", f"{base}/documents/guide.md", {"content": "x", "mime_type": "text/plain"}),
        ("delete", f"{base}/documents/guide.md", None),
        ("post", f"{base}/documents/guide.md/reingest", None),
        ("post", f"{base}/reindex", {"embedding_model": "test:hash768"}),
        ("post", f"{base}/reindex", {"embedding_model": "test:hash768", "dry_run": True}),
        ("delete", f"{base}/reindex", None),
    ]
    for method, url, body in calls:
        kwargs = {"headers": b} if body is None else {"headers": b, "json": body}
        r = client.request(method.upper(), url, **kwargs)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
    assert client.get(f"{API}/knowledge-bases", headers=b).json()["items"] == []
    # Tenant A's data is untouched.
    assert client.get(f"{base}/documents/guide.md", headers=a).json()["latest_version"] == 1
    assert len(fake_dbos.calls) == 1


# ----------------------------------------------------------------------------------- documents


def test_upload_creates_a_kb_ingest_run_and_unchanged_is_a_noop(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    run_id = str(uuid.uuid4())
    r = _put(client, headers, kb["id"], run_id=run_id, metadata={"team": "it"}, source_url="https://docs.example/guide")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["unchanged"] is False and body["created"] is True and body["run_id"] == run_id
    doc = body["document"]
    assert doc["latest_version"] == 1 and doc["status"] == "pending" and doc["last_run_id"] == run_id
    assert doc["title"] == "guide.md" and doc["metadata"] == {"team": "it"} and "content" not in doc

    (options, args), = fake_dbos.calls
    assert options["workflow_name"] == "eap.kb_ingest" and options["queue_name"] == "eap-kb-ingest-fair"
    assert options["workflow_id"] == run_id and options["queue_partition_key"] == f"{tenant}:{kb['id']}"
    expected_input = json.dumps({"doc_version": 1, "document_id": doc["id"], "embed_split": True}, separators=(",", ":"))  # P1.C5
    assert args == (run_id, tenant, kb["id"], expected_input)

    run = client.get(f"{API}/runs/{run_id}", headers=headers).json()
    assert run["kind"] == "kb_ingest" and run["knowledge_base_id"] == kb["id"] and run["input"] == expected_input
    listed = client.get(f"{API}/runs?kind=kb_ingest", headers=headers).json()["items"]
    assert [i["run_id"] for i in listed] == [run_id]

    # Same content and fields: a no-op, no run, nothing enqueued.
    same = _put(client, headers, kb["id"], metadata={"team": "it"}, source_url="https://docs.example/guide")
    assert same.status_code == 200 and same.json()["unchanged"] is True and same.json()["run_id"] is None
    assert len(fake_dbos.calls) == 1
    # A retry of the first request (same run_id) gets its run back, handed off again (DBOS dedups).
    retry = _put(client, headers, kb["id"], run_id=run_id, metadata={"team": "it"}, source_url="https://docs.example/guide")
    assert retry.status_code == 202 and retry.json()["run_id"] == run_id and len(fake_dbos.calls) == 2
    assert fake_dbos.calls[1] == fake_dbos.calls[0]

    # Changed content: version 2 and a new run.
    changed = _put(client, headers, kb["id"], content=MD + "\nMore.\n", metadata={"team": "it"})
    assert changed.status_code == 202 and changed.json()["created"] is False
    assert changed.json()["document"]["latest_version"] == 2 and changed.json()["run_id"] != run_id
    assert json.loads(fake_dbos.calls[-1][1][3])["doc_version"] == 2
    # The old request's run_id with new content is not a retry of it: 409, nothing written.
    reused = _put(client, headers, kb["id"], run_id=run_id, content="new text", mime_type="text/plain")
    assert reused.status_code == 409
    assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents/guide.md", headers=headers).json()["latest_version"] == 2
    # Another document's run_id is a conflict, and nothing is written.
    clash = _put(client, headers, kb["id"], key="other.md", run_id=run_id, content="new text", mime_type="text/plain")
    assert clash.status_code == 409
    assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents/other.md", headers=headers).status_code == 404

    with_content = client.get(
        f"{API}/knowledge-bases/{kb['id']}/documents/guide.md?include_content=true", headers=headers
    ).json()
    assert with_content["content"] == MD + "\nMore.\n"


def test_upload_input_hardening(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    client, _ = pg
    _, headers = tenant_of()
    kb = _create_kb(client, headers)
    url = f"{API}/knowledge-bases/{kb['id']}/documents/a.md"
    hdrs = {**headers, "Content-Type": "application/json"}

    monkeypatch.setenv("EAP_KB_MAX_DOCUMENT_BYTES", "1000")
    big = _put(client, headers, kb["id"], key="a.md", content="x" * 2000, mime_type="text/plain")
    assert big.status_code == 413 and big.json()["detail"] == "request body too large (max 1000 bytes)"
    monkeypatch.delenv("EAP_KB_MAX_DOCUMENT_BYTES")
    # Above the default 256 KiB guard of other routes, below the 5 MiB upload cap.
    large = _put(client, headers, kb["id"], key="a.md", content="word " * 80_000, mime_type="text/plain")
    assert large.status_code == 202, large.text

    latin1 = '{"content": "caf\xe9", "mime_type": "text/plain"}'.encode("latin-1")
    assert client.put(url, content=latin1, headers=hdrs).status_code == 415
    nul = b'{"content": "a\x00b", "mime_type": "text/plain"}'
    assert client.put(url, content=nul, headers=hdrs).status_code == 415
    escaped_nul = _put(client, headers, kb["id"], key="a.md", content="a\u0000b", mime_type="text/plain")
    assert escaped_nul.status_code == 422

    def detail(**body):
        r = _put(client, headers, kb["id"], key="b.md", **body)
        assert r.status_code == 422, r.text
        return r.json()["detail"]

    assert "unsupported document mime type" in detail(mime_type="application/pdf")
    assert detail(content="   \n") == "content is empty"
    assert detail(document_key="other.md") == "document_key in the body must match the path"
    assert any("http or https" in e["msg"] for e in detail(source_url="javascript:alert(1)"))
    assert detail(metadata={"nested": {"a": 1}})  # metadata must be flat scalars
    assert detail(unknown=1)[0]["type"] == "extra_forbidden"
    too_long_key = client.put(f"{API}/knowledge-bases/{kb['id']}/documents/{'k' * 300}", json={"content": "x", "mime_type": "text/plain"}, headers=headers)
    assert too_long_key.status_code == 422
    assert len(fake_dbos.calls) == 1


def test_failed_enqueue_then_retry_requeues_the_same_run(pg, tenant_of, fake_dbos, kb_cleanup):
    from eap_run_store import ENQUEUE_FAILED_ERROR

    client, _ = pg
    _, headers = tenant_of()
    kb = _create_kb(client, headers)
    fake_dbos.fail_times = 1
    run_id = str(uuid.uuid4())
    r = _put(client, headers, kb["id"], run_id=run_id)
    assert r.status_code == 503
    run = client.get(f"{API}/runs/{run_id}", headers=headers).json()
    assert run["status"] == "failed" and run["error"] == ENQUEUE_FAILED_ERROR
    # The document holds the new version; the same upload (unchanged now) re-queues the stored run.
    again = _put(client, headers, kb["id"])
    assert again.status_code == 202 and again.json()["run_id"] == run_id
    assert client.get(f"{API}/runs/{run_id}", headers=headers).json()["status"] == "queued"
    assert len(fake_dbos.calls) == 1


def test_pending_document_without_a_run_gets_one_on_the_next_upload(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    # A request that stored the document (with its run id) and has not created the run yet.
    owner = str(uuid.uuid4())
    app.state.knowledge_store.put_document(tenant, kb["id"], document_key="guide.md", title="guide.md",
                                            content=MD, mime_type="text/markdown", metadata={}, run_id=owner)
    # While it may still be in flight, an identical upload is a no-op (no second run).
    young = _put(client, headers, kb["id"])
    assert young.status_code == 200 and young.json()["unchanged"] is True and fake_dbos.calls == []
    # After ORPHAN_AFTER_SECONDS the owner is presumed dead: the next upload starts the run.
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE kb_documents SET updated_at = now() - interval '2 minutes' WHERE tenant_id = :t"),
                     {"t": tenant})
    r = _put(client, headers, kb["id"])
    assert r.status_code == 202 and r.json()["run_id"] != owner and len(fake_dbos.calls) == 1
    assert r.json()["document"]["last_run_id"] == r.json()["run_id"]
    assert _put(client, headers, kb["id"]).status_code == 200  # now it has its run


def test_reingest_and_delete_document(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    first = _put(client, headers, kb["id"]).json()["run_id"]
    url = f"{API}/knowledge-bases/{kb['id']}/documents/guide.md"
    busy = client.post(f"{url}/reingest", headers=headers)
    assert busy.status_code == 409 and first in busy.json()["detail"]

    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'failed' WHERE id = :r"), {"r": first})
        conn.execute(text("UPDATE kb_documents SET status = 'failed', error = 'boom' WHERE tenant_id = :t"), {"t": tenant})
    run_id = str(uuid.uuid4())
    r = client.post(f"{url}/reingest", json={"run_id": run_id}, headers=headers)
    assert r.status_code == 202, r.text
    assert r.json()["run_id"] == run_id and r.json()["document"]["status"] == "pending"
    assert r.json()["document"]["error"] is None and r.json()["document"]["latest_version"] == 1
    assert client.post(f"{url}/reingest", json={"run_id": run_id}, headers=headers).status_code == 202  # idempotent
    # The first run's id names the same request (same document version): replayed, not a new run.
    replay = client.post(f"{url}/reingest", json={"run_id": first}, headers=headers)
    assert replay.status_code == 202 and replay.json()["run_id"] == first
    # A new run_id while the reingest run is still queued: busy.
    assert client.post(f"{url}/reingest", json={"run_id": str(uuid.uuid4())}, headers=headers).status_code == 409
    assert client.post(f"{API}/knowledge-bases/{kb['id']}/documents/nope.md/reingest", headers=headers).status_code == 404

    assert client.delete(url, headers=headers).status_code == 204
    assert client.delete(url, headers=headers).status_code == 404
    assert client.get(url, headers=headers).status_code == 404
    assert client.get(f"{API}/runs/{run_id}", headers=headers).status_code == 200  # runs stay


# ------------------------------------------------------------------------------ delete with refs


def test_delete_referenced_knowledge_base_needs_force(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, headers = tenant_of()
    kb = _create_kb(client, headers)
    spare = _create_kb(client, headers, "Spare")
    spec = {**SPEC, "knowledge_bases": [{"id": kb["id"].upper()}]}
    agent = client.post(f"{API}/agents", json={"spec": spec}, headers=headers)
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["id"]
    # An unreferenced knowledge base is deleted at once.
    assert client.delete(f"{API}/knowledge-bases/{spare['id']}", headers=headers).status_code == 204
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=headers).status_code == 201

    blocked = client.delete(f"{API}/knowledge-bases/{kb['id']}", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["agents"] == [
        {"agent_id": agent_id, "agent_name": SPEC["name"], "version": 1, "archived": False, "draft": False}
    ]
    assert SPEC["name"] in blocked.json()["detail"]

    # Publishing a version without it unblocks the delete.
    assert client.patch(f"{API}/agents/{agent_id}", json={"spec": SPEC}, headers=headers).status_code == 200
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=headers).status_code == 201
    assert client.delete(f"{API}/knowledge-bases/{kb['id']}", headers=headers).status_code == 204


def test_a_draft_only_reference_blocks_the_delete_and_the_draft_still_runs_after_force(
    pg, tenant_of, fake_dbos, kb_cleanup
):
    """A knowledge base attached only by an agent's draft: 409 lists the agent with draft=true and
    version null; after a forced delete the draft test run starts (201, the retrieve tool answers
    "no longer exist" in the worker) while publishing that draft is still 422."""
    client, _ = pg
    _, headers = tenant_of()
    kb = _create_kb(client, headers)
    agent_id = client.post(
        f"{API}/agents", json={"spec": {**SPEC, "knowledge_bases": [{"id": kb["id"]}]}}, headers=headers
    ).json()["id"]
    blocked = client.delete(f"{API}/knowledge-bases/{kb['id']}", headers=headers)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["agents"] == [
        {"agent_id": agent_id, "agent_name": SPEC["name"], "version": None, "archived": False, "draft": True}
    ]
    # A published version without it and a draft that still has it: still a draft-only reference.
    assert client.patch(f"{API}/agents/{agent_id}", json={"spec": SPEC}, headers=headers).status_code == 200
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=headers).status_code == 201
    spec = {**SPEC, "knowledge_bases": [{"id": kb["id"]}]}
    assert client.patch(f"{API}/agents/{agent_id}", json={"spec": spec}, headers=headers).status_code == 200
    [ref] = client.delete(f"{API}/knowledge-bases/{kb['id']}", headers=headers).json()["agents"]
    assert (ref["version"], ref["draft"]) == (None, True)

    assert client.delete(f"{API}/knowledge-bases/{kb['id']}?force=true", headers=headers).status_code == 204
    run_id = str(uuid.uuid4())
    r = client.post(f"{API}/agents/{agent_id}/runs", json={"run_id": run_id, "input": "hi", "version": "draft"},
                    headers=headers)
    assert r.status_code == 201, r.text
    [(options, args)] = fake_dbos.calls
    assert options["workflow_id"] == run_id and args[3] == 0  # version 0: the draft snapshot
    assert [ref["id"] for ref in json.loads(args[4])["knowledge_bases"]] == [kb["id"]]
    r = client.post(f"{API}/agents/{agent_id}/versions", headers=headers)
    assert r.status_code == 422 and r.json()["detail"] == f"knowledge base '{kb['id']}' does not exist in this tenant"


def test_a_published_and_draft_reference_is_listed_once_as_published(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, headers = tenant_of()
    kb = _create_kb(client, headers)
    spec = {**SPEC, "knowledge_bases": [{"id": kb["id"]}]}
    agent_id = client.post(f"{API}/agents", json={"spec": spec}, headers=headers).json()["id"]
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=headers).status_code == 201
    [ref] = client.delete(f"{API}/knowledge-bases/{kb['id']}", headers=headers).json()["agents"]
    assert (ref["version"], ref["draft"]) == (1, False)
    # The draft drops it, the published version still has it: listed as published, not draft.
    assert client.patch(f"{API}/agents/{agent_id}", json={"spec": SPEC}, headers=headers).status_code == 200
    [ref] = client.delete(f"{API}/knowledge-bases/{kb['id']}", headers=headers).json()["agents"]
    assert (ref["version"], ref["draft"]) == (1, False)


def test_force_delete(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, headers = tenant_of()
    kb = _create_kb(client, headers)
    _put(client, headers, kb["id"])
    agent_id = client.post(
        f"{API}/agents", json={"spec": {**SPEC, "knowledge_bases": [{"id": kb["id"]}]}}, headers=headers
    ).json()["id"]
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=headers).status_code == 201
    assert client.delete(f"{API}/knowledge-bases/{kb['id']}", headers=headers).status_code == 409
    assert client.delete(f"{API}/knowledge-bases/{kb['id']}?force=true", headers=headers).status_code == 204
    assert client.get(f"{API}/knowledge-bases/{kb['id']}", headers=headers).status_code == 404
    assert len(client.get(f"{API}/runs?kind=kb_ingest", headers=headers).json()["items"]) == 1


def test_delete_retries_once_on_deadlock():
    from sqlalchemy.exc import OperationalError

    from eap_api.services.knowledge import _retry_deadlock_once

    class Orig(Exception):
        def __init__(self, sqlstate):
            self.sqlstate = sqlstate

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("DELETE", {}, Orig("40P01"))
        return True

    assert _retry_deadlock_once(flaky) is True and len(calls) == 2

    def other():
        raise OperationalError("DELETE", {}, Orig("57014"))

    with pytest.raises(OperationalError):
        _retry_deadlock_once(other)


# -------------------------------------------------------------------------------------- reindex


def test_reindex_dry_run_start_conflict_and_cancel(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    client, _ = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    url = f"{API}/knowledge-bases/{kb['id']}/reindex"

    dry = client.post(url, json={"embedding_model": "test:hash768", "dry_run": True}, headers=headers)
    assert dry.status_code == 200, dry.text
    assert dry.json() | {"estimated_cost_usd": None} == {
        "dry_run": True, "knowledge_base_id": kb["id"], "embedding_model": "test:hash768", "dimensions": 768,
        "chunks": 0, "approx_tokens": 0, "max_ingest_tokens": 2_000_000, "exceeds_budget": False,
        "estimated_cost_usd": None, "price_source": "override", "cost_note": None, "reindex_pending": False,
    }
    assert Decimal(dry.json()["estimated_cost_usd"]) == 0
    assert fake_dbos.calls == []
    assert client.post(url, json={"embedding_model": "openai:nope"}, headers=headers).status_code == 422

    run_id = str(uuid.uuid4())
    started = client.post(url, json={"embedding_model": "test:hash768", "run_id": run_id}, headers=headers)
    assert started.status_code == 202, started.text
    assert started.json()["run_id"] == run_id
    assert started.json()["knowledge_base"]["status"] == "reindexing"
    assert started.json()["knowledge_base"]["pending_generation"] == 2
    (options, args), = fake_dbos.calls
    assert options["workflow_name"] == "eap.kb_ingest" and args == (run_id, tenant, kb["id"], '{"embed_split":true,"reindex":2}')
    # Retry of the same request: same run, no second reindex.
    again = client.post(url, json={"embedding_model": "test:hash768", "run_id": run_id}, headers=headers)
    assert again.status_code == 202 and again.json()["run_id"] == run_id
    conflict = client.post(url, json={"embedding_model": "test:hash"}, headers=headers)
    assert conflict.status_code == 409 and "already reindexing" in conflict.json()["detail"]

    assert client.delete(url, headers=headers).status_code == 204
    kb_now = client.get(f"{API}/knowledge-bases/{kb['id']}", headers=headers).json()
    assert kb_now["status"] == "ready" and kb_now["pending_generation"] is None
    assert client.delete(url, headers=headers).status_code == 204  # nothing pending: still 204
    next_one = client.post(url, json={"embedding_model": "test:hash768"}, headers=headers)
    assert next_one.status_code == 202 and next_one.json()["knowledge_base"]["pending_generation"] == 3


def test_reindex_retry_after_failure(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    url = f"{API}/knowledge-bases/{kb['id']}/reindex"
    first = client.post(url, json={"embedding_model": "test:hash768"}, headers=headers).json()["run_id"]
    app.state.knowledge_store.mark_reindex_failed(tenant, kb["id"], 2, "embedding_failed")
    retry = client.post(url, json={"embedding_model": "test:hash768"}, headers=headers)
    assert retry.status_code == 202 and retry.json()["run_id"] != first
    assert fake_dbos.calls[-1][1][3] == '{"embed_split":true,"reindex":2}'
    # Another model while one is pending (failed) is still a conflict.
    assert client.post(url, json={"embedding_model": "test:hash"}, headers=headers).status_code == 409


# --------------------------------------------------------------------------------- price estimate


def test_estimate_embedding_cost(tmp_path, monkeypatch):
    from datetime import date

    from eap_api.routes.knowledge import estimate_embedding_cost

    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"models": {
        "test:*": [{"effective_from": "2026-01-01", "input_per_mtok": "2", "output_per_mtok": "8"}],
        "litellm:emb": [
            {"effective_from": "2026-01-01", "input_per_mtok": "0.02", "output_per_mtok": "0"},
            {"effective_from": "2027-01-01", "input_per_mtok": "9", "output_per_mtok": "0"},
        ],
    }}))
    monkeypatch.setenv("EAP_MODEL_PRICES_FILE", str(prices))
    day = date(2026, 11, 13)
    assert estimate_embedding_cost("test:hash", 500_000, day) == ("1.0000000000", "override", None)
    assert estimate_embedding_cost("litellm:emb", 1_000_000, day)[0] == "0.0200000000"
    cost, source, note = estimate_embedding_cost("litellm:other", 10, day)
    assert cost is None and source is None and "no price" in note
    cost, _, note = estimate_embedding_cost("openai:text-embedding-3-small", 10, day)
    assert cost is None and "genai-prices" in note
    monkeypatch.setenv("EAP_MODEL_PRICES_FILE", str(tmp_path / "missing.json"))
    assert estimate_embedding_cost("test:hash", 10, day)[0] is None


# ------------------------------------------------------------------- review fixes (concurrency etc.)


def test_chunked_body_without_content_length_over_the_cap_is_413(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    client, _ = pg
    _, headers = tenant_of()
    kb = _create_kb(client, headers)
    monkeypatch.setenv("EAP_KB_MAX_DOCUMENT_BYTES", "2000")
    payload = json.dumps({"content": "x" * 5000, "mime_type": "text/plain"}).encode()

    def chunks():
        for i in range(0, len(payload), 500):
            yield payload[i : i + 500]

    r = client.put(
        f"{API}/knowledge-bases/{kb['id']}/documents/a.txt",
        content=chunks(),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert r.status_code == 413 and r.json()["detail"] == "request body too large (max 2000 bytes)"
    assert fake_dbos.calls == []


def test_concurrent_identical_puts_start_one_run(pg, tenant_of, fake_dbos, kb_cleanup):
    from concurrent.futures import ThreadPoolExecutor

    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _put(client, headers, kb["id"]), range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(202) == 1 and codes.count(200) == 7, codes
    with app.state.database_engine.connect() as conn:
        runs = conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t AND kind = 'kb_ingest'"),
                            {"t": tenant}).scalar_one()
    assert runs == 1 and len(fake_dbos.calls) == 1


def test_put_and_delete_race_still_records_the_run(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    run_store = app.state.run_stores[0]
    original = run_store.create_kb_ingest_run

    def delete_first(*args, **kwargs):
        app.state.knowledge_store.delete_document(tenant, kb["id"], "guide.md")
        return original(*args, **kwargs)

    monkeypatch.setattr(run_store, "create_kb_ingest_run", delete_first)
    r = _put(client, headers, kb["id"])
    # The run is created and enqueued; the worker finds the document gone and ends it cancelled.
    assert r.status_code == 202 and len(fake_dbos.calls) == 1
    assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents/guide.md", headers=headers).status_code == 404


def test_put_and_reingest_race(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    store = app.state.knowledge_store
    owner = str(uuid.uuid4())
    store.put_document(tenant, kb["id"], document_key="guide.md", title="guide.md", content=MD,
                       mime_type="text/markdown", metadata={}, run_id=owner)
    # The upload that owns the version is between its write and its run insert: reingest waits.
    busy = client.post(f"{API}/knowledge-bases/{kb['id']}/documents/guide.md/reingest", headers=headers)
    assert busy.status_code == 409 and owner in busy.json()["detail"] and fake_dbos.calls == []

    # Two reingests: the compare-and-set on last_run_id lets exactly one win.
    from eap_run_store.knowledge import IngestRunConflict

    first = store.request_reingest(tenant, kb["id"], "guide.md", run_id="r1", expected_last_run_id=owner, doc_version=1)
    assert first["last_run_id"] == "r1"
    with pytest.raises(IngestRunConflict):
        store.request_reingest(tenant, kb["id"], "guide.md", run_id="r2", expected_last_run_id=owner, doc_version=1)
    doc_id = first["id"]
    assert not store.attach_ingest_run(tenant, doc_id, "r3", doc_version=1, expected_last_run_id=owner)
    assert not store.attach_ingest_run(tenant, doc_id, "r3", doc_version=2, expected_last_run_id="r1")
    assert store.attach_ingest_run(tenant, doc_id, "r3", doc_version=1, expected_last_run_id="r1")


def test_reingest_of_a_ready_document_is_a_noop(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    first = _put(client, headers, kb["id"]).json()["run_id"]
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'completed' WHERE id = :r"), {"r": first})
        conn.execute(text("UPDATE kb_documents SET status = 'ready', active_version = 1 WHERE tenant_id = :t"),
                     {"t": tenant})
    r = client.post(f"{API}/knowledge-bases/{kb['id']}/documents/guide.md/reingest", headers=headers)
    assert r.status_code == 200 and r.json()["run_id"] is None and r.json()["unchanged"] is True
    assert r.json()["document"]["status"] == "ready" and len(fake_dbos.calls) == 1


def test_enqueue_failure_503_carries_run_id(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, headers = tenant_of()
    kb = _create_kb(client, headers)
    fake_dbos.fail_times = 1
    r = _put(client, headers, kb["id"])
    assert r.status_code == 503 and r.json()["run_id"]
    assert "same run_id" in r.json()["detail"]
    again = _put(client, headers, kb["id"])  # the same upload without run_id re-queues the recorded run
    assert again.status_code == 202 and again.json()["run_id"] == r.json()["run_id"]


def test_reindex_enqueue_failure_is_cancelled_and_a_plain_retry_works(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    url = f"{API}/knowledge-bases/{kb['id']}/reindex"
    fake_dbos.fail_times = 1
    failed = client.post(url, json={"embedding_model": "test:hash768"}, headers=headers)
    assert failed.status_code == 503 and failed.json()["run_id"] and "cancelled" in failed.json()["detail"]
    now = client.get(f"{API}/knowledge-bases/{kb['id']}", headers=headers).json()
    assert now["status"] == "ready" and now["pending_generation"] is None
    assert client.get(f"{API}/runs/{failed.json()['run_id']}", headers=headers).json()["status"] == "failed"
    retry = client.post(url, json={"embedding_model": "test:hash768"}, headers=headers)
    assert retry.status_code == 202 and retry.json()["knowledge_base"]["pending_generation"] == 3
    assert args_of(fake_dbos.calls[-1]) == (retry.json()["run_id"], tenant, kb["id"], '{"embed_split":true,"reindex":3}')
    # Replay with the right run_id but another model is a conflict.
    other = client.post(url, json={"embedding_model": "test:hash", "run_id": retry.json()["run_id"]}, headers=headers)
    assert other.status_code == 409


def args_of(call):
    return call[1]


def test_invalid_ids_are_404_on_every_route(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, headers = tenant_of()
    for bad in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
        base = f"{API}/knowledge-bases/{bad}"
        calls = [
            ("GET", base, None), ("PATCH", base, {"name": "x"}), ("DELETE", base, None),
            ("GET", f"{base}/documents", None), ("GET", f"{base}/documents/a.md", None),
            ("PUT", f"{base}/documents/a.md", {"content": "hello", "mime_type": "text/plain"}),
            ("DELETE", f"{base}/documents/a.md", None), ("POST", f"{base}/documents/a.md/reingest", None),
            ("POST", f"{base}/reindex", {"embedding_model": "test:hash768"}),
            ("POST", f"{base}/reindex", {"embedding_model": "test:hash768", "dry_run": True}),
            ("DELETE", f"{base}/reindex", None),
        ]
        for method, url, body in calls:
            kwargs = {"headers": headers} if body is None else {"headers": headers, "json": body}
            r = client.request(method, url, **kwargs)
            assert r.status_code == 404, (method, url, r.status_code, r.text)
    assert fake_dbos.calls == []


# ------------------------------------------------------------------- re-review fixes


def _backdate_runs(app, tenant):
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET created_at = now() - interval '2 minutes' WHERE tenant_id = :t"),
                     {"t": tenant})


def test_crash_between_run_insert_and_enqueue_is_recovered(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    run_store = app.state.run_stores[0]

    def mark_fails(*args, **kwargs):
        raise ConnectionError("database gone too")

    # The enqueue and the failure mark both fail: the run stays 'queued' with no workflow, exactly
    # as if the API died between creating the run and enqueuing it.
    original_mark = run_store.mark_enqueue_failed
    monkeypatch.setattr(run_store, "mark_enqueue_failed", mark_fails)
    fake_dbos.fail_times = 1
    run_id = str(uuid.uuid4())
    assert _put(client, headers, kb["id"], run_id=run_id).status_code == 503
    # Restore only this patch (monkeypatch.undo() would also drop the autouse auth and model env).
    monkeypatch.setattr(run_store, "mark_enqueue_failed", original_mark)
    assert client.get(f"{API}/runs/{run_id}", headers=headers).json()["status"] == "queued"
    assert fake_dbos.calls == []

    # 1. A retry with the same run_id hands it off again.
    r = _put(client, headers, kb["id"], run_id=run_id)
    assert r.status_code == 202 and r.json()["run_id"] == run_id
    assert [c[0]["workflow_id"] for c in fake_dbos.calls] == [run_id]

    # 2. Without run_id: a fresh queued run is left alone (no-op) ...
    assert _put(client, headers, kb["id"]).status_code == 200 and len(fake_dbos.calls) == 1
    # ... but one still queued after ORPHAN_AFTER_SECONDS is handed off again (idempotent in DBOS).
    _backdate_runs(app, tenant)
    stale = _put(client, headers, kb["id"])
    assert stale.status_code == 202 and stale.json()["run_id"] == run_id and len(fake_dbos.calls) == 2
    # 3. Reingest does the same instead of answering 409.
    re = client.post(f"{API}/knowledge-bases/{kb['id']}/documents/guide.md/reingest", headers=headers)
    assert re.status_code == 202 and re.json()["run_id"] == run_id and len(fake_dbos.calls) == 3
    assert len(client.get(f"{API}/runs?kind=kb_ingest", headers=headers).json()["items"]) == 1


def test_stalled_request_cannot_start_a_second_run_after_a_recovery(pg, tenant_of, fake_dbos, kb_cleanup):
    from eap_api.services.knowledge import _renew_claim

    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    store = app.state.knowledge_store
    stalled = str(uuid.uuid4())
    doc = store.put_document(tenant, kb["id"], document_key="guide.md", title="guide.md", content=MD,
                             mime_type="text/markdown", metadata={}, run_id=stalled).document
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE kb_documents SET updated_at = now() - interval '2 minutes' WHERE tenant_id = :t"),
                     {"t": tenant})
    recovered = _put(client, headers, kb["id"])
    assert recovered.status_code == 202 and recovered.json()["run_id"] != stalled
    # The stalled request wakes up: its compare-and-set fails, so it creates no run.
    assert _renew_claim(store, tenant, doc, stalled) is False
    assert _renew_claim(store, tenant, doc, recovered.json()["run_id"]) is True


def test_concurrent_reindex_retries_start_one_run(pg, tenant_of, fake_dbos, kb_cleanup):
    from concurrent.futures import ThreadPoolExecutor

    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    url = f"{API}/knowledge-bases/{kb['id']}/reindex"
    assert client.post(url, json={"embedding_model": "test:hash768"}, headers=headers).status_code == 202
    app.state.knowledge_store.mark_reindex_failed(tenant, kb["id"], 2, "embedding_failed")
    calls = len(fake_dbos.calls)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda _: client.post(url, json={"embedding_model": "test:hash768"}, headers=headers), range(6)
        ))
    codes = sorted(r.status_code for r in results)
    assert codes.count(202) == 1 and codes.count(409) == 5, codes
    assert len(fake_dbos.calls) == calls + 1
    assert client.get(f"{API}/knowledge-bases/{kb['id']}", headers=headers).json()["status"] == "reindexing"


def test_failed_hand_off_of_a_reindex_retry_restores_reindex_failed(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    url = f"{API}/knowledge-bases/{kb['id']}/reindex"
    client.post(url, json={"embedding_model": "test:hash768"}, headers=headers)
    app.state.knowledge_store.mark_reindex_failed(tenant, kb["id"], 2, "embedding_failed")
    fake_dbos.fail_times = 1
    assert client.post(url, json={"embedding_model": "test:hash768"}, headers=headers).status_code == 503
    assert client.get(f"{API}/knowledge-bases/{kb['id']}", headers=headers).json()["status"] == "reindex_failed"
    assert client.post(url, json={"embedding_model": "test:hash768"}, headers=headers).status_code == 202


def test_orphan_recovery_loses_to_a_stalled_owners_lease_renewal(pg, tenant_of, fake_dbos, kb_cleanup):
    from eap_api.services.knowledge import _renew_claim

    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    store = app.state.knowledge_store
    owner = str(uuid.uuid4())
    doc = store.put_document(tenant, kb["id"], document_key="guide.md", title="guide.md", content=MD,
                             mime_type="text/markdown", metadata={}, run_id=owner).document
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE kb_documents SET updated_at = now() - interval '2 minutes' WHERE tenant_id = :t"),
                     {"t": tenant})
    seen = store.get_document(tenant, kb["id"], "guide.md")  # what a recovering request read
    assert _renew_claim(store, tenant, doc, owner) is True  # the stalled owner renews first
    # The recovery's compare-and-set on (last_run_id, updated_at) now loses: no second run.
    assert not store.attach_ingest_run(tenant, doc["id"], "recovery", doc_version=1,
                                       expected_last_run_id=owner, expected_updated_at=seen["updated_at"])
    assert _put(client, headers, kb["id"]).status_code == 200 and fake_dbos.calls == []
    # Unchanged rows still match their own updated_at.
    fresh = store.get_document(tenant, kb["id"], "guide.md")
    assert store.attach_ingest_run(tenant, doc["id"], owner, doc_version=1,
                                   expected_last_run_id=owner, expected_updated_at=fresh["updated_at"])


def test_reindex_replay_while_failed_claims_the_generation(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, headers = tenant_of()
    kb = _create_kb(client, headers)
    url = f"{API}/knowledge-bases/{kb['id']}/reindex"
    client.post(url, json={"embedding_model": "test:hash768"}, headers=headers)
    app.state.knowledge_store.mark_reindex_failed(tenant, kb["id"], 2, "embedding_failed")
    fake_dbos.fail_times = 1
    rid = str(uuid.uuid4())
    failed = client.post(url, json={"embedding_model": "test:hash768", "run_id": rid}, headers=headers)
    assert failed.status_code == 503 and failed.json()["run_id"] == rid
    assert client.get(f"{API}/knowledge-bases/{kb['id']}", headers=headers).json()["status"] == "reindex_failed"
    # Another request claims the generation first: the replay of rid now loses with 409.
    assert app.state.knowledge_store.claim_reindex_retry(tenant, kb["id"], 2)
    assert client.post(url, json={"embedding_model": "test:hash768", "run_id": rid}, headers=headers).status_code == 409
    # Back in the failed state, the replay claims it and hands rid off again.
    app.state.knowledge_store.mark_reindex_failed(tenant, kb["id"], 2, "embedding_failed")
    calls = len(fake_dbos.calls)
    ok = client.post(url, json={"embedding_model": "test:hash768", "run_id": rid}, headers=headers)
    assert ok.status_code == 202 and ok.json()["run_id"] == rid and len(fake_dbos.calls) == calls + 1
    assert ok.json()["knowledge_base"]["status"] == "reindexing"


@pytest.mark.parametrize("value", ["lots", "-1"])
def test_startup_rejects_a_bad_exact_search_cap(monkeypatch, value):
    """Final review: EAP_KB_EXACT_SEARCH_MAX_CHUNKS fails the API at start, not silently per search."""
    from eap_api.routes.knowledge import check_knowledge_configuration

    monkeypatch.setenv("EAP_KB_EXACT_SEARCH_MAX_CHUNKS", value)
    with pytest.raises(ValueError, match="EAP_KB_EXACT_SEARCH_MAX_CHUNKS"):
        check_knowledge_configuration()
    monkeypatch.setenv("EAP_KB_EXACT_SEARCH_MAX_CHUNKS", "0")
    check_knowledge_configuration()
