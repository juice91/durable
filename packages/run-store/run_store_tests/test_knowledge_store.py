"""KnowledgeStore (RM.4) against real pgvector in the session's migrated schema."""

from __future__ import annotations

import hashlib
import math
import os
import pickle
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eap_db.testing import SCHEMA_ENV
from eap_schemas import EmbeddingDimensionMismatch, KnowledgeBaseNotFound

from eap_run_store import (
    ChunkInput,
    DocumentNotFound,
    GenerationNotWritable,
    KnowledgeBaseNameConflict,
    KnowledgeStore,
    ReindexInProgress,
    RunStore,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

MODEL = "test:hash"
MODEL_768 = "test:hash768"


def vec(dims: int, *hot: int, noise: float = 0.0) -> list[float]:
    """Unit vector with weight on the `hot` axes (texts that share axes rank closer)."""
    v = [noise] * dims
    for h in hot:
        v[h % dims] += 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def chunk(ordinal: int, body: str, title: str = "Section") -> ChunkInput:
    embed = f"path: Doc > {title}\n\ntitle: {title}\n\n{body}"
    return ChunkInput(
        ordinal=ordinal,
        title=title,
        text=body,
        embed_text=embed,
        embed_text_hash=hashlib.sha256(embed.encode()).hexdigest(),
        approx_tokens=max(1, math.ceil(len(embed) / 4)),
        heading_path=("Doc", title),
        anchor=f"https://example.test/doc#{title.lower()}",
    )


@pytest.fixture
def store(engine):
    return KnowledgeStore(engine)


@pytest.fixture
def tenant():
    return f"kb_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def other_tenant():
    return f"kb_o_{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant, other_tenant):
    yield
    with engine.begin() as conn:
        for t in (tenant, other_tenant):
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})


def make_kb(store, tenant, name="Docs", model=MODEL, dims=1536):
    return store.create_knowledge_base(tenant, name=name, embedding_model=model, dimensions=dims, description="d")


def ingest(store, tenant, kb, key, bodies, *, hot, dims=1536, metadata=None, finalize=True):
    """Upload, chunk, embed (chunk i -> vec(hot[i])) and finalise one document version."""
    put = store.put_document(
        tenant, kb["id"], document_key=key, title=key.title(), content="\n\n".join(bodies),
        mime_type="text/markdown", source_url="https://example.test/doc", metadata=metadata,
    )
    doc, version = put.document, put.document["latest_version"]
    gen = store.get_knowledge_base(tenant, kb["id"])["active_generation"]
    store.upsert_chunks(
        tenant, doc["id"], version, gen, [chunk(i, b, f"S{i}") for i, b in enumerate(bodies)], metadata=metadata or {}
    )
    batch = store.next_unembedded_batch(tenant, kb["id"], gen, document_id=doc["id"], doc_version=version)
    store.write_embeddings(tenant, kb["id"], gen, {c.id: vec(dims, hot[c.ordinal]) for c in batch}, embedding_model=kb["embedding_model"])
    if finalize:
        assert store.finalize_document(tenant, doc["id"], version, gen, embedding_model=kb["embedding_model"]).status == "switched"
    return doc, version


def search(store, tenant, kb_ids, q, **kw):
    kw.setdefault("embedding_model", MODEL)
    kw.setdefault("top_k", 5)
    return store.search(tenant, kb_ids, q, **kw)


# ----------------------------------------------------------------------- knowledge bases


