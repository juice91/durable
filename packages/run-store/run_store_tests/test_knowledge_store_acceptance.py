"""RM.4 acceptance tests for KnowledgeStore: isolation of every method, generations, limits, atomicity."""

from __future__ import annotations

import os
import pickle
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eap_db.testing import SCHEMA_ENV
from eap_schemas import EmbeddingDimensionMismatch, KnowledgeBaseNotFound

import eap_run_store.knowledge as knowledge_module
from eap_run_store import (
    DocumentNotFound,
    GenerationNotWritable,
    KnowledgeBaseLimitExceeded,
    KnowledgeBaseNameConflict,
    KnowledgeStore,
    ReindexInProgress,
    RunStore,
)

from .test_knowledge_store import MODEL, MODEL_768, chunk, ingest, isolated_store, make_kb, search, vec  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


@pytest.fixture
def store(engine):
    return KnowledgeStore(engine)


@pytest.fixture
def tenant():
    return f"kba_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def other_tenant():
    return f"kba_o_{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant, other_tenant):
    yield
    with engine.begin() as conn:
        for t in (tenant, other_tenant):
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})


def _snapshot(engine, tenant):
    with engine.connect() as conn:
        out = {}
        for table, cols in (
            ("knowledge_bases", "id, name, description, status, active_generation, pending_generation, error"),
            ("kb_documents", "id, document_key, status, error, latest_version, active_version, last_run_id, content"),
            ("kb_chunks", "id, doc_version, generation, ordinal, (embedding IS NULL) AS pending"),
        ):
            out[table] = [tuple(r) for r in conn.execute(text(
                f"SELECT {cols} FROM {table} WHERE tenant_id = :t ORDER BY 1"), {"t": tenant})]
        return out


# ------------------------------------------------------------ tenant isolation, every method


def test_every_tenant_scoped_method_refuses_another_tenants_ids(store, engine, tenant, other_tenant):
    kb = make_kb(store, tenant)
    doc, v = ingest(store, tenant, kb, "secret", ["alpha", "beta"], hot=[1, 2], metadata={"lang": "en"})
    kid, did = kb["id"], doc["id"]
    store.start_reindex(tenant, kid, embedding_model=MODEL_768, dimensions=768)
    store.copy_generation_chunks(tenant, kid, 2)
    before = _snapshot(engine, tenant)
    b = other_tenant
    q = vec(1536, 1)

    # Reads: nothing.
    assert store.get_knowledge_base(b, kid) is None
    assert store.list_knowledge_bases(b) == []
    assert store.get_knowledge_bases(b, [kid]) == []
    assert store.existing_knowledge_base_ids(b, [kid]) == set()
    assert store.list_documents(b, kid) == []
    assert store.get_document(b, kid, "secret") is None
    assert store.get_document_by_id(b, did) is None
    assert store.get_ingest_source(b, did, v) is None
    assert search(store, b, [kid], q) == []
    assert search(store, b, [kid], q, metadata_filter={"lang": "en"}, min_score=0.0) == []
    assert store.explain_search(b, [kid], q, embedding_model=MODEL, top_k=3) == ""
    assert store.embedding_progress(b, kid, 1).total == 0
    assert store.embedding_progress(b, kid, 2, document_id=did, doc_version=v).total == 0
    assert store.next_unembedded_batch(b, kid, 2) == []
    assert store.next_unembedded_batch(b, kid, 1) == []

    # Writes: not-found errors or False, and tenant A's rows are unchanged.
    gone = (KnowledgeBaseNotFound, DocumentNotFound, GenerationNotWritable)
    for call in (
        lambda: store.update_knowledge_base(b, kid, name="hijacked"),
        lambda: store.put_document(b, kid, document_key="x", title="x", content="x", mime_type="text/plain"),
        lambda: store.request_reingest(b, kid, "secret"),
        lambda: store.upsert_chunks(b, did, v, 1, [chunk(5, "x")], metadata={}),
        lambda: store.apply_cached_embeddings(b, kid, 1),
        lambda: store.write_embeddings(b, kid, 1, {}, embedding_model=MODEL),
        lambda: store.write_embeddings(b, kid, 2, {str(uuid.uuid4()): vec(768, 1)}, embedding_model=MODEL_768),
        lambda: store.start_reindex(b, kid, embedding_model=MODEL_768, dimensions=768),
        lambda: store.copy_generation_chunks(b, kid, 2),
        lambda: store.estimate_reindex(b, kid),
    ):
        with pytest.raises(gone):
            call()
    assert store.delete_knowledge_base(b, kid) is False
    assert store.delete_document(b, kid, "secret") is False
    assert store.attach_ingest_run(b, did, str(uuid.uuid4())) is False
    assert store.mark_document_ingesting(b, did, v + 1, str(uuid.uuid4())) is False
    assert store.mark_document_failed(b, did, v, "boom") is False
    assert store.mark_reindex_failed(b, kid, 2, "boom") is False
    assert store.cancel_reindex(b, kid) is False
    assert store.finalize_document(b, did, v, 1, embedding_model=MODEL).status == "missing"
    assert store.finalize_reindex(b, kid, 2, embedding_model=MODEL_768).status == "missing"
    assert _snapshot(engine, tenant) == before

    # Tenant A still gets its own data, and B can reuse A's names and keys independently.
    assert [h.document_key for h in search(store, tenant, [kid], q)] == ["secret", "secret"]
    kb_b = make_kb(store, b)
    assert kb_b["id"] != kid
    assert search(store, b, [kb_b["id"], kid], q) == []


