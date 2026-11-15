"""Acceptance: which documents `outdated_extraction_documents` selects, per ingest state (browser check
finding A). Pending/ingesting first ingests and in-flight newer versions are skipped; failed and
never-ingested-then-failed documents, and ready stale ones, are selected; other tenants never are."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from eap_run_store.knowledge import KnowledgeStore

from .test_knowledge_store import ingest, make_kb

RST_MAP = {"text/x-rst": ("rst", 1)}


@pytest.fixture
def store(engine):
    return KnowledgeStore(engine)


@pytest.fixture
def tenants(engine):
    ts = (f"kb_t_{uuid.uuid4().hex[:8]}", f"kb_o_{uuid.uuid4().hex[:8]}")
    yield ts
    with engine.begin() as conn:
        for t in ts:
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})


def _status(engine, doc_id, status):
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_documents SET status = :s WHERE id = :i"), {"s": status, "i": doc_id})


def _never(store, tenant, kb, key, status):
    doc = store.put_document(tenant, kb["id"], document_key=key, title=key, content="body",
                             mime_type="text/x-rst").document
    if status != "pending":
        _status(store.engine, doc["id"], status)
    return doc


def _stale(store, engine, tenant, kb, key):
    doc, _ = ingest(store, tenant, kb, key, ["body"], hot=[1])
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_documents SET mime_type = 'text/x-rst' WHERE id = :i"), {"i": doc["id"]})
        conn.execute(text("UPDATE kb_chunks SET metadata = CAST(:m AS jsonb) WHERE document_id = :i"),
                     {"m": json.dumps({"eap": {"extractor": "rst", "extractor_version": 0}}), "i": doc["id"]})
    return doc


def _keys(store, tenant, kb):
    return sorted(d["document_key"] for d in store.outdated_extraction_documents(tenant, kb["id"], RST_MAP))


def test_selection_matrix_by_ingest_state(store, engine, tenants):
    tenant, other = tenants
    kb = make_kb(store, tenant)
    _never(store, tenant, kb, "never-pending.rst", "pending")
    _never(store, tenant, kb, "never-ingesting.rst", "ingesting")
    _never(store, tenant, kb, "never-failed.rst", "failed")
    _stale(store, engine, tenant, kb, "stale-ready.rst")
    for key, status in (("v2-pending.rst", "pending"), ("v2-ingesting.rst", "ingesting"), ("v2-failed.rst", "failed")):
        doc = _stale(store, engine, tenant, kb, key)
        store.new_document_version(tenant, kb["id"], key, run_id=str(uuid.uuid4()))
        _status(engine, doc["id"], status)
    assert _keys(store, tenant, kb) == ["never-failed.rst", "stale-ready.rst", "v2-failed.rst"]
    # Tenant isolation: the same state under another tenant's knowledge base selects nothing here.
    kb_other = make_kb(store, other)
    _never(store, other, kb_other, "x.rst", "failed")
    assert _keys(store, tenant, kb) == ["never-failed.rst", "stale-ready.rst", "v2-failed.rst"]
    assert store.outdated_extraction_documents(tenant, kb_other["id"], RST_MAP) == []


def test_a_document_leaves_the_selection_when_its_run_starts_and_returns_if_it_fails(store, engine, tenants):
    tenant, _ = tenants
    kb = make_kb(store, tenant)
    doc = _never(store, tenant, kb, "a.rst", "failed")
    assert _keys(store, tenant, kb) == ["a.rst"]
    _status(engine, doc["id"], "pending")  # re-queued
    assert _keys(store, tenant, kb) == []
    _status(engine, doc["id"], "ingesting")
    assert _keys(store, tenant, kb) == []
    _status(engine, doc["id"], "failed")
    assert _keys(store, tenant, kb) == ["a.rst"]