def test_crud_and_name_conflict(store, tenant, other_tenant):
    kb = make_kb(store, tenant)
    assert kb["active_generation"] == 1 and kb["status"] == "ready"
    assert kb["chunking"] == {"max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40, "chunker_version": 1}
    with pytest.raises(KnowledgeBaseNameConflict):
        make_kb(store, tenant)
    make_kb(store, other_tenant)  # names are per tenant
    renamed = store.update_knowledge_base(tenant, kb["id"], name="Manual", description=None)
    assert renamed["name"] == "Manual" and renamed["description"] is None
    listed = store.list_knowledge_bases(tenant)
    assert [k["name"] for k in listed] == ["Manual"] and listed[0]["document_count"] == 0
    assert store.get_knowledge_base(other_tenant, kb["id"]) is None
    assert store.get_knowledge_bases(tenant, [kb["id"].upper(), "not-a-uuid", str(uuid.uuid4())]) == [
        store.get_knowledge_bases(tenant, [kb["id"]])[0]
    ]
    with pytest.raises(ValueError):
        store.create_knowledge_base(tenant, name="x", embedding_model=MODEL, dimensions=512)
    assert store.delete_knowledge_base(other_tenant, kb["id"]) is False
    assert store.delete_knowledge_base(tenant, kb["id"]) is True
    assert store.delete_knowledge_base(tenant, kb["id"]) is False


def test_put_document_unchanged_is_a_noop_and_a_change_bumps_the_version(store, tenant):
    kb = make_kb(store, tenant)
    first = store.put_document(tenant, kb["id"], document_key="a", title="A", content="x", mime_type="text/plain")
    assert first.created and not first.unchanged and first.document["latest_version"] == 1
    same = store.put_document(tenant, kb["id"], document_key="a", title="A", content="x", mime_type="text/plain")
    assert same.unchanged and same.document["latest_version"] == 1
    meta = store.put_document(
        tenant, kb["id"], document_key="a", title="A", content="x", mime_type="text/plain", metadata={"lang": "en"}
    )
    assert not meta.unchanged and meta.document["latest_version"] == 2 and meta.document["status"] == "pending"
    assert store.get_document(tenant, kb["id"], "a", include_content=True)["content"] == "x"


# ---------------------------------------------------------------- versions and visibility


def test_only_the_active_version_is_visible_and_the_switch_is_atomic(store, tenant):
    kb = make_kb(store, tenant)
    doc, v1 = ingest(store, tenant, kb, "guide", ["alpha one", "beta two"], hot=[1, 2])
    hits = search(store, tenant, [kb["id"]], vec(1536, 1))
    assert hits[0].doc_version == v1 and hits[0].ordinal == 0 and hits[0].score == pytest.approx(1.0)
    assert hits[0].anchor_url == "https://example.test/doc#s0" and hits[0].document_key == "guide"

    # Version 2 fully embedded but not finalised: still invisible.
    _, v2 = ingest(store, tenant, kb, "guide", ["alpha changed"], hot=[3], finalize=False)
    assert {h.doc_version for h in search(store, tenant, [kb["id"]], vec(1536, 3))} == {v1}
    gen = kb["active_generation"]
    assert store.finalize_document(tenant, doc["id"], v2, gen, embedding_model=kb["embedding_model"]).status == "switched"
    hits = search(store, tenant, [kb["id"]], vec(1536, 3))
    assert {h.doc_version for h in hits} == {v2}
    # The old version's chunks are gone, and a repeated finalise is idempotent.
    progress = store.embedding_progress(tenant, kb["id"], gen, document_id=doc["id"], doc_version=v1)
    assert progress.total == 0
    again = store.finalize_document(tenant, doc["id"], v2, gen, embedding_model=kb["embedding_model"])
    assert again.status == "already_active" and again.chunk_count == 1
    assert store.get_document(tenant, kb["id"], "guide")["status"] == "ready"
    assert store.finalize_document(tenant, doc["id"], v1, gen, embedding_model=kb["embedding_model"]).status == "superseded"


def test_finalize_is_incomplete_until_every_chunk_is_embedded(store, tenant):
    kb = make_kb(store, tenant)
    doc, v = ingest(store, tenant, kb, "d", ["one", "two"], hot=[1, 2], finalize=False)
    gen = kb["active_generation"]
    with store.engine.begin() as conn:  # un-embed one chunk
        conn.execute(text(
            "UPDATE kb_chunks SET embedding = NULL, dimensions = NULL WHERE tenant_id = :t AND ordinal = 1"
        ), {"t": tenant})
    result = store.finalize_document(tenant, doc["id"], v, gen, embedding_model=kb["embedding_model"])
    assert result.status == "incomplete" and result.pending == 1
    assert store.finalize_document(tenant, doc["id"], v, gen + 1, embedding_model=MODEL).status == "stale_generation"
    assert store.get_document(tenant, kb["id"], "d")["active_version"] is None


def test_chunk_upsert_is_idempotent(store, tenant):
    kb = make_kb(store, tenant)
    put = store.put_document(tenant, kb["id"], document_key="d", title="D", content="c", mime_type="text/plain")
    chunks = [chunk(i, f"body {i}") for i in range(3)]
    first = store.upsert_chunks(tenant, put.document["id"], 1, 1, chunks, metadata={})
    again = store.upsert_chunks(tenant, put.document["id"], 1, 1, chunks, metadata={})
    assert (first.total, first.inserted) == (3, 3)
    assert (again.total, again.inserted) == (3, 0)
    assert store.embedding_progress(tenant, kb["id"], 1).total == 3
    with pytest.raises(GenerationNotWritable):
        store.upsert_chunks(tenant, put.document["id"], 1, 7, chunks, metadata={})


def test_write_embeddings_records_event_and_usage_once(store, tenant, engine):
    kb = make_kb(store, tenant)
    doc, v = ingest(store, tenant, kb, "d", ["one"], hot=[1], finalize=False)
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_chunks SET embedding = NULL, dimensions = NULL WHERE tenant_id = :t"), {"t": tenant})
    run_id = uuid.uuid4()
    RunStore(engine).create_kb_ingest_run(tenant, kb["id"], '{"document_id": "x", "doc_version": 1}', run_id)
    usage = {
        "request_key": "embed:batch:0", "model_id": MODEL, "provider_model_name": "hash", "input_tokens": 7,
        "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None, "price_source": None,
    }
    ids = [c.id for c in store.next_unembedded_batch(tenant, kb["id"], 1)]
    assert len(ids) == 1
    for attempt in range(2):  # a re-executed step writes nothing twice
        result = store.write_embeddings(
            tenant, kb["id"], 1, {i: vec(1536, 1) for i in ids}, run_id=str(run_id),
            event_key="embed:batch:0", event_data={"embedded": 1, "total": 1}, usage=usage, embedding_model=MODEL)
        assert result.written == (1 if attempt == 0 else 0)
        assert result.event_created is (attempt == 0)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT operation, input_tokens FROM run_model_requests WHERE run_id = :r"
        ), {"r": str(run_id)}).all()
        events = conn.execute(text(
            "SELECT type, data FROM execution_events WHERE run_id = :r"
        ), {"r": str(run_id)}).all()
    assert rows == [("embedding", 7)]
    assert [(e.type, e.data) for e in events] == [("ingest_progress", {"embedded": 1, "total": 1})]
    run = RunStore(engine).get_run(tenant, run_id)
    assert run["kind"] == "kb_ingest" and run["knowledge_base_id"] == kb["id"]