# ---------------------------------------------------------------- versions and generations


def _chunk_count(engine, tenant, where=""):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM kb_chunks WHERE tenant_id = :t {where}"), {"t": tenant}).scalar()


def test_a_failed_ingest_leaves_the_previous_version_searchable(store, tenant):
    kb = make_kb(store, tenant)
    doc, v1 = ingest(store, tenant, kb, "guide", ["stable text"], hot=[1])
    # Version 2 is uploaded, half chunked, and its ingest fails.
    put = store.put_document(tenant, kb["id"], document_key="guide", title="G", content="new", mime_type="text/plain")
    v2 = put.document["latest_version"]
    assert v2 == v1 + 1
    store.upsert_chunks(tenant, doc["id"], v2, 1, [chunk(0, "half"), chunk(1, "done")], metadata={})
    assert store.mark_document_ingesting(tenant, doc["id"], v2, str(uuid.uuid4()))
    assert store.mark_document_failed(tenant, doc["id"], v2, "embedding_failed: 0 of 2 sections embedded")
    got = store.get_document(tenant, kb["id"], "guide")
    assert got["status"] == "failed" and got["active_version"] == v1 and "embedding_failed" in got["error"]
    hits = search(store, tenant, [kb["id"]], vec(1536, 1))
    assert [(h.doc_version, h.content) for h in hits] == [(v1, "stable text")]
    # Un-embedded chunks of v2 never surface, even for a query that would match them.
    assert {h.doc_version for h in search(store, tenant, [kb["id"]], vec(1536, 2))} == {v1}
    # A stale failure report (older version) does not touch a newer upload.
    assert store.mark_document_failed(tenant, doc["id"], v1, "late") is False


