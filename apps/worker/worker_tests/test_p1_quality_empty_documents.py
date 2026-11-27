"""RAG P1 quality fix 2: a worker-extracted version with no extractable text is an EMPTY document,
not a failure.

The live Acme check marked 89 rst pages `extraction_failed` (toctree-only index pages, include-only
wrappers: `.. include::` is dropped, by decision). They now finish 'ready' with 0 chunks and no
error; their `extracted` event carries `empty_reason: "no_extractable_text"`. PDFs never do: an
image-only PDF fails `no_text_layer` (a blank one `no_extractable_text`) and stays re-extractable. The fail-closed rule stays for a document that already has searchable
chunks: an empty new version never replaces them. Real Postgres + pgvector; real sandbox children.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from eap_run_store import KnowledgeStore
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.kb_ingest_helpers import chunk_stats, cleanup_knowledge, create_kb, enqueue_kb_ingest
from worker_tests.test_conversation import step_names
from worker_tests.test_kb_extract_chunk import put, split_input

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "knowledge"))
from knowledge_tests.pdf_fixtures import build_pdf, manual_pdf  # noqa: E402

TOCTREE_ONLY = """\
.. toctree::
   :maxdepth: 2

   linux/index
   rtos/index
"""

INCLUDE_ONLY = """\
.. include:: _Users_Guide.rst
"""

WITH_TEXT = """\
Kernel
======

