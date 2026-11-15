"""RM.7 acceptance tests for the knowledge base API: what test_knowledge_api.py does not cover."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from .run_helpers import requires_pg
from .test_knowledge_api import API, MD, _create_kb, _put, kb_cleanup, _knowledge_env  # noqa: F401 - fixtures
from .test_runs import SPEC, fake_dbos, pg, tenant_of, stream_settings, deltas  # noqa: F401 - fixtures

pytestmark = requires_pg


def _snapshot(client, headers, kb_id):
    """Everything tenant A can see about a knowledge base, for before/after comparison."""
    base = f"{API}/knowledge-bases/{kb_id}"
    return {
        "kbs": client.get(f"{API}/knowledge-bases", headers=headers).json(),
        "kb": client.get(base, headers=headers).json(),
        "docs": client.get(f"{base}/documents", headers=headers).json(),
        "doc": client.get(f"{base}/documents/guide.md?include_content=true", headers=headers).json(),
        "runs": client.get(f"{API}/runs?kind=kb_ingest", headers=headers).json(),
    }


def _counts(app, tenant):
    with app.state.database_engine.connect() as conn:
        return [
            conn.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
            for t in ("knowledge_bases", "kb_documents", "runs")
        ]


def test_cross_tenant_every_route_and_method(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    ta, a = tenant_of()
    tb, b = tenant_of()
    kb = _create_kb(client, a)
    run_a = _put(client, a, kb["id"]).json()["run_id"]
    base = f"{API}/knowledge-bases/{kb['id']}"
    before, counts_a, calls = _snapshot(client, a, kb["id"]), _counts(app, ta), len(fake_dbos.calls)
    doc = f"{base}/documents/guide.md"
    big = {"content": "x", "mime_type": "text/plain"}
    calls_b = [
        ("GET", base, None), ("PATCH", base, {"description": "stolen"}),
        ("DELETE", base, None), ("DELETE", f"{base}?force=true", None),
        ("GET", f"{base}/documents", None), ("GET", f"{doc}?include_content=true", None),
        ("PUT", doc, big), ("PUT", doc, {**big, "run_id": str(uuid.uuid4())}),
        ("DELETE", doc, None), ("POST", f"{doc}/reingest", None),
        ("POST", f"{doc}/reingest", {"run_id": str(uuid.uuid4())}),
        ("POST", f"{base}/reindex", {"embedding_model": "test:hash768"}),
        ("POST", f"{base}/reindex", {"embedding_model": "test:hash768", "dry_run": True}),
        ("DELETE", f"{base}/reindex", None),
        ("GET", f"{API}/runs/{run_a}", None), ("GET", f"{API}/runs/{run_a}/events", None),
    ]
    for method, url, body in calls_b:
        kw = {"headers": b, **({} if body is None else {"json": body})}
        r = client.request(method, url, **kw)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
    assert client.get(f"{API}/runs?kind=kb_ingest", headers=b).json()["items"] == []
    assert client.get(f"{API}/knowledge-bases", headers=b).json()["items"] == []
    assert _counts(app, tb) == [0, 0, 0]
    assert _snapshot(client, a, kb["id"]) == before and _counts(app, ta) == counts_a
    assert len(fake_dbos.calls) == calls


def test_cross_tenant_run_id_reuse_does_not_touch_other_tenants_run(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    ta, a = tenant_of()
    tb, b = tenant_of()
    kb_a, kb_b = _create_kb(client, a), _create_kb(client, b)
    run_a = _put(client, a, kb_a["id"]).json()["run_id"]
    run_before = client.get(f"{API}/runs/{run_a}", headers=a).json()
    for r in (
        _put(client, b, kb_b["id"], run_id=run_a),
        client.post(f"{API}/knowledge-bases/{kb_b['id']}/reindex", headers=b,
                    json={"embedding_model": "test:hash768", "run_id": run_a}),
    ):
        assert r.status_code in (404, 409), r.text
    assert client.get(f"{API}/runs/{run_a}", headers=a).json() == run_before
    assert client.get(f"{API}/runs?kind=kb_ingest", headers=b).json()["items"] == []
    assert len(fake_dbos.calls) == 1
    # A failed reindex attempt must not leave B's knowledge base stuck reindexing.
    assert client.get(f"{API}/knowledge-bases/{kb_b['id']}", headers=b).json()["status"] == "ready"


# ------------------------------------------------------------------------------------- bodies


def _raw_body(size: int) -> bytes:
    """A valid upload body of exactly `size` bytes."""
    head, tail = b'{"mime_type":"text/plain","content":"', b'"}'
    return head + b"a" * (size - len(head) - len(tail)) + tail


def test_body_cap_boundary_and_lying_content_length(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    client, app = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    url = f"{API}/knowledge-bases/{kb['id']}/documents/cap.txt"
    hdrs = {**h, "Content-Type": "application/json"}
    monkeypatch.setenv("EAP_KB_MAX_DOCUMENT_BYTES", "3000")
    assert client.put(url, content=_raw_body(3000), headers=hdrs).status_code == 202
    over = client.put(url, content=_raw_body(3001), headers=hdrs)
    assert over.status_code == 413 and "max 3000 bytes" in over.json()["detail"]
    # A declared length far over the cap with a tiny body is refused too.
    lie = client.put(url, content=_raw_body(100), headers={**hdrs, "Content-Length": "999999999"})
    assert lie.status_code == 413, lie.text
    # The refused uploads stored nothing: still one document, one version, one enqueue.
    doc = client.get(url, headers=h).json()
    assert doc["latest_version"] == 1 and len(fake_dbos.calls) == 1
    # Other routes keep the small default cap even though the upload route has its own.
    huge = client.post(f"{API}/knowledge-bases", headers=h,
                       json={"name": "n", "embedding_model": "test:hash", "description": "d" * 300_000})
    assert huge.status_code == 413


def test_malformed_bodies_are_rejected_and_store_nothing(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    url = f"{API}/knowledge-bases/{kb['id']}/documents/m.md"
    hdrs = {**h, "Content-Type": "application/json"}
    ok = {"content": MD, "mime_type": "text/markdown"}
    cases = {
        "non-utf8": (b'{"content":"\xff\xfe","mime_type":"text/plain"}', 415),
        "raw NUL": (b'{"content":"a\x00b","mime_type":"text/plain"}', 415),
        "escaped NUL": (json.dumps({**ok, "content": "a\u0000b"}).encode(), 422),
        "invalid json": (b'{"content": "abc", ', 422),
        "not an object": (b'["content"]', 422),
        "missing mime": (json.dumps({"content": MD}).encode(), 422),
        "unsupported mime": (json.dumps({**ok, "mime_type": "application/pdf"}).encode(), 422),
        "empty content": (json.dumps({**ok, "content": ""}).encode(), 422),
        "whitespace content": (json.dumps({**ok, "content": " \n\t"}).encode(), 422),
    }
    for name, (body, code) in cases.items():
        r = client.put(url, content=body, headers=hdrs)
        assert r.status_code == code, (name, r.status_code, r.text)
    assert client.get(url, headers=h).status_code == 404
    assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents", headers=h).json()["items"] == []
    assert fake_dbos.calls == []


def test_document_that_extracts_empty_is_422(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    for key, mime, content in [
        ("a.html", "text/html", "<html><head><style>p{}</style></head><body><script>1</script><nav>x</nav></body></html>"),
        ("b.md", "text/markdown", "<!-- only a comment -->\n\n"),
    ]:
        r = _put(client, h, kb["id"], key=key, mime_type=mime, content=content)
        assert r.status_code == 422, (key, r.status_code, r.text)
        assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents/{key}", headers=h).status_code == 404
    assert fake_dbos.calls == []


# --------------------------------------------------------------------------------------- runs


def _runs(client, h):
    return client.get(f"{API}/runs?kind=kb_ingest", headers=h).json()["items"]


def test_unchanged_reupload_noop_then_changes_make_versions(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    tenant, h = tenant_of()
    kb = _create_kb(client, h)
    first = _put(client, h, kb["id"], title="Guide", metadata={"a": 1})
    assert first.status_code == 202 and len(fake_dbos.calls) == 1
    for _ in range(2):
        same = _put(client, h, kb["id"], title="Guide", metadata={"a": 1})
        assert same.status_code == 200 and same.json()["unchanged"] is True and same.json()["run_id"] is None
    assert len(fake_dbos.calls) == 1 and len(_runs(client, h)) == 1
    # Changed content, then changed metadata, then changed title: one version and one run each.
    steps = [{"content": MD + "\nmore\n"}, {"content": MD + "\nmore\n", "metadata": {"a": 2}},
             {"content": MD + "\nmore\n", "metadata": {"a": 2}, "title": "Renamed"}]
    for n, change in enumerate(steps, start=2):
        r = _put(client, h, kb["id"], **{"title": "Guide", "metadata": {"a": 1}, **change})
        assert r.status_code == 202, r.text
        assert r.json()["document"]["latest_version"] == n and len(fake_dbos.calls) == n
        options, args = fake_dbos.calls[-1]
        run_id = r.json()["run_id"]
        assert options["workflow_id"] == run_id and options["workflow_name"] == "eap.kb_ingest"
        doc_id = r.json()["document"]["id"]
        want = json.dumps({"doc_version": n, "document_id": doc_id, "embed_split": True}, separators=(",", ":"))  # P1.C5
        assert args == (run_id, tenant, kb["id"], want)
    assert len(_runs(client, h)) == 4 and len({r["run_id"] for r in _runs(client, h)}) == 4


def test_retry_with_same_run_id_is_one_run(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    rid = str(uuid.uuid4())
    r1 = _put(client, h, kb["id"], run_id=rid)
    r2 = _put(client, h, kb["id"], run_id=rid)
    assert r1.status_code == r2.status_code == 202 and r1.json()["run_id"] == r2.json()["run_id"] == rid
    assert r1.json()["document"]["latest_version"] == r2.json()["document"]["latest_version"] == 1
    assert [r["run_id"] for r in _runs(client, h)] == [rid]
    # Any repeated enqueue names the same workflow id with identical arguments: DBOS dedups it.
    assert len({json.dumps([c[0], c[1]], sort_keys=True, default=str) for c in fake_dbos.calls}) == 1
    assert {c[0]["workflow_id"] for c in fake_dbos.calls} == {rid}


# ------------------------------------------------------------------------- delete during ingest


def _run_row(app, run_id):
    with app.state.database_engine.connect() as conn:
        return conn.execute(
            text("SELECT status, kind, knowledge_base_id FROM runs WHERE id = :r"), {"r": run_id}
        ).one_or_none()


def test_delete_document_while_ingest_is_queued(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    run_id = _put(client, h, kb["id"]).json()["run_id"]
    url = f"{API}/knowledge-bases/{kb['id']}/documents/guide.md"
    assert client.delete(url, headers=h).status_code == 204
    assert client.get(url, headers=h).status_code == 404
    assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents", headers=h).json()["items"] == []
    # The run row survives, still queued (the worker ends it cancelled when it finds the document gone).
    row = _run_row(app, run_id)
    assert row is not None and row.status == "queued" and row.kind == "kb_ingest"
    assert str(row.knowledge_base_id) == kb["id"]
    assert client.get(f"{API}/runs/{run_id}", headers=h).json()["status"] == "queued"
    # The reingest of the deleted document is a 404, and a re-upload starts again at version 1.
    assert client.post(f"{url}/reingest", headers=h).status_code == 404
    again = _put(client, h, kb["id"])
    assert again.status_code == 202 and again.json()["document"]["latest_version"] == 1
    assert again.json()["run_id"] != run_id


def test_delete_knowledge_base_while_ingest_is_queued(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    run_id = _put(client, h, kb["id"]).json()["run_id"]
    assert client.delete(f"{API}/knowledge-bases/{kb['id']}", headers=h).status_code == 204
    for url in (f"{API}/knowledge-bases/{kb['id']}", f"{API}/knowledge-bases/{kb['id']}/documents"):
        assert client.get(url, headers=h).status_code == 404
    # The run stays and is readable; its knowledge base reference is whatever the schema keeps.
    got = client.get(f"{API}/runs/{run_id}", headers=h)
    assert got.status_code == 200 and got.json()["kind"] == "kb_ingest" and got.json()["status"] == "queued"
    assert got.json()["knowledge_base_id"] in (kb["id"], None)
    assert [r["run_id"] for r in _runs(client, h)] == [run_id]
    # Nothing can be started against the deleted knowledge base.
    n = len(fake_dbos.calls)
    assert _put(client, h, kb["id"], key="x.md").status_code == 404
    assert client.post(f"{API}/knowledge-bases/{kb['id']}/reindex", headers=h,
                       json={"embedding_model": "test:hash768"}).status_code == 404
    assert len(fake_dbos.calls) == n


# ---------------------------------------------------------------------------------- reindex


def test_reindex_conflict_dry_run_and_cancel_leave_consistent_state(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, h = tenant_of()
    kb = _create_kb(client, h)
    _put(client, h, kb["id"])
    url = f"{API}/knowledge-bases/{kb['id']}/reindex"
    kb_url = f"{API}/knowledge-bases/{kb['id']}"
    idle = client.get(kb_url, headers=h).json()
    runs, calls = len(_runs(client, h)), len(fake_dbos.calls)
    dry = client.post(url, json={"embedding_model": "test:hash768", "dry_run": True}, headers=h)
    assert dry.status_code == 200 and dry.json()["reindex_pending"] is False
    assert client.get(kb_url, headers=h).json() == idle
    assert len(_runs(client, h)) == runs and len(fake_dbos.calls) == calls

    rid = client.post(url, json={"embedding_model": "test:hash768"}, headers=h).json()["run_id"]
    pending = client.get(kb_url, headers=h).json()
    assert pending["status"] == "reindexing" and pending["pending_generation"] == 2
    runs, calls = len(_runs(client, h)), len(fake_dbos.calls)
    for model in ("test:hash", "test:hash768"):
        c = client.post(url, json={"embedding_model": model}, headers=h)
        assert c.status_code == 409, c.text
    assert client.get(kb_url, headers=h).json() == pending
    assert len(_runs(client, h)) == runs and len(fake_dbos.calls) == calls
    # A dry run while one is pending still answers, and says so; it starts nothing.
    d2 = client.post(url, json={"embedding_model": "test:hash", "dry_run": True}, headers=h)
    assert d2.status_code == 200 and d2.json()["reindex_pending"] is True
    assert len(fake_dbos.calls) == calls

    assert client.delete(url, headers=h).status_code == 204
    after = client.get(kb_url, headers=h).json()
    assert after["status"] == "ready" and after["pending_generation"] is None
    assert after["embedding_model"] == "test:hash" and after["dimensions"] == 1536
    assert _run_row(app, rid).kind == "kb_ingest"  # the run row is kept for the worker to cancel
    assert len(_runs(client, h)) == runs


def test_reindex_retry_after_failure_rules(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, h = tenant_of()
    kb = _create_kb(client, h)
    url = f"{API}/knowledge-bases/{kb['id']}/reindex"
    kb_url = f"{API}/knowledge-bases/{kb['id']}"
    first = client.post(url, json={"embedding_model": "test:hash768"}, headers=h).json()["run_id"]
    app.state.knowledge_store.mark_reindex_failed(tenant, kb["id"], 2, "embedding_failed")
    assert client.get(kb_url, headers=h).json()["status"] == "reindex_failed"
    # A different model does not retry a failed reindex of another one.
    assert client.post(url, json={"embedding_model": "test:hash"}, headers=h).status_code == 409
    rid = str(uuid.uuid4())
    retry = client.post(url, json={"embedding_model": "test:hash768", "run_id": rid}, headers=h)
    assert retry.status_code == 202 and retry.json()["run_id"] == rid != first
    assert retry.json()["knowledge_base"]["pending_generation"] == 2  # same generation, not a new one
    options, args = fake_dbos.calls[-1]
    assert options["workflow_id"] == rid and args == (rid, tenant, kb["id"], '{"embed_split":true,"reindex":2}')
    assert len({r["run_id"] for r in _runs(client, h)}) == 2
    # Cancel after the failed state also clears it.
    assert client.delete(url, headers=h).status_code == 204
    assert client.get(kb_url, headers=h).json()["pending_generation"] is None


# ------------------------------------------------------------------------------- 422 model policy


def test_disallowed_embedding_model_is_422_everywhere(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    for model in ("openai:text-embedding-3-small", "anthropic:x", "test", "litellm:emb-small=1024", ""):
        r = client.post(f"{API}/knowledge-bases", json={"name": "N", "embedding_model": model}, headers=h)
        assert r.status_code == 422, (model, r.status_code, r.text)
    assert client.get(f"{API}/knowledge-bases", headers=h).json()["items"] == []
    kb = _create_kb(client, h)
    url = f"{API}/knowledge-bases/{kb['id']}/reindex"
    before = client.get(f"{API}/knowledge-bases/{kb['id']}", headers=h).json()
    for body in ({"embedding_model": "openai:nope"}, {"embedding_model": "openai:nope", "dry_run": True}):
        assert client.post(url, json=body, headers=h).status_code == 422
    assert client.get(f"{API}/knowledge-bases/{kb['id']}", headers=h).json() == before
    assert fake_dbos.calls == [] and _runs(client, h) == []


# -------------------------------------------------------------------------- delete with references


def _agent(client, h, name, kb_ids):
    spec = {**SPEC, "name": name, "knowledge_bases": [{"id": k} for k in kb_ids]}
    r = client.post(f"{API}/agents", json={"spec": spec}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _publish(client, h, agent_id, kb_ids=None):
    if kb_ids is not None:
        spec = {**SPEC, "knowledge_bases": [{"id": k} for k in kb_ids]}
        assert client.patch(f"{API}/agents/{agent_id}", json={"spec": spec}, headers=h).status_code == 200
    r = client.post(f"{API}/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def test_delete_with_references_latest_version_only(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb1, kb2 = _create_kb(client, h, "One"), _create_kb(client, h, "Two")
    a1 = _agent(client, h, "alpha", [kb1["id"]])
    a2 = _agent(client, h, "beta", [kb1["id"]])
    _publish(client, h, a1)
    _publish(client, h, a2)
    blocked = client.delete(f"{API}/knowledge-bases/{kb1['id']}", headers=h)
    assert blocked.status_code == 409
    listed = {a["agent_id"]: a for a in blocked.json()["agents"]}
    assert set(listed) == {a1, a2} and listed[a1]["agent_name"] == "alpha" and listed[a1]["version"] == 1
    assert "alpha" in blocked.json()["detail"] and "beta" in blocked.json()["detail"]
    assert client.get(f"{API}/knowledge-bases/{kb1['id']}", headers=h).status_code == 200  # untouched

    # Agent alpha moves to kb2 in version 2: kb1 now has only beta, and kb2 is blocked by alpha.
    _publish(client, h, a1, [kb2["id"]])
    still = client.delete(f"{API}/knowledge-bases/{kb1['id']}", headers=h)
    assert still.status_code == 409 and [a["agent_id"] for a in still.json()["agents"]] == [a2]
    _publish(client, h, a2, [])
    assert client.delete(f"{API}/knowledge-bases/{kb1['id']}", headers=h).status_code == 204  # only v1s referenced it
    b2 = client.delete(f"{API}/knowledge-bases/{kb2['id']}", headers=h)
    assert b2.status_code == 409 and b2.json()["agents"][0]["version"] == 2
    assert client.delete(f"{API}/knowledge-bases/{kb2['id']}?force=true", headers=h).status_code == 204
    assert client.get(f"{API}/knowledge-bases", headers=h).json()["items"] == []


def test_force_false_and_unreferenced_delete(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    agent = _agent(client, h, "gamma", [kb["id"]])  # draft only: blocks too, marked as a draft
    blocked = client.delete(f"{API}/knowledge-bases/{kb['id']}?force=false", headers=h)
    assert blocked.status_code == 409 and [(a["agent_id"], a["draft"]) for a in blocked.json()["agents"]] == [(agent, True)]
    assert client.patch(f"{API}/agents/{agent}", json={"spec": SPEC}, headers=h).status_code == 200
    assert client.delete(f"{API}/knowledge-bases/{kb['id']}?force=false", headers=h).status_code == 204
    assert client.delete(f"{API}/knowledge-bases/{kb['id']}?force=true", headers=h).status_code == 404
    assert client.get(f"{API}/agents/{agent}", headers=h).status_code == 200


# ------------------------------------------------------------------------- embedding models, e2e


def test_embedding_models_reflect_the_allowlist(pg, tenant_of, monkeypatch):
    client, _ = pg
    _, h = tenant_of()
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "litellm:text-embedding-3-small=1536,litellm:nomic=768")
    items = client.get(f"{API}/embedding-models", headers=h).json()["items"]
    assert {i["id"]: i["dimensions"] for i in items} == {"litellm:text-embedding-3-small": 1536, "litellm:nomic": 768}
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "litellm:only=1024")
    items = client.get(f"{API}/embedding-models", headers=h).json()["items"]
    assert [(i["id"], i["dimensions"]) for i in items] == [("litellm:only", 1024)]
    # A model no longer on the list can no longer be used to create a knowledge base.
    r = client.post(f"{API}/knowledge-bases", json={"name": "k", "embedding_model": "litellm:nomic"}, headers=h)
    assert r.status_code == 422


def test_markdown_upload_creates_listed_kb_ingest_run(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _create_kb(client, h, "Handbook")
    url = f"{API}/knowledge-bases/{kb['id']}/documents/handbook.md"
    r = client.put(url, headers=h, json={"content": MD, "mime_type": "text/markdown", "title": "Handbook"})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    listed = client.get(f"{API}/runs?kind=kb_ingest", headers=h).json()["items"]
    assert [(i["run_id"], i["kind"], i["knowledge_base_id"], i["status"]) for i in listed] == [
        (run_id, "kb_ingest", kb["id"], "queued")
    ]
    assert client.get(f"{API}/runs?kind=agent", headers=h).json()["items"] == []
    doc = client.get(url, headers=h).json()
    assert doc["last_run_id"] == run_id and doc["mime_type"] == "text/markdown" and doc["status"] == "pending"
    assert client.get(f"{API}/knowledge-bases/{kb['id']}", headers=h).json()["document_count"] == 1