def test_old_generation_serves_search_until_finalize_reindex(store, tenant, engine):
    kb = make_kb(store, tenant)
    ingest(store, tenant, kb, "a", ["one", "two"], hot=[1, 2])
    store.start_reindex(tenant, kb["id"], embedding_model=MODEL_768, dimensions=768)
    store.copy_generation_chunks(tenant, kb["id"], 2)
    batch = store.next_unembedded_batch(tenant, kb["id"], 2)
    # Fully embedded in the new generation, not finalised: the old generation still answers.
    store.write_embeddings(tenant, kb["id"], 2, {c.id: vec(768, c.ordinal + 1) for c in batch}, embedding_model=MODEL_768)
    old = search(store, tenant, [kb["id"]], vec(1536, 1))
    assert [(h.generation, h.ordinal) for h in old] == [(1, 0), (1, 1)]
    assert search(store, tenant, [kb["id"]], vec(768, 1), embedding_model=MODEL_768) == []
    assert _chunk_count(engine, tenant, "AND generation = 2") == 2
    assert store.finalize_reindex(tenant, kb["id"], 2, embedding_model=MODEL_768).status == "switched"
    assert search(store, tenant, [kb["id"]], vec(1536, 1)) == []
    new = search(store, tenant, [kb["id"]], vec(768, 1), embedding_model=MODEL_768)
    assert len(new) == 2 and {h.generation for h in new} == {2}
    assert _chunk_count(engine, tenant, "AND generation = 1") == 0


def test_only_the_active_or_pending_generation_is_writable(store, tenant):
    kb = make_kb(store, tenant)
    doc, v = ingest(store, tenant, kb, "a", ["one"], hot=[1])
    for gen in (0, 2, 9):  # 2 is not pending yet
        with pytest.raises(GenerationNotWritable):
            store.upsert_chunks(tenant, doc["id"], v, gen, [chunk(7, "x")], metadata={})
    store.start_reindex(tenant, kb["id"], embedding_model=MODEL_768, dimensions=768)
    assert store.upsert_chunks(tenant, doc["id"], v, 2, [chunk(7, "x")], metadata={}).inserted == 1
    store.cancel_reindex(tenant, kb["id"])
    with pytest.raises(GenerationNotWritable):
        store.upsert_chunks(tenant, doc["id"], v, 2, [chunk(7, "x")], metadata={})


# ------------------------------------------------------------------------ cascades (SQL)


def test_cascades_remove_chunks_in_every_generation(store, tenant, engine):
    kb = make_kb(store, tenant)
    ingest(store, tenant, kb, "a", ["one", "two"], hot=[1, 2])
    ingest(store, tenant, kb, "b", ["three"], hot=[3])
    store.start_reindex(tenant, kb["id"], embedding_model=MODEL_768, dimensions=768)
    assert store.copy_generation_chunks(tenant, kb["id"], 2) == 3
    assert _chunk_count(engine, tenant) == 6
    assert store.delete_document(tenant, kb["id"], "a") is True
    assert _chunk_count(engine, tenant) == 2  # both generations of "a" went
    assert store.get_document(tenant, kb["id"], "a") is None
    assert store.delete_document(tenant, kb["id"], "a") is False
    assert store.delete_knowledge_base(tenant, kb["id"]) is True
    assert _chunk_count(engine, tenant) == 0


def test_deleting_a_document_mid_ingest_is_reported_not_swallowed(store, tenant):
    kb = make_kb(store, tenant)
    put = store.put_document(tenant, kb["id"], document_key="d", title="D", content="c", mime_type="text/plain")
    did = put.document["id"]
    store.upsert_chunks(tenant, did, 1, 1, [chunk(0, "x")], metadata={})
    store.delete_document(tenant, kb["id"], "d")
    with pytest.raises(DocumentNotFound):
        store.upsert_chunks(tenant, did, 1, 1, [chunk(0, "x")], metadata={})
    assert store.finalize_document(tenant, did, 1, 1, embedding_model=MODEL).status == "missing"
    store.delete_knowledge_base(tenant, kb["id"])
    with pytest.raises(KnowledgeBaseNotFound):
        store.write_embeddings(tenant, kb["id"], 1, {}, embedding_model=MODEL)
    assert store.get_ingest_source(tenant, did, 1) is None


# ------------------------------------------------------------------- upserts and the cache