# --------------------------------------------------------------------- tenant isolation


def test_another_tenant_never_sees_or_touches_a_knowledge_base(store, tenant, other_tenant):
    kb = make_kb(store, tenant)
    doc, v = ingest(store, tenant, kb, "secret", ["alpha"], hot=[1])
    # Tenant B names tenant A's id: no rows, no error, nothing changed.
    assert search(store, other_tenant, [kb["id"]], vec(1536, 1)) == []
    assert store.explain_search(other_tenant, [kb["id"]], vec(1536, 1), embedding_model=MODEL, top_k=3) == ""
    assert store.existing_knowledge_base_ids(other_tenant, [kb["id"]]) == set()
    assert store.list_documents(other_tenant, kb["id"]) == []
    assert store.get_document_by_id(other_tenant, doc["id"]) is None
    assert store.get_ingest_source(other_tenant, doc["id"], v) is None
    assert store.delete_document(other_tenant, kb["id"], "secret") is False
    with pytest.raises(KnowledgeBaseNotFound):
        store.put_document(other_tenant, kb["id"], document_key="x", title="x", content="x", mime_type="text/plain")
    with pytest.raises(DocumentNotFound):
        store.upsert_chunks(other_tenant, doc["id"], v, 1, [chunk(0, "x")], metadata={})
    with pytest.raises(KnowledgeBaseNotFound):
        store.write_embeddings(other_tenant, kb["id"], 1, {}, embedding_model=MODEL)
    assert store.finalize_document(other_tenant, doc["id"], v, 1, embedding_model=MODEL).status == "missing"
    # Composite FKs: a chunk row cannot point at another tenant's document.
    with pytest.raises(IntegrityError):
        with store.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO kb_chunks (tenant_id, kb_id, document_id, doc_version, generation, ordinal, title,"
                " content, embed_text, embed_text_hash, approx_tokens) VALUES (:t, :kb, :d, 1, 1, 9, 't', 'c',"
                " 'e', 'h', 1)"
            ), {"t": other_tenant, "kb": kb["id"], "d": doc["id"]})
    assert [h.document_key for h in search(store, tenant, [kb["id"]], vec(1536, 1))] == ["secret"]


