"""KnowledgeStore.search: a small knowledge base in a large shared HNSW index still fills top_k
(the exact-search fallback, RM checkpoint review)."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import event, text

from eap_db.testing import SCHEMA_ENV
from eap_run_store import knowledge as knowledge_module

from .test_knowledge_store import chunk, isolated_store, make_kb, vec  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

DIMS = 768
MODEL = "test:hash768"
FOREIGN_ROWS = 3000


def _ingest_many(store, tenant, kb, key, vectors):
    put = store.put_document(
        tenant, kb["id"], document_key=key, title=key, content="x", mime_type="text/markdown",
        source_url="https://example.test/doc",
    )
    doc, version = put.document, put.document["latest_version"]
    gen = store.get_knowledge_base(tenant, kb["id"])["active_generation"]
    store.upsert_chunks(tenant, doc["id"], version, gen, [chunk(i, f"body {key} {i}", f"S{i}") for i in range(len(vectors))], metadata={})
    while True:
        batch = store.next_unembedded_batch(tenant, kb["id"], gen, document_id=doc["id"], doc_version=version)
        if not batch:
            break
        store.write_embeddings(tenant, kb["id"], gen, {c.id: vectors[c.ordinal] for c in batch}, embedding_model=MODEL)
    assert store.finalize_document(tenant, doc["id"], version, gen, embedding_model=MODEL).status == "switched"


def _no_sort(dbapi_connection, _record):
    with dbapi_connection.cursor() as cur:
        cur.execute("SET enable_sort = off")
    dbapi_connection.commit()


@pytest.fixture
def crowded(isolated_store):
    """A foreign tenant with FOREIGN_ROWS chunks all nearer to the query than any of ours, and our
    small knowledge base (8 chunks), in one throwaway schema (so the plan does not depend on other
    tests' rows)."""
    store = isolated_store
    ours, foreign = f"kb_t_{uuid.uuid4().hex[:8]}", f"kb_f_{uuid.uuid4().hex[:8]}"
    fkb = make_kb(store, foreign, name="big", model=MODEL, dims=DIMS)
    _ingest_many(store, foreign, fkb, "big", [vec(DIMS, 1, 2 + (i % 500), noise=0.0) for i in range(FOREIGN_ROWS)])
    kb = make_kb(store, ours, name="small", model=MODEL, dims=DIMS)
    _ingest_many(store, ours, kb, "small", [vec(DIMS, 600 + i, 700 + i) for i in range(8)])
    with store.engine.begin() as conn:
        conn.execute(text("ANALYZE kb_chunks"))
    # On a table this small the planner would rather read the kb's few rows by a btree and sort
    # them (exact anyway). A large shared table makes it walk the HNSW index instead; penalising
    # explicit sorts (this throwaway engine only) gives that plan here.
    store.engine.dispose()
    event.listen(store.engine, "connect", _no_sort)
    plan = store.explain_search(ours, [kb["id"]], vec(DIMS, 1), embedding_model=MODEL, top_k=5, disable_seqscan=False)
    assert f"ix_kb_chunks_embedding_{DIMS}" in plan, plan  # precondition: the ANN query walks the shared index
    return store, ours, kb


def test_small_kb_fills_top_k_in_a_crowded_index(crowded, monkeypatch):
    store, tenant, kb = crowded
    # A low scan cap stands in for a shared index far larger than this test can build.
    monkeypatch.setattr(knowledge_module, "HNSW_MAX_SCAN_TUPLES", 200)
    hits = store.search(tenant, [kb["id"]], vec(DIMS, 1), embedding_model=MODEL, top_k=5)
    assert len(hits) == 5
    assert all(h.kb_id == kb["id"] for h in hits)
    assert [h.distance for h in hits] == sorted(h.distance for h in hits)


def test_no_fallback_above_the_exact_search_limit(crowded, monkeypatch):
    store, tenant, kb = crowded
    monkeypatch.setattr(knowledge_module, "HNSW_MAX_SCAN_TUPLES", 200)
    monkeypatch.setenv("EAP_KB_EXACT_SEARCH_MAX_CHUNKS", "4")  # our 8 chunks count as "large"
    hits = store.search(tenant, [kb["id"]], vec(DIMS, 1), embedding_model=MODEL, top_k=5)
    assert len(hits) < 5  # the ANN result as is: the fallback is only for small knowledge bases


def test_exact_search_does_not_use_the_hnsw_index(crowded):
    store, tenant, kb = crowded
    plan = store.explain_search(tenant, [kb["id"]], vec(DIMS, 1), embedding_model=MODEL, top_k=5, exact=True)
    assert f"ix_kb_chunks_embedding_{DIMS}" not in plan, plan
