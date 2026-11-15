"""RAG P1 quality fixes, acceptance (worker): empty-ready vs fail-closed, end to end on Postgres.

New cases beyond test_p1_quality_empty_documents.py: title-plus-toctree, YAML and PDF re-ingest over
active chunks, a mixed PDF, and retrieval ignoring an empty document while its sibling is found."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from eap_run_store import KnowledgeStore
from eap_worker.models import hash_embedding
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.kb_ingest_helpers import chunk_stats, cleanup_knowledge, create_kb, enqueue_kb_ingest
from worker_tests.test_kb_extract_chunk import put, split_input
from worker_tests.test_p1_quality_empty_documents import TOCTREE_ONLY, WITH_TEXT, run_rst
from knowledge_tests.pdf_fixtures import body_lines, build_pdf, manual_pdf


@pytest.fixture
def kb_tenant(tenant, worker) -> Iterator[str]:
    yield tenant
    cleanup_knowledge(worker.engine, tenant)


@pytest.fixture
def store(worker) -> KnowledgeStore:
    return KnowledgeStore(worker.engine)


def run_doc(client, worker, store, tenant, kb_id, key, mime, content=None, pdf=None):
    run_id = uuid.uuid4()
    if pdf is not None:
        import base64
        content = base64.b64encode(pdf).decode()
    res = store.put_document(tenant, kb_id, document_key=key, title="", source_url=None, metadata={},
                             run_id=str(run_id), content=content, mime_type=mime)
    doc_id, version = res.document["id"], res.document["latest_version"]
    enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=split_input(doc_id, version),
                      app_version=APP_VERSION, run_id=run_id)
    return doc_id, version, run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=120)


def test_title_plus_toctree_page_is_ready_with_zero_chunks(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, v, run_id, run = run_rst(client, worker, store, kb_tenant, kb_id, "Index\n=====\n\n.. toctree::\n\n   a\n")
    assert run["status"] == "completed", run["error"]
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert (doc["status"], doc["chunk_count"], doc["active_version"], doc["error"]) == ("ready", 0, v, None)
    ev = [e for e in event_rows(worker.engine, run_id) if e[2] == "extracted"]
    assert len(ev) == 1 and ev[0][3]["empty_reason"] == "no_extractable_text"


def test_toctree_plus_real_text_is_ready_with_chunks_and_no_empty_reason(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    body = WITH_TEXT + "\n.. toctree::\n\n   a\n"
    doc_id, _, run_id, run = run_rst(client, worker, store, kb_tenant, kb_id, body)
    assert run["status"] == "completed", run["error"]
    assert store.get_document_by_id(kb_tenant, doc_id)["chunk_count"] > 0
    assert all("empty_reason" not in e[3] for e in event_rows(worker.engine, run_id) if e[2] == "extracted")


@pytest.mark.parametrize(("mime", "good", "empty"), [
    ("application/yaml", "title: Kernel\nproperties:\n  reg: {}\n", "---\n...\n"),
    ("text/x-rst", WITH_TEXT, TOCTREE_ONLY),
], ids=["yaml", "rst"])
def test_reingest_to_empty_over_active_chunks_keeps_the_old_chunks_and_fails(
    client, worker, store, kb_tenant, mime, good, empty
):
    kb_id = create_kb(store, kb_tenant)
    key = "doc.x"
    doc_id, v1, _, run = run_doc(client, worker, store, kb_tenant, kb_id, key, mime, good)
    assert run["status"] == "completed", run["error"]
    before = chunk_stats(worker.engine, kb_tenant, doc_id)
    assert before
    _, v2, r2, run2 = run_doc(client, worker, store, kb_tenant, kb_id, key, mime, empty)
    assert v2 == v1 + 1 and run2["status"] == "failed"
    assert event_rows(worker.engine, r2)[-1][3]["extraction_code"] == "no_extractable_text"
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["active_version"] == v1 and doc["status"] == "failed" and doc["chunk_count"] == len(before)
    assert chunk_stats(worker.engine, kb_tenant, doc_id) == before  # nothing deleted or added


def test_image_only_pdf_reingest_over_active_chunks_keeps_them(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, v1, _, run = run_doc(client, worker, store, kb_tenant, kb_id, "t.pdf", "application/pdf", pdf=manual_pdf())
    assert run["status"] == "completed", run["error"]
    before = chunk_stats(worker.engine, kb_tenant, doc_id)
    _, _, r2, run2 = run_doc(client, worker, store, kb_tenant, kb_id, "t.pdf", "application/pdf",
                             pdf=build_pdf([[], []], image_pages={0, 1}))
    assert run2["status"] == "failed"
    assert event_rows(worker.engine, r2)[-1][3]["extraction_code"] == "no_text_layer"
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["active_version"] == v1 and chunk_stats(worker.engine, kb_tenant, doc_id) == before


def test_mixed_pdf_is_chunked_and_completed(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    pdf = build_pdf([[], body_lines(0), body_lines(1)], image_pages={0})
    doc_id, _, run_id, run = run_doc(client, worker, store, kb_tenant, kb_id, "mixed.pdf", "application/pdf", pdf=pdf)
    assert run["status"] == "completed", run["error"]
    assert store.get_document_by_id(kb_tenant, doc_id)["chunk_count"] > 0
    ev = [e for e in event_rows(worker.engine, run_id) if e[2] == "extracted"][0][3]
    assert "empty_reason" not in ev and ev["image_only_pages"] == [1]


def test_retrieval_ignores_an_empty_document_and_finds_its_sibling(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    empty_id, _, _, r1 = run_rst(client, worker, store, kb_tenant, kb_id, TOCTREE_ONLY, key="toc.rst")
    sib_id, _, _, r2 = run_rst(client, worker, store, kb_tenant, kb_id, WITH_TEXT, key="kernel.rst")
    assert r1["status"] == r2["status"] == "completed"
    found = store.search(kb_tenant, [kb_id], hash_embedding("kernel defconfig", 1536),
                         embedding_model="test:hash", top_k=50)
    assert found and {h.document_id for h in found} == {sib_id}
    assert empty_id not in {h.document_id for h in found}