# ----------------------------------------------------------------------------- cascades


def test_document_and_knowledge_base_deletes_cascade(store, tenant, engine):
    kb = make_kb(store, tenant)
    ingest(store, tenant, kb, "a", ["one", "two"], hot=[1, 2])
    ingest(store, tenant, kb, "b", ["three"], hot=[3])
    run_id = uuid.uuid4()
    RunStore(engine).create_kb_ingest_run(tenant, kb["id"], "{}", run_id)

    def count(table):
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant}).scalar()

    assert count("kb_chunks") == 3
    assert store.delete_document(tenant, kb["id"], "a") is True
    assert count("kb_chunks") == 1 and count("kb_documents") == 1
    assert store.delete_knowledge_base(tenant, kb["id"]) is True
    assert (count("kb_chunks"), count("kb_documents"), count("knowledge_bases")) == (0, 0, 0)
    assert RunStore(engine).get_run(tenant, run_id) is not None  # runs stay (audit and cost)


# ------------------------------------------------------------------------- embedding cache


def test_cache_reuses_only_within_the_same_knowledge_base_and_generation(store, tenant):
    kb = make_kb(store, tenant)
    other = make_kb(store, tenant, name="Other")
    ingest(store, tenant, kb, "a", ["shared text"], hot=[5])
    # Same embed text in another document of the same knowledge base: reused.
    put = store.put_document(tenant, kb["id"], document_key="b", title="B", content="c", mime_type="text/plain")
    store.upsert_chunks(tenant, put.document["id"], 1, 1, [chunk(0, "shared text", "S0")], metadata={})
    assert store.apply_cached_embeddings(tenant, kb["id"], 1, document_id=put.document["id"], doc_version=1) == 1
    assert store.next_unembedded_batch(tenant, kb["id"], 1) == []
    # Same embed text in another knowledge base: not reused.
    put2 = store.put_document(tenant, other["id"], document_key="b", title="B", content="c", mime_type="text/plain")
    store.upsert_chunks(tenant, put2.document["id"], 1, 1, [chunk(0, "shared text", "S0")], metadata={})
    assert store.apply_cached_embeddings(tenant, other["id"], 1) == 0
    progress = store.embedding_progress(tenant, other["id"], 1)
    assert (progress.total, progress.embedded) == (1, 0) and progress.pending_tokens > 0
    # Another generation (a reindex under another model) of the same knowledge base: not reused.
    store.start_reindex(tenant, kb["id"], embedding_model=MODEL_768, dimensions=768)
    # Only active versions are copied ("b" was never finalised).
    assert store.copy_generation_chunks(tenant, kb["id"], 2) == 1
    assert store.apply_cached_embeddings(tenant, kb["id"], 2) == 0
    assert store.embedding_progress(tenant, kb["id"], 2).embedded == 0