def test_upsert_twice_gives_identical_rows(store, tenant, engine):
    kb = make_kb(store, tenant)
    put = store.put_document(tenant, kb["id"], document_key="d", title="D", content="c", mime_type="text/plain")
    chunks = [chunk(i, f"body {i}") for i in range(4)]
    store.upsert_chunks(tenant, put.document["id"], 1, 1, chunks, metadata={})
    sql = "SELECT id, ordinal, content, embed_text_hash, created_at FROM kb_chunks WHERE tenant_id = :t ORDER BY ordinal"
    with engine.connect() as conn:
        first = conn.execute(text(sql), {"t": tenant}).all()
    again = store.upsert_chunks(tenant, put.document["id"], 1, 1, chunks, metadata={})
    with engine.connect() as conn:
        second = conn.execute(text(sql), {"t": tenant}).all()
    assert (again.total, again.inserted) == (4, 0)
    assert first == second and len(first) == 4
    # A partial overlap inserts only the new ordinals.
    more = store.upsert_chunks(tenant, put.document["id"], 1, 1, chunks + [chunk(4, "body 4")], metadata={})
    assert (more.total, more.inserted) == (5, 1)


def test_cache_is_scoped_to_tenant_and_reuses_unchanged_sections_across_versions(store, tenant, other_tenant):
    kb = make_kb(store, tenant)
    doc, v1 = ingest(store, tenant, kb, "d", ["same section", "old section"], hot=[1, 2])
    put = store.put_document(tenant, kb["id"], document_key="d", title="D", content="edited", mime_type="text/plain")
    v2 = put.document["latest_version"]
    store.upsert_chunks(tenant, doc["id"], v2, 1, [chunk(0, "same section", "S0"), chunk(1, "new section", "S1")], metadata={})
    assert store.apply_cached_embeddings(tenant, kb["id"], 1, document_id=doc["id"], doc_version=v2) == 1
    [left] = store.next_unembedded_batch(tenant, kb["id"], 1)
    assert (left.doc_version, left.ordinal) == (v2, 1)
    # Same text, same model, same dimensions, but another tenant's knowledge base: never reused.
    kb_b = make_kb(store, other_tenant)
    pb = store.put_document(other_tenant, kb_b["id"], document_key="d", title="D", content="c", mime_type="text/plain")
    store.upsert_chunks(other_tenant, pb.document["id"], 1, 1, [chunk(0, "same section", "S0")], metadata={})
    assert store.apply_cached_embeddings(other_tenant, kb_b["id"], 1) == 0
    with pytest.raises(KnowledgeBaseNotFound):
        store.apply_cached_embeddings(other_tenant, kb["id"], 1)


# ------------------------------------------------------------------------------- limits


def test_document_and_chunk_caps(store, tenant, monkeypatch):
    assert knowledge_module.MAX_DOCUMENTS_PER_KNOWLEDGE_BASE == 2000
    assert knowledge_module.MAX_CHUNKS_PER_KNOWLEDGE_BASE == 200_000
    monkeypatch.setattr(knowledge_module, "MAX_DOCUMENTS_PER_KNOWLEDGE_BASE", 2)
    monkeypatch.setattr(knowledge_module, "MAX_CHUNKS_PER_KNOWLEDGE_BASE", 3)
    kb = make_kb(store, tenant)
    docs = []
    for key in ("a", "b"):
        docs.append(store.put_document(tenant, kb["id"], document_key=key, title=key, content=key, mime_type="text/plain"))
    with pytest.raises(KnowledgeBaseLimitExceeded) as err:
        store.put_document(tenant, kb["id"], document_key="c", title="c", content="c", mime_type="text/plain")
    assert (err.value.what, err.value.limit) == ("documents", 2)
    # Updating an existing document is not a new document.
    assert store.put_document(tenant, kb["id"], document_key="a", title="a", content="a2", mime_type="text/plain")
    a, b = docs[0].document["id"], docs[1].document["id"]
    store.upsert_chunks(tenant, a, 1, 1, [chunk(0, "x"), chunk(1, "y")], metadata={})
    with pytest.raises(KnowledgeBaseLimitExceeded) as err:
        store.upsert_chunks(tenant, b, 1, 1, [chunk(0, "p"), chunk(1, "q")], metadata={})
    assert (err.value.what, err.value.limit) == ("chunks", 3)
    assert store.embedding_progress(tenant, kb["id"], 1).total == 2  # rolled back, nothing partial
    assert store.upsert_chunks(tenant, b, 1, 1, [chunk(0, "p")], metadata={}).inserted == 1