Build the kernel with the defconfig.
"""


@pytest.fixture
def kb_tenant(tenant, worker) -> Iterator[str]:
    yield tenant
    cleanup_knowledge(worker.engine, tenant)


@pytest.fixture
def store(worker) -> KnowledgeStore:
    return KnowledgeStore(worker.engine)


def run_rst(client, worker, store, tenant, kb_id, body: str, key="index.rst"):
    run_id = uuid.uuid4()
    res = store.put_document(tenant, kb_id, document_key=key, title="", source_url="https://d.example/i.html",
                             metadata={}, run_id=str(run_id), content=body, mime_type="text/x-rst")
    doc_id, version = res.document["id"], res.document["latest_version"]
    enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=split_input(doc_id, version),
                      app_version=APP_VERSION, run_id=run_id)
    return doc_id, version, run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=120)


def extracted(engine, run_id) -> dict:
    rows = [e for e in event_rows(engine, run_id) if e[2] == "extracted"]
    assert len(rows) == 1
    return rows[0][3]


def assert_empty_ready(store, worker, tenant, doc_id, version, run_id, run):
    assert run["status"] == "completed", run["error"]
    doc = store.get_document_by_id(tenant, doc_id)
    assert doc["status"] == "ready" and doc["error"] is None
    assert doc["active_version"] == version and doc["chunk_count"] == 0
    assert chunk_stats(worker.engine, tenant, doc_id) == []
    end = event_rows(worker.engine, run_id)[-1]
    assert end[2] == "run_completed" and end[3]["result"]["chunk_count"] == 0


@pytest.mark.parametrize("body", [TOCTREE_ONLY, INCLUDE_ONLY], ids=["toctree-only", "include-only"])
def test_a_toctree_or_include_only_rst_page_is_ready_and_empty(
    client, worker, worker_settings, store, kb_tenant, body
):
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, run_id, run = run_rst(client, worker, store, kb_tenant, kb_id, body)
    assert_empty_ready(store, worker, kb_tenant, doc_id, version, run_id, run)
    data = extracted(worker.engine, run_id)
    assert data["extractor"] == "rst" and data["empty_reason"] == "no_extractable_text" and data["chars"] == 0
    names = step_names(worker_settings.dbos_system_schema, run_id)
    assert "eap.kb_extract_chunk" in names and "eap.kb_finalize" in names
    assert "eap.kb_embed_write" not in names and "eap.kb_mark_failed" not in names  # nothing embedded, no failure


@pytest.mark.parametrize(("pdf", "code"), [
    (build_pdf([[], []], image_pages={0, 1}), "no_text_layer"),  # scanned: image pages, no text layer
    (build_pdf([[], []]), "no_extractable_text"),  # blank pages
], ids=["image-only", "blank"])
def test_a_pdf_with_no_text_stays_failed_never_empty(client, worker, store, kb_tenant, pdf, code):
    """Its pages have content part 2's image extraction can read: failed with no active version,
    which is what the bulk re-extract selects."""
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, run_id = put(store, kb_tenant, kb_id, pdf=pdf)
    enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id,
                      input_json=split_input(doc_id, version), app_version=APP_VERSION, run_id=run_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=120)
    assert run["status"] == "failed"
    end = event_rows(worker.engine, run_id)[-1]
    assert end[3]["error_code"] == "extraction_failed" and end[3]["extraction_code"] == code
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["status"] == "failed" and doc["active_version"] is None
    assert doc["error"].startswith("extraction_failed: ")
    assert "extracted" not in [e[2] for e in event_rows(worker.engine, run_id)]


def test_an_empty_yaml_file_is_ready_and_empty(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    run_id = uuid.uuid4()
    res = store.put_document(kb_tenant, kb_id, document_key="empty.yaml", title="", source_url=None, metadata={},
                             run_id=str(run_id), content="---\n...\n", mime_type="application/yaml")
    doc_id, version = res.document["id"], res.document["latest_version"]
    enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id,
                      input_json=split_input(doc_id, version), app_version=APP_VERSION, run_id=run_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=120)
    assert_empty_ready(store, worker, kb_tenant, doc_id, version, run_id, run)
    assert extracted(worker.engine, run_id)["empty_reason"] == "no_extractable_text"


def test_an_empty_new_version_still_fails_closed_over_searchable_chunks(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, v1, _, run = run_rst(client, worker, store, kb_tenant, kb_id, WITH_TEXT)
    assert run["status"] == "completed", run["error"]
    n = store.get_document_by_id(kb_tenant, doc_id)["chunk_count"]
    assert n > 0
    _, v2, r2, run2 = run_rst(client, worker, store, kb_tenant, kb_id, TOCTREE_ONLY)
    assert run2["status"] == "failed"
    assert event_rows(worker.engine, r2)[-1][3]["extraction_code"] == "no_extractable_text"
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["active_version"] == v1 and doc["chunk_count"] == n and doc["status"] == "failed"
    assert "extracted" not in [e[2] for e in event_rows(worker.engine, r2)]


def test_an_empty_document_takes_a_new_version_with_text_and_another_empty_one(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, v1, r1, run = run_rst(client, worker, store, kb_tenant, kb_id, TOCTREE_ONLY)
    assert_empty_ready(store, worker, kb_tenant, doc_id, v1, r1, run)
    # Empty again (an active version with 0 chunks has nothing to lose): ready and empty.
    _, v2, r2, run2 = run_rst(client, worker, store, kb_tenant, kb_id, INCLUDE_ONLY)
    assert v2 == v1 + 1
    assert_empty_ready(store, worker, kb_tenant, doc_id, v2, r2, run2)
    # Then real text: an ordinary version with chunks, and no empty_reason.
    _, v3, r3, run3 = run_rst(client, worker, store, kb_tenant, kb_id, WITH_TEXT)
    assert run3["status"] == "completed", run3["error"]
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["active_version"] == v3 and doc["chunk_count"] > 0 and doc["status"] == "ready"
    assert "empty_reason" not in extracted(worker.engine, r3)


def test_a_pdf_with_text_is_unchanged(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, run_id = put(store, kb_tenant, kb_id, pdf=manual_pdf())
    enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id,
                      input_json=split_input(doc_id, 1), app_version=APP_VERSION, run_id=run_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=120)
    assert run["status"] == "completed", run["error"]
    assert "empty_reason" not in extracted(worker.engine, run_id)
    assert store.get_document_by_id(kb_tenant, doc_id)["chunk_count"] > 0
