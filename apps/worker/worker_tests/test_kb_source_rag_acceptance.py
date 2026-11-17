"""The RAG proof of the composition rule (design: "the TI task is a workflow of tools feeding knowledge-base RAG").

A targeted sync with the fake catalogue hands a real PDF datasheet to `eap.kb_ingest` (the in-process worker
ingests it with the real extractor and the `test:hash` embedder); an agent bound to that knowledge base answers
through `retrieve` a question whose answer is only in the datasheet. Provider tools never enter: the agent sees the
knowledge base only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from eap_kb_sources.fake import FakeCatalogAdapter
from eap_kb_sources.registry import register_adapter
from eap_run_store import KnowledgeStore
from eap_run_store.kb_sources import ConnectionStore, SourceStore
from worker_tests.conftest import event_rows, wait_for
from worker_tests.kb_ingest_helpers import cleanup_knowledge
from worker_tests.test_kb_source_sync import start
from worker_tests.test_retrieve import by_type, make_kb, rag_spec, run

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "knowledge"))
from knowledge_tests.pdf_fixtures import build_pdf  # noqa: E402

FACT = "The absolute maximum supply voltage of FAKE0001 is 7.35 volts"
QUESTION = "What is the absolute maximum supply voltage of FAKE0001"


def datasheet_pdf(key: str) -> bytes:
    """A small real PDF: a title page and a page with the one fact the test asks about."""
    return build_pdf(
        [[(f"{key} Product Datasheet", 18), ("Revision A, November 2026", 10)],
         [("Absolute maximum ratings", 14), (f"{FACT}.", 10), ("Operating temperature range is minus 40 to 125 degrees.", 10)]],
        title=f"{key} datasheet",
    )


class PdfCatalog(FakeCatalogAdapter):
    """The fake catalogue whose datasheet is a PDF (the base adapter's documents are Markdown)."""

    def __init__(self) -> None:
        super().__init__(size=4, page_size=5, documents=())  # no Markdown document of its own

    def _store(self, key, request, store, counters, errors):
        super()._store(key, request, store, counters, errors)
        entity = store.upsert_entity(key, lifecycle_status="ACTIVE", attributes={}, identifiers=[], parameters=[])
        url = f"https://fake.example/lit/{key}/datasheet.pdf"
        if store.document_state(entity, "datasheet", url) is None:
            counters.documents_discovered += 1
        if store.submit_document(entity, "datasheet", url, datasheet_pdf(key), mime_type="application/pdf",
                                 title=f"{key} datasheet", etag="rev1", source_revision="1") == "queued":
            counters.documents_queued += 1


@pytest.fixture
def env(worker, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    register_adapter("product_catalog", "fake", PdfCatalog)
    kb = make_kb(worker, tenant, name="Parts datasheets")
    conn = ConnectionStore(worker.engine).create(
        tenant, name="fake", kind="oauth2_client_credentials", base_url=None, config={}, credential_refs={})
    source = SourceStore(worker.engine).create(
        tenant, knowledge_base_id=kb["id"], connection_id=conn.id, name="parts", kind="product_catalog",
        provider="fake", config={"document_policy": {"types": ["datasheet"]}})
    yield SimpleNamespace(tenant=tenant, kb=kb, source=source, rt=worker, engine=worker.engine)
    cleanup_knowledge(worker.engine, tenant)
    with worker.engine.begin() as c:
        c.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": tenant})


def test_an_agent_answers_from_the_synced_datasheet(env, client):
    _sync_id, result = start(env, {"mode": "targeted", "part_numbers": ["FAKE0001", "FAKE0002"]})
    assert result["status"] == "completed" and result["report"]["documents_queued"] == 2

    def ingested():
        with env.engine.connect() as conn:
            rows = conn.execute(text("SELECT status, mime_type FROM kb_documents WHERE tenant_id = :t"),
                                {"t": env.tenant}).all()
        return rows if len(rows) == 2 and all(r.status == "ready" for r in rows) else None

    rows = wait_for(ingested, timeout=120, what="the datasheets to be ingested")
    assert {r.mime_type for r in rows} == {"application/pdf"}
    # the hand-off kept bytes out of the sync's own outputs: the report is counters only
    assert "7.35" not in json.dumps(result["report"])

    run_id, row = run(env.rt, client, env.tenant, rag_spec({"id": env.kb["id"]}), QUESTION)
    assert row["status"] == "completed", row
    [call] = by_type(event_rows(env.engine, run_id), "tool_result")
    assert call["status"] == "ok"
    assert "7.35 volts" in json.dumps(call["content"]), "the answer is only in the datasheet"
    assert any("FAKE0001" in (s.get("title") or s.get("document_key") or "") for s in call["sources"])
    assert row["output"].startswith("According to [1] "), row["output"]
    assert "FAKE0001 datasheet" in row["output"] or "FAKE0001" in row["output"]
