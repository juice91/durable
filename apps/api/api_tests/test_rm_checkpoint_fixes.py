"""RAG milestone checkpoint review fixes (2026-11-14), API side: dot-only document keys and no
trailing-slash redirects, the `eap-kb-ingest` queue, unavailable memory features refused at publish,
restore and draft runs, paged document lists, the `knowledge_base_id` run filter and a knowledge
base's ingest spend."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from eap_api.launcher import KB_INGEST_QUEUE_NAME, QUEUE_NAME
from eap_api.routes.knowledge import decode_document_cursor, encode_document_cursor
from eap_api.schemas import document_key_problem

from .run_helpers import requires_pg
from .test_knowledge_api import API, MD, _create_kb, _knowledge_env, _put, kb_cleanup  # noqa: F401 - fixtures
from .test_runs import SPEC, deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401 - fixtures

OBSERVATIONAL = {"observational": {"model": {"provider": "test", "name": "observer"}}}
SEMANTIC_RECALL = {"semantic_recall": {"embedding_model": "test:hash"}}


# ------------------------------------------------------------------------------ document keys


@pytest.mark.parametrize("key", [".", "..", "...", "a/b", "a\\b", "..\\..", " a", "a\n"])
def test_path_like_document_keys_are_refused(key):
    assert document_key_problem(key) is not None


@pytest.mark.parametrize("key", ["guide.md", ".env", "..hidden", "a..b", "v1.2.", "é.md"])
def test_ordinary_document_keys_are_accepted(key):
    assert document_key_problem(key) is None


@requires_pg
def test_dot_keys_never_reach_the_knowledge_base(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    base = f"{API}/knowledge-bases/{kb['id']}"
    body = {"content": MD, "mime_type": "text/markdown"}
    # Encoded so the client does not normalize them away: the route sees the literal key.
    for raw in ("%2E", "%2E%2E", "%2E%2E%2E", "a%5Cb"):
        r = client.put(f"{base}/documents/{raw}", json=body, headers=h)
        assert r.status_code == 422, (raw, r.status_code, r.text)
    assert "only dots" in client.put(f"{base}/documents/%2E%2E", json=body, headers=h).json()["detail"]
    assert fake_dbos.calls == []

    # The attack: a browser that builds DELETE …/documents/.. (fetch with encodeURIComponent, which
    # leaves dots alone) sends DELETE /knowledge-bases/{id}/ after removing the dot segments. No
    # 307 to the knowledge base (fetch would follow it with DELETE): 404, and it is still there.
    # (httpx strips the trailing slash as well, so a Python client would reach the knowledge base
    # route itself: the server cannot tell that apart, which is why such keys cannot be stored.)
    r = client.delete(f"{base}/", headers=h, follow_redirects=False)
    assert r.status_code == 404 and "location" not in r.headers, (r.status_code, r.headers)
    assert client.get(base, headers=h).status_code == 200
    # Other trailing-slash paths are not redirected either.
    assert client.get(f"{API}/knowledge-bases/", headers=h, follow_redirects=False).status_code == 404


# ---------------------------------------------------------------------------------- the queue


@requires_pg
def test_kb_ingest_is_enqueued_on_its_own_queue(pg, tenant_of, fake_dbos, kb_cleanup):
    assert KB_INGEST_QUEUE_NAME == "eap-kb-ingest" and QUEUE_NAME == "eap-agent-runs"
    client, _ = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    run_id = str(uuid.uuid4())
    assert _put(client, h, kb["id"], run_id=run_id).status_code == 202
    (options, _args), = fake_dbos.calls
    assert options["queue_name"] == "eap-kb-ingest" and options["workflow_id"] == run_id
    assert options["workflow_name"] == "eap.kb_ingest"


# ------------------------------------------------------------------- unavailable memory features


def _agent_with_draft(client, h, memory=None):
    spec = {**SPEC, **({"memory": memory} if memory else {})}
    r = client.post(f"{API}/agents", json={"spec": spec}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@requires_pg
@pytest.mark.parametrize(
    ("memory", "message"),
    [
        (SEMANTIC_RECALL, "semantic recall is not available yet"),
    ],
)
def test_unavailable_memory_features_are_refused(pg, tenant_of, fake_dbos, memory, message):
    client, app = pg
    tenant, h = tenant_of()

    # Publish.
    agent_id = _agent_with_draft(client, h, memory)
    r = client.post(f"{API}/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 422 and r.json()["detail"].startswith(message), r.text

    # Draft run (the shared validate path).
    r = client.post(
        f"{API}/agents/{agent_id}/runs",
        json={"run_id": str(uuid.uuid4()), "input": "hi", "version": "draft"},
        headers=h,
    )
    assert r.status_code == 422 and r.json()["detail"].startswith(message), r.text
    assert fake_dbos.calls == []

    # Restore of a version stored before the check existed.
    plain = _agent_with_draft(client, h)
    assert client.post(f"{API}/agents/{plain}/versions", headers=h).status_code == 201
    stored = {**SPEC, "instructions": "v1 with memory", "memory": memory}
    with app.state.database_engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_versions SET spec = CAST(:s AS jsonb) WHERE agent_id = :a AND version = 1"),
            {"s": json.dumps(stored), "a": plain},
        )
    r = client.patch(f"{API}/agents/{plain}", json={"spec": {**SPEC, "instructions": "v2"}}, headers=h)
    assert r.status_code == 200, r.text
    assert client.post(f"{API}/agents/{plain}/versions", headers=h).status_code == 201
    r = client.post(f"{API}/agents/{plain}/versions/1/restore", json={"discard_draft": True}, headers=h)
    assert r.status_code == 422 and r.json()["detail"].startswith(message), r.text
    del tenant


# ------------------------------------------------------------------------ document pagination


def test_document_cursor_round_trip_and_rejects_foreign_tokens():
    for key in ("guide.md", "é ü.md", "a" * 256):
        assert decode_document_cursor(encode_document_cursor(key)) == key
    from eap_api.routes.knowledge import InvalidDocumentCursor

    for bad in ("", "!!!", "e30", "eyJrIjoxfQ", "eyJrIjoiIn0"):  # "", garbage, {}, {"k":1}, {"k":""}
        with pytest.raises(InvalidDocumentCursor):
            decode_document_cursor(bad)


@requires_pg
def test_documents_are_paged_by_key(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _create_kb(client, h)
    keys = [f"doc-{i:02d}.md" for i in range(5)]
    for key in reversed(keys):
        assert _put(client, h, kb["id"], key=key, content=f"# {key}\n\nBody of {key}.\n").status_code == 202
    url = f"{API}/knowledge-bases/{kb['id']}/documents"

    seen, after, pages = [], None, 0
    while True:
        params = {"limit": 2, **({"after": after} if after else {})}
        page = client.get(url, params=params, headers=h).json()
        seen += [d["document_key"] for d in page["items"]]
        pages += 1
        after = page["next_after"]
        if after is None:
            break
    assert seen == keys and pages == 3

    full = client.get(url, params={"limit": 5}, headers=h).json()
    assert len(full["items"]) == 5 and full["next_after"] is not None  # a full page always offers more
    assert client.get(url, params={"limit": 5, "after": full["next_after"]}, headers=h).json() == {
        "items": [],
        "next_after": None,
    }
    default = client.get(url, headers=h).json()
    assert [d["document_key"] for d in default["items"]] == keys and default["next_after"] is None

    bad = client.get(url, params={"after": "not-a-cursor"}, headers=h)
    assert bad.status_code == 422 and "next_after" in bad.json()["detail"]
    assert client.get(url, params={"limit": 0}, headers=h).status_code == 422
    assert client.get(url, params={"limit": 201}, headers=h).status_code == 422

    # Tenant scope: another tenant's cursor or knowledge base finds nothing.
    _, other = tenant_of()
    assert client.get(url, headers=other).status_code == 404


# ------------------------------------------------------------------------- ingest runs and cost


def _usage(conn, tenant, run_id, key, inp, cost):
    conn.execute(
        text(
            "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at, operation)"
            " VALUES (:t, :r, :k, 'test:hash', :i, 0, 0, 0, true, :c, :src, now(), 'embedding')"
        ),
        {"t": tenant, "r": run_id, "k": key, "i": inp, "c": cost, "src": "override" if cost is not None else None},
    )


@requires_pg
def test_runs_filter_by_knowledge_base_and_ingest_totals(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    tenant, h = tenant_of()
    kb = _create_kb(client, h)
    other_kb = _create_kb(client, h, name="Other")

    fresh = client.get(f"{API}/knowledge-bases/{kb['id']}", headers=h).json()
    assert (fresh["ingest_tokens"], fresh["ingest_cost_usd"], fresh["ingest_requests"]) == (0, "0.0000000000", 0)

    first, second, elsewhere = (str(uuid.uuid4()) for _ in range(3))
    assert _put(client, h, kb["id"], key="a.md", run_id=first).status_code == 202
    assert _put(client, h, kb["id"], key="b.md", run_id=second).status_code == 202
    assert _put(client, h, other_kb["id"], key="a.md", run_id=elsewhere).status_code == 202

    listed = client.get(f"{API}/runs", params={"knowledge_base_id": kb["id"]}, headers=h).json()["items"]
    assert sorted(r["run_id"] for r in listed) == sorted([first, second])
    assert all(r["knowledge_base_id"] == kb["id"] for r in listed)
    both = client.get(f"{API}/runs", params={"knowledge_base_id": kb["id"], "kind": "kb_ingest"}, headers=h)
    assert len(both.json()["items"]) == 2
    assert client.get(f"{API}/runs", params={"knowledge_base_id": "nope"}, headers=h).status_code == 422

    with app.state.database_engine.begin() as conn:
        _usage(conn, tenant, first, "embed:0", 1000, "0.0001")
        _usage(conn, tenant, second, "embed:0", 250, "0.000025")
        _usage(conn, tenant, elsewhere, "embed:0", 99_999, "9")
    totals = client.get(f"{API}/knowledge-bases/{kb['id']}", headers=h).json()
    assert totals["ingest_tokens"] == 1250 and totals["ingest_requests"] == 2
    assert totals["ingest_cost_usd"] == "0.0001250000"
    # The list and the other routes keep the plain shape.
    listed_kb = client.get(f"{API}/knowledge-bases", headers=h).json()["items"][0]
    assert "ingest_tokens" not in listed_kb

    with app.state.database_engine.begin() as conn:
        _usage(conn, tenant, second, "embed:1", 10, None)  # one unpriced request: the cost is unknown
    totals = client.get(f"{API}/knowledge-bases/{kb['id']}", headers=h).json()
    assert totals["ingest_tokens"] == 1260 and totals["ingest_cost_usd"] is None

    # Tenant scope: another tenant sees neither the knowledge base nor its runs.
    _, other = tenant_of()
    assert client.get(f"{API}/knowledge-bases/{kb['id']}", headers=other).status_code == 404
    assert client.get(f"{API}/runs", params={"knowledge_base_id": kb["id"]}, headers=other).json()["items"] == []