def test_name_conflicts_and_one_pending_reindex(store, tenant):
    one, two = make_kb(store, tenant, name="One"), make_kb(store, tenant, name="Two")
    with pytest.raises(KnowledgeBaseNameConflict) as err:
        store.update_knowledge_base(tenant, two["id"], name="One")
    assert err.value.name == "One"
    assert store.update_knowledge_base(tenant, one["id"], name="One").get("name") == "One"  # own name is fine
    assert store.get_knowledge_base(tenant, two["id"])["name"] == "Two"
    first = store.start_reindex(tenant, one["id"], embedding_model=MODEL_768, dimensions=768)
    with pytest.raises(ReindexInProgress) as rerr:
        store.start_reindex(tenant, one["id"], embedding_model="test:hash1024", dimensions=1024)
    assert rerr.value.pending_generation == first["pending_generation"] == 2
    # Another knowledge base is unaffected; after a cancel the next reindex gets a new number (never reused).
    store.start_reindex(tenant, two["id"], embedding_model=MODEL_768, dimensions=768)
    store.cancel_reindex(tenant, one["id"])
    assert store.start_reindex(tenant, one["id"], embedding_model="test:hash1024", dimensions=1024)[
        "pending_generation"
    ] == 3


# ------------------------------------------------- write_embeddings atomicity and dimensions


def _usage(key="embed:batch:0", tokens=7):
    return {
        "request_key": key, "model_id": MODEL, "provider_model_name": "hash", "input_tokens": tokens,
        "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None, "price_source": None,
    }


def _counts(engine, run_id):
    with engine.connect() as conn:
        ev = conn.execute(text("SELECT count(*) FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}).scalar()
        us = conn.execute(text("SELECT count(*) FROM run_model_requests WHERE run_id = :r"), {"r": str(run_id)}).scalar()
    return ev, us


def _unembed(engine, tenant):
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_chunks SET embedding = NULL, dimensions = NULL WHERE tenant_id = :t"), {"t": tenant})


@pytest.fixture
def pending(store, engine, tenant):
    kb = make_kb(store, tenant)
    ingest(store, tenant, kb, "d", ["one", "two"], hot=[1, 2], finalize=False)
    _unembed(engine, tenant)
    run_id = uuid.uuid4()
    RunStore(engine).create_kb_ingest_run(tenant, kb["id"], "{}", run_id)
    return kb, str(run_id), [c.id for c in store.next_unembedded_batch(tenant, kb["id"], 1)]


def test_write_embeddings_rolls_back_vectors_when_the_usage_row_fails(store, engine, tenant, pending):
    kb, run_id, ids = pending
    assert len(ids) == 2
    bad_usage = {"request_key": "embed:batch:0", "model_id": MODEL}  # incomplete: the usage insert fails
    with pytest.raises(Exception):
        store.write_embeddings(
            tenant, kb["id"], 1, {i: vec(1536, 1) for i in ids}, run_id=run_id,
            event_key="embed:batch:0", event_data={"embedded": 2, "total": 2}, usage=bad_usage, embedding_model=MODEL)
    assert store.embedding_progress(tenant, kb["id"], 1).embedded == 0
    assert _counts(engine, run_id) == (0, 0)
    # An unknown run (or another tenant's) fails the whole batch too.
    with pytest.raises(Exception):
        store.write_embeddings(
            tenant, kb["id"], 1, {i: vec(1536, 1) for i in ids}, run_id=str(uuid.uuid4()),
            event_key="embed:batch:0", usage=_usage(), embedding_model=MODEL)
    assert store.embedding_progress(tenant, kb["id"], 1).embedded == 0


def test_write_embeddings_commits_everything_and_replay_never_double_counts(store, engine, tenant, pending):
    kb, run_id, ids = pending
    kwargs = dict(run_id=run_id, event_key="embed:batch:0", event_data={"embedded": 2, "total": 2}, usage=_usage(),
                  embedding_model=MODEL)
    first = store.write_embeddings(tenant, kb["id"], 1, {i: vec(1536, 1) for i in ids}, **kwargs)
    assert (first.written, first.event_created) == (2, True)
    assert store.embedding_progress(tenant, kb["id"], 1).embedded == 2 and _counts(engine, run_id) == (1, 1)
    for _ in range(3):
        replay = store.write_embeddings(tenant, kb["id"], 1, {i: vec(1536, 1) for i in ids}, **kwargs)
        assert (replay.written, replay.event_created) == (0, False)
    assert _counts(engine, run_id) == (1, 1)
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT sum(input_tokens), min(operation), count(*) FROM run_model_requests WHERE run_id = :r"
        ), {"r": run_id}).one() == (7, "embedding", 1)
    # A second batch key is a second usage row.
    store.write_embeddings(tenant, kb["id"], 1, {}, run_id=run_id, event_key="embed:batch:1",
                           event_data={"embedded": 2, "total": 2}, usage=_usage("embed:batch:1", 3), embedding_model=MODEL)
    assert _counts(engine, run_id) == (2, 2)
    with pytest.raises(ValueError):
        store.write_embeddings(tenant, kb["id"], 1, {}, run_id=run_id, usage=_usage("k"), embedding_model=MODEL)  # usage needs an event key