# ---------------------------------------------------------------------------- dimensions


def test_dimension_mismatch_guard(store, tenant, engine):
    kb = make_kb(store, tenant)
    doc, v = ingest(store, tenant, kb, "d", ["one"], hot=[1], finalize=False)
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_chunks SET embedding = NULL, dimensions = NULL WHERE tenant_id = :t"), {"t": tenant})
    [pending] = store.next_unembedded_batch(tenant, kb["id"], 1)
    with pytest.raises(EmbeddingDimensionMismatch) as err:
        store.write_embeddings(tenant, kb["id"], 1, {pending.id: vec(768, 1)}, embedding_model=MODEL)
    assert (err.value.expected, err.value.actual) == (1536, 768)
    assert pickle.loads(pickle.dumps(err.value)).actual == 768
    assert store.embedding_progress(tenant, kb["id"], 1).embedded == 0  # nothing written
    # The database guard: dimensions must equal the vector's length.
    with pytest.raises(IntegrityError, match="ck_kb_chunks_embedding"):
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE kb_chunks SET embedding = CAST(:e AS vector), dimensions = 1536 WHERE id = :id"
            ), {"e": "[" + ",".join(["0.1"] * 768) + "]", "id": pending.id})
    # A query vector of the right model but other dimensions.
    store.write_embeddings(tenant, kb["id"], 1, {pending.id: vec(1536, 1)}, embedding_model=MODEL)
    store.finalize_document(tenant, doc["id"], v, 1, embedding_model=MODEL)
    with pytest.raises(EmbeddingDimensionMismatch):
        search(store, tenant, [kb["id"]], vec(1024, 1))
    # Another model's knowledge bases are simply not searched.
    assert search(store, tenant, [kb["id"]], vec(1024, 1), embedding_model="test:other") == []


# -------------------------------------------------------------------- filters and scores


def test_min_score_and_metadata_filter(store, tenant):
    kb = make_kb(store, tenant)
    ingest(store, tenant, kb, "en", ["english"], hot=[1], metadata={"lang": "en", "year": 2024})
    ingest(store, tenant, kb, "de", ["german"], hot=[2], metadata={"lang": "de", "year": 2023})
    q = vec(1536, 1, 2)  # equally close to both (score ~0.707)
    assert {h.document_key for h in search(store, tenant, [kb["id"]], q)} == {"en", "de"}
    assert search(store, tenant, [kb["id"]], q, min_score=0.8) == []
    assert [h.document_key for h in search(store, tenant, [kb["id"]], q, metadata_filter={"lang": "de"})] == ["de"]
    both = search(store, tenant, [kb["id"]], q, metadata_filter={"lang": ["de", "en"], "year": [2023, 2024]})
    assert {h.document_key for h in both} == {"en", "de"}
    assert search(store, tenant, [kb["id"]], q, metadata_filter={"year": 2025}) == []
    assert [h.document_key for h in search(store, tenant, [kb["id"]], vec(1536, 1), top_k=1)] == ["en"]


# -------------------------------------------------------------------------------- reindex


