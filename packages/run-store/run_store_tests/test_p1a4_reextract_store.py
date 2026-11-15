"""RAG P1.A4 store methods: the binary content digest, a new version from the same content (single
and bulk, with the chunker v2 rewrite), the outdated-extractor query and the extraction record."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import text

from eap_schemas import KnowledgeBaseNotFound
from eap_run_store.knowledge import DocumentNotFound, IngestRunConflict, KnowledgeStore

from .test_knowledge_store import ingest, make_kb

RST_MAP = {"text/x-rst": ("rst", 1), "text/prs.fallenstein.rst": ("rst", 1), "application/yaml": ("yaml", 2)}


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
            conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})


def _put(store, tenant, kb, key, mime="text/x-rst", **kw):
    return store.put_document(tenant, kb["id"], document_key=key, title=key, content=kw.pop("content", "body"),
                              mime_type=mime, **kw).document


def _record(engine, doc_id, eap):
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_chunks SET metadata = CAST(:m AS jsonb) WHERE document_id = :d"),
                     {"m": json.dumps({"eap": eap}), "d": doc_id})


def test_put_document_takes_a_binary_digest(store, tenant):
    kb = make_kb(store, tenant)
    digest = hashlib.sha256(b"%PDF-1.7").hexdigest()
    with pytest.raises(ValueError):
        _put(store, tenant, kb, "a.pdf", mime="application/pdf", content_sha256="ABC")
    doc = _put(store, tenant, kb, "a.pdf", mime="application/pdf", content="JVBERi0xLjc=", content_sha256=digest)
    assert doc["content_hash"] == digest
    again = store.put_document(tenant, kb["id"], document_key="a.pdf", title="a.pdf", content="JVBERi0xLjc=",
                               mime_type="application/pdf", content_sha256=digest)
    assert again.unchanged


def test_new_document_version_keeps_the_content(store, tenant, other_tenant):
    kb = make_kb(store, tenant)
    doc = _put(store, tenant, kb, "a.rst")
    run = str(uuid.uuid4())
    bumped = store.new_document_version(tenant, kb["id"], "a.rst", run_id=run)
    assert bumped["latest_version"] == 2 and bumped["content_hash"] == doc["content_hash"]
    assert bumped["status"] == "pending" and bumped["last_run_id"] == run
    with pytest.raises(IngestRunConflict):
        store.new_document_version(tenant, kb["id"], "a.rst", run_id=run, expected_latest_version=1)
    with pytest.raises(DocumentNotFound):
        store.new_document_version(tenant, kb["id"], "missing", run_id=run)
    with pytest.raises(DocumentNotFound):
        store.new_document_version(other_tenant, kb["id"], "a.rst", run_id=run)


def test_start_reextract_is_scoped_and_rewrites_only_the_chunker_version(store, tenant, other_tenant):
    kb = make_kb(store, tenant)
    other_kb = make_kb(store, tenant, name="Other")
    a = _put(store, tenant, kb, "a.rst")
    _put(store, tenant, kb, "b.rst")
    foreign = _put(store, tenant, other_kb, "c.rst")
    with pytest.raises(KnowledgeBaseNotFound):
        store.start_reextract(other_tenant, kb["id"], document_ids=None, chunker_version=2)
    with pytest.raises(KnowledgeBaseNotFound):
        store.start_reextract(tenant, str(uuid.uuid4()), document_ids=None)
    updated, docs, changed = store.start_reextract(tenant, kb["id"], document_ids=[a["id"], foreign["id"], "junk"])
    assert not changed and [d["document_key"] for d in docs] == ["a.rst"] and docs[0]["latest_version"] == 2
    assert store.get_document(tenant, other_kb["id"], "c.rst")["latest_version"] == 1
    before = dict(updated["chunking"])
    updated, docs, changed = store.start_reextract(tenant, kb["id"], document_ids=None, chunker_version=2)
    assert changed and updated["chunking"] == {**before, "chunker_version": 2}
    assert {d["document_key"]: d["latest_version"] for d in docs} == {"a.rst": 3, "b.rst": 2}
    assert len({d["last_run_id"] for d in docs}) == 2 and all(uuid.UUID(d["last_run_id"]) for d in docs)
    _, docs, changed = store.start_reextract(tenant, kb["id"], document_ids=[], chunker_version=2)
    assert not changed and docs == []
    # A duplicate switch (already on v2 once the lock is held) bumps nothing.
    _, docs, changed = store.start_reextract(tenant, kb["id"], document_ids=None, chunker_version=2)
    assert not changed and docs == []
    assert store.get_document(tenant, kb["id"], "a.rst")["latest_version"] == 3


def test_outdated_extraction_documents(store, engine, tenant, other_tenant):
    kb = make_kb(store, tenant)
    current, _ = ingest(store, tenant, kb, "current.rst", ["body one"], hot=[1])
    old, _ = ingest(store, tenant, kb, "old.yaml", ["body two"], hot=[2])
    stringy, _ = ingest(store, tenant, kb, "stringy.rst", ["body three"], hot=[3])
    newer, _ = ingest(store, tenant, kb, "newer.rst", ["body four"], hot=[4])
    other, _ = ingest(store, tenant, kb, "other.rst", ["body five"], hot=[5])
    with engine.begin() as conn:  # ingest() stores Markdown; give them the types under test
        for doc, mime in ((current, "text/x-rst"), (old, "application/yaml"), (stringy, "text/prs.fallenstein.rst"),
                          (newer, "Text/X-RST; charset=utf-8"), (other, "text/x-rst")):
            conn.execute(text("UPDATE kb_documents SET mime_type = :m WHERE id = :id"), {"m": mime, "id": doc["id"]})
    _record(engine, current["id"], {"extractor": "rst", "extractor_version": 1})
    _record(engine, old["id"], {"extractor": "yaml", "extractor_version": 1})
    _record(engine, stringy["id"], {"extractor": "rst", "extractor_version": "1"})  # not a number: outdated
    _record(engine, newer["id"], {"extractor": "rst", "extractor_version": 7})
    _record(engine, other["id"], {"extractor": "markdown-doxygen", "extractor_version": 1})
    never = _put(store, tenant, kb, "never.rst")  # no active version
    md = _put(store, tenant, kb, "plain.md", mime="text/markdown")  # API path: not in the map
    keys = [d["document_key"] for d in store.outdated_extraction_documents(tenant, kb["id"], RST_MAP)]
    assert keys == ["never.rst", "old.yaml", "other.rst", "stringy.rst"]
    assert store.outdated_extraction_documents(other_tenant, kb["id"], RST_MAP) == []
    assert store.outdated_extraction_documents(tenant, kb["id"], {}) == []
    del never, md


def test_latest_extraction_is_the_active_versions(store, engine, tenant):
    from eap_run_store import RunStore

    kb = make_kb(store, tenant)
    doc = _put(store, tenant, kb, "a.pdf", mime="application/pdf")
    runs = RunStore(engine)
    run_ids = {}
    for version, pages in ((1, 3), (1, 4), (2, 5)):  # version 1 extracted twice (a retry), then 2
        run_id = uuid.uuid4()
        run_ids.setdefault(version, []).append(str(run_id))
        runs.create_kb_ingest_run(tenant, kb["id"], json.dumps({"document_id": doc["id"], "doc_version": version}), run_id)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                     " VALUES (:t, :r, 1, :k, 'extracted', CAST(:d AS jsonb), now() + make_interval(secs => :p))"),
                {"t": tenant, "r": str(run_id), "k": f"extract:{version}", "p": pages,
                 "d": json.dumps({"extractor": "pdf", "extractor_version": 1, "sections": 1, "chars": 9, "pages": pages})},
            )
    assert store.latest_extraction(tenant, kb["id"], doc["id"]) is None  # no active version yet
    with engine.begin() as conn:  # version 1 active; version 2 (newer, extracted) failed later
        conn.execute(text("UPDATE kb_documents SET active_version = 1, latest_version = 2, status = 'failed',"
                          " error = 'embedding_failed: x' WHERE id = :id"), {"id": doc["id"]})
    got = store.latest_extraction(tenant, kb["id"], doc["id"])
    assert got["pages"] == 4 and got["doc_version"] == 1 and got["run_id"] == run_ids[1][1]
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_documents SET active_version = 2, status = 'ready' WHERE id = :id"), {"id": doc["id"]})
    assert store.latest_extraction(tenant, kb["id"], doc["id"])["pages"] == 5
    assert store.latest_extraction("someone-else", kb["id"], doc["id"]) is None