def test_dimension_guard_in_python_and_in_the_database(store, engine, tenant, pending):
    kb, run_id, ids = pending
    for bad in (1, 767, 1535, 1537):
        with pytest.raises(EmbeddingDimensionMismatch) as err:
            store.write_embeddings(tenant, kb["id"], 1, {ids[0]: [0.1] * bad}, embedding_model=MODEL)
        assert (err.value.expected, err.value.actual) == (1536, bad)
    # One bad vector in a batch of good ones writes none of them.
    with pytest.raises(EmbeddingDimensionMismatch):
        store.write_embeddings(tenant, kb["id"], 1, {ids[0]: vec(1536, 1), ids[1]: vec(768, 1)}, run_id=run_id,
                               event_key="k", usage=_usage(), embedding_model=MODEL)
    assert store.embedding_progress(tenant, kb["id"], 1).embedded == 0 and _counts(engine, run_id) == (0, 0)
    # The check constraint rejects: wrong length, an unsupported size, and (own test below) a vector without dimensions.
    for dims, length in ((1536, 768), (512, 512)):
        with pytest.raises(IntegrityError, match="ck_kb_chunks_embedding"):
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE kb_chunks SET embedding = CAST(:e AS vector), dimensions = :d WHERE id = :id"
                ), {"e": "[" + ",".join(["0.1"] * length) + "]", "d": dims, "id": ids[0]})


def test_db_check_rejects_an_embedding_without_dimensions(engine, store, tenant, pending):
    """IMPLEMENTATION BUG if red: ck_kb_chunks_embedding evaluates to NULL (which a CHECK accepts) when
    embedding IS NOT NULL and dimensions IS NULL, so such a row escapes both the check and the
    partial HNSW indexes."""
    _, _, ids = pending
    with pytest.raises(IntegrityError, match="ck_kb_chunks_embedding"):
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE kb_chunks SET embedding = CAST(:e AS vector), dimensions = NULL WHERE id = :id"
            ), {"e": "[" + ",".join(["0.1"] * 1536) + "]", "id": ids[0]})


# ----------------------------------------------------------------- search and the index