def test_reindex_switches_the_generation_atomically(store, tenant):
    kb = make_kb(store, tenant)
    ingest(store, tenant, kb, "a", ["one", "two"], hot=[1, 2])
    estimate = store.estimate_reindex(tenant, kb["id"])
    assert estimate.chunks == 2 and estimate.approx_tokens > 0
    pending = store.start_reindex(tenant, kb["id"], embedding_model=MODEL_768, dimensions=768)
    assert pending["pending_generation"] == 2 and pending["status"] == "reindexing"
    with pytest.raises(ReindexInProgress):
        store.start_reindex(tenant, kb["id"], embedding_model=MODEL_768, dimensions=768)
    assert store.copy_generation_chunks(tenant, kb["id"], 2) == 2
    assert store.copy_generation_chunks(tenant, kb["id"], 2) == 0
    assert store.finalize_reindex(tenant, kb["id"], 2, embedding_model=MODEL_768).status == "incomplete"
    # Retrieval still uses generation 1 under the old model.
    assert len(search(store, tenant, [kb["id"]], vec(1536, 1))) == 2
    batch = store.next_unembedded_batch(tenant, kb["id"], 2)
    with pytest.raises(EmbeddingDimensionMismatch):
        store.write_embeddings(tenant, kb["id"], 2, {batch[0].id: vec(1536, 1)}, embedding_model=MODEL_768)
    store.write_embeddings(tenant, kb["id"], 2, {c.id: vec(768, c.ordinal + 1) for c in batch}, embedding_model=MODEL_768)
    result = store.finalize_reindex(tenant, kb["id"], 2, embedding_model=MODEL_768)
    assert result.status == "switched" and result.chunk_count == 2
    after = store.get_knowledge_base(tenant, kb["id"])
    assert (after["embedding_model"], after["dimensions"], after["active_generation"]) == (MODEL_768, 768, 2)
    assert after["pending_generation"] is None and after["status"] == "ready"
    assert search(store, tenant, [kb["id"]], vec(1536, 1)) == []  # old model: nothing
    hits = search(store, tenant, [kb["id"]], vec(768, 1), embedding_model=MODEL_768)
    assert hits[0].generation == 2 and hits[0].ordinal == 0
    assert store.embedding_progress(tenant, kb["id"], 1).total == 0  # old generation dropped
    assert store.finalize_reindex(tenant, kb["id"], 2, embedding_model=MODEL_768).status == "already_active"


def test_cancel_and_failed_reindex(store, tenant):
    kb = make_kb(store, tenant)
    ingest(store, tenant, kb, "a", ["one"], hot=[1])
    store.start_reindex(tenant, kb["id"], embedding_model=MODEL_768, dimensions=768)
    store.copy_generation_chunks(tenant, kb["id"], 2)
    assert store.mark_reindex_failed(tenant, kb["id"], 2, "embedding_failed: 0 of 1 sections embedded")
    assert store.get_knowledge_base(tenant, kb["id"])["status"] == "reindex_failed"
    assert store.cancel_reindex(tenant, kb["id"]) is True
    assert store.embedding_progress(tenant, kb["id"], 2).total == 0
    assert store.finalize_reindex(tenant, kb["id"], 2, embedding_model=MODEL_768).status == "missing"
    assert store.cancel_reindex(tenant, kb["id"]) is False


# ------------------------------------------------------------------------- index usage


@pytest.mark.parametrize("dims,model", [(768, MODEL_768), (1024, "test:hash1024"), (1536, MODEL)])
def test_search_uses_the_hnsw_index_of_its_dimension(store, tenant, dims, model):
    kb = make_kb(store, tenant, name=f"kb{dims}", model=model, dims=dims)
    ingest(store, tenant, kb, "a", ["one", "two"], hot=[1, 2], dims=dims)
    plan = store.explain_search(tenant, [kb["id"]], vec(dims, 1), embedding_model=model, top_k=4)
    assert f"ix_kb_chunks_embedding_{dims}" in plan, plan
    assert [h.ordinal for h in search(store, tenant, [kb["id"]], vec(dims, 1), embedding_model=model)] == [0, 1]


def test_errors_are_pickle_safe():
    for err in (
        KnowledgeBaseNameConflict("t", "n"),
        DocumentNotFound("d"),
        ReindexInProgress("k", 2),
        GenerationNotWritable("k", 3),
    ):
        assert str(pickle.loads(pickle.dumps(err))) == str(err)