def test_top_k_is_ordered_by_exact_distance_and_scores_match(store, tenant):
    kb = make_kb(store, tenant)
    # Chunk i leans toward axis 1 by decreasing amounts: distances are strictly increasing with i.
    bodies = [f"body {i}" for i in range(6)]
    hots = [(1,), (1, 2), (1, 2, 3), (1, 2, 3, 4), (1, 2, 3, 4, 5), (2,)]
    put = store.put_document(tenant, kb["id"], document_key="d", title="D", content="x", mime_type="text/plain")
    store.upsert_chunks(tenant, put.document["id"], 1, 1, [chunk(i, b, f"S{i}") for i, b in enumerate(bodies)], metadata={})
    batch = {c.ordinal: c.id for c in store.next_unembedded_batch(tenant, kb["id"], 1)}
    store.write_embeddings(tenant, kb["id"], 1, {batch[i]: vec(1536, *h) for i, h in enumerate(hots)}, embedding_model=MODEL)
    assert store.finalize_document(tenant, put.document["id"], 1, 1, embedding_model=MODEL).status == "switched"
    q = vec(1536, 1)
    hits = search(store, tenant, [kb["id"]], q, top_k=6)
    assert [h.ordinal for h in hits] == [0, 1, 2, 3, 4, 5]
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True) and scores[0] == pytest.approx(1.0) and scores[-1] == pytest.approx(0.0)
    assert scores[1] == pytest.approx(2**-0.5)
    assert [h.ordinal for h in search(store, tenant, [kb["id"]], q, top_k=3)] == [0, 1, 2]
    cut = search(store, tenant, [kb["id"]], q, top_k=6, min_score=0.5)
    assert [h.ordinal for h in cut] == [0, 1, 2, 3]  # 1, .707, .577, .5
    assert all(h.score >= 0.5 - 1e-9 for h in cut)
    for bad in (0, 201):
        with pytest.raises(ValueError):
            search(store, tenant, [kb["id"]], q, top_k=bad)
    with pytest.raises(ValueError):
        search(store, tenant, [kb["id"]], q, min_score=1.5)
    assert search(store, tenant, [], q) == []


def test_index_is_used_with_filters_and_several_knowledge_bases(isolated_store, tenant):
    store = isolated_store  # a throwaway schema: the plan must not depend on other tests' rows
    for dims, model in ((768, MODEL_768), (1024, "test:hash1024"), (1536, MODEL)):
        a = make_kb(store, tenant, name=f"a{dims}", model=model, dims=dims)
        b = make_kb(store, tenant, name=f"b{dims}", model=model, dims=dims)
        ingest(store, tenant, a, "x", ["one"], hot=[1], dims=dims, metadata={"lang": "en"})
        ingest(store, tenant, b, "y", ["two"], hot=[2], dims=dims, metadata={"lang": "de"})
        plan = store.explain_search(
            tenant, [a["id"], b["id"]], vec(dims, 1), embedding_model=model, top_k=5,
            metadata_filter={"lang": ["en", "de"]}, min_score=0.0,
        )
        assert f"ix_kb_chunks_embedding_{dims}" in plan, plan
        hits = search(store, tenant, [a["id"], b["id"]], vec(dims, 1), embedding_model=model)
        assert [h.document_key for h in hits] == ["x", "y"]
        # A query of one dimension never reads another dimension's rows.
        assert search(store, tenant, [a["id"]], vec(dims, 1), embedding_model="test:other") == []


def test_every_new_error_is_pickle_safe_and_keeps_its_fields():
    errors = (
        KnowledgeBaseNameConflict("t", "n"),
        KnowledgeBaseLimitExceeded("kb", "documents", 2000),
        DocumentNotFound("d"),
        ReindexInProgress("k", 2),
        GenerationNotWritable("k", 3),
    )
    for err in errors:
        back = pickle.loads(pickle.dumps(err))
        assert type(back) is type(err) and str(back) == str(err) and back.args == err.args
        assert back.code == err.code and back.__dict__ == err.__dict__
    assert pickle.loads(pickle.dumps(KnowledgeBaseLimitExceeded("kb", "chunks", 9))).limit == 9
    assert pickle.loads(pickle.dumps(ReindexInProgress("k", 4))).pending_generation == 4
    assert pickle.loads(pickle.dumps(KnowledgeBaseNotFound("x"))).args == ("x",)
    assert pickle.loads(pickle.dumps(EmbeddingDimensionMismatch("m", 1536, 768))).actual == 768
