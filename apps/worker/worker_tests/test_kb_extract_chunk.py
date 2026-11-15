"""P1.A3: eap.kb_extract_chunk, the sandboxed extract-and-chunk step for worker-extracted documents,
against real Postgres + pgvector and an in-process DBOS worker."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from eap_run_store import KnowledgeStore
from eap_worker import extract_sandbox, kb_ingest
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.kb_ingest_helpers import (
    assert_contiguous,
    chunk_stats,
    cleanup_knowledge,
    create_kb,
    enqueue_kb_ingest,
    ingest_input,
    markdown_doc,
    reindex_input,
)
from worker_tests.test_conversation import step_names

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "knowledge"))
from knowledge_tests.pdf_fixtures import body_lines, build_pdf, manual_pdf  # noqa: E402

SOURCE = "https://docs.example/am62x/trm.pdf"


@pytest.fixture
def kb_tenant(tenant, worker) -> Iterator[str]:
    yield tenant
    cleanup_knowledge(worker.engine, tenant)


@pytest.fixture
def store(worker) -> KnowledgeStore:
    return KnowledgeStore(worker.engine)


@pytest.fixture
def sandbox_calls(monkeypatch) -> list[str]:
    """Spies on eap_worker.extract_sandbox.extract_in_sandbox (the parse), passing calls through."""
    calls: list[str] = []
    real = extract_sandbox.extract_in_sandbox

    async def spy(extractor, *args, **kwargs):
        calls.append(extractor)
        return await real(extractor, *args, **kwargs)

    monkeypatch.setattr(extract_sandbox, "extract_in_sandbox", spy)
    return calls


def split_input(document_id: str, version: int) -> str:
    return json.dumps(
        {"doc_version": version, "document_id": document_id, "embed_split": True}, sort_keys=True, separators=(",", ":")
    )


def put(store, tenant, kb_id, *, pdf: bytes | None = None, markdown: str | None = None, key="trm.pdf"):
    run_id = uuid.uuid4()
    if pdf is not None:
        kwargs = {"content": base64.b64encode(pdf).decode(), "mime_type": "application/pdf"}
    else:
        kwargs = {"content": markdown, "mime_type": "text/markdown"}
    result = store.put_document(
        tenant, kb_id, document_key=key, title="", source_url=SOURCE, metadata={"device": "am62x"},
        run_id=str(run_id), **kwargs,
    )
    return result.document["id"], result.document["latest_version"], run_id


def ingest(client, worker, store, tenant, kb_id, *, split=True, **kw):
    doc_id, version, run_id = put(store, tenant, kb_id, **kw)
    input_json = split_input(doc_id, version) if split else ingest_input(doc_id, version)
    enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=input_json,
                      app_version=APP_VERSION, run_id=run_id)
    return doc_id, version, run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=120)


def chunk_rows(engine, tenant, doc_id):
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT ordinal, heading_path, anchor_url, metadata, content FROM kb_chunks"
                    " WHERE tenant_id = :t AND document_id = :d ORDER BY ordinal"
                ),
                {"t": tenant, "d": doc_id},
            ).mappings()
        ]


# --------------------------------------------------------------------------- end to end


def test_pdf_ingests_with_page_citations(client, worker, worker_settings, store, kb_tenant, sandbox_calls):
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, run_id, run = ingest(client, worker, store, kb_tenant, kb_id, pdf=manual_pdf())
    assert run["status"] == "completed", run["error"]
    assert sandbox_calls == ["pdf"]
    names = step_names(worker_settings.dbos_system_schema, run_id)
    assert "eap.kb_extract_chunk" in names and "eap.kb_chunk" not in names
    assert names[:3] == ["eap.start_ingest", "eap.append_event", "eap.kb_extract_chunk"]

    rows = chunk_rows(worker.engine, kb_tenant, doc_id)
    assert rows
    for row in rows:
        eap = row["metadata"]["eap"]
        assert eap["extractor"] == "pdf" and eap["extractor_version"] == 1 and eap["page"] >= 1
        assert row["anchor_url"] == f"{SOURCE}#page={eap['page']}"
        assert row["metadata"]["device"] == "am62x"  # the user's metadata is kept next to `eap`
    pins = [r for r in rows if r["heading_path"] == ["Setup", "Pins"]]
    assert pins and {r["metadata"]["eap"]["page"] for r in pins} <= {4, 5}
    assert not any("Copyright 2026" in r["content"] for r in rows)

    events = event_rows(worker.engine, run_id)
    assert_contiguous(events)
    keys = [e[1] for e in events]
    assert keys.count(f"extract:{version}") == 1 and keys.index(f"extract:{version}") == keys.index("chunk:0") - 1
    extracted = next(e for e in events if e[1] == f"extract:{version}")
    assert extracted[2] == "extracted"
    data = extracted[3]
    assert data["extractor"] == "pdf" and data["extractor_version"] == 1 and data["pages"] == 6
    assert data["image_only_pages"] == [6] and data["sections"] >= 5 and data["chars"] > 0
    assert "paragraph explains" not in json.dumps([e[3] for e in events])
    assert store.get_document_by_id(kb_tenant, doc_id)["status"] == "ready"


def test_markdown_keeps_kb_chunk(client, worker, worker_settings, store, kb_tenant, sandbox_calls):
    kb_id = create_kb(store, kb_tenant)
    _, _, run_id, run = ingest(client, worker, store, kb_tenant, kb_id, markdown=markdown_doc(3), key="h.md")
    assert run["status"] == "completed", run["error"]
    names = step_names(worker_settings.dbos_system_schema, run_id)
    assert names[:3] == ["eap.start_ingest", "eap.append_event", "eap.kb_chunk"]
    assert "eap.kb_extract_chunk" not in names and sandbox_calls == []
    assert "extracted" not in [e[2] for e in event_rows(worker.engine, run_id)]


def test_a_run_without_the_flag_keeps_kb_chunk(client, worker, worker_settings, store, kb_tenant, sandbox_calls):
    # A run enqueued by an earlier API (no embed_split) never gets the new step.
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, run_id, run = ingest(client, worker, store, kb_tenant, kb_id, pdf=manual_pdf(), split=False)
    names = step_names(worker_settings.dbos_system_schema, run_id)
    assert "eap.kb_chunk" in names and "eap.kb_extract_chunk" not in names and sandbox_calls == []
    assert run["status"] == "failed" and run["error"].startswith("UnsupportedMimeType:")


def test_encrypted_pdf_fails_with_its_reason(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, run_id, run = ingest(client, worker, store, kb_tenant, kb_id, pdf=build_pdf([body_lines(0)], encrypt="pw"))
    assert run["status"] == "failed"
    assert run["error"] == "ExtractionFailed: The PDF is encrypted with a password."
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["status"] == "failed" and doc["error"] == "extraction_failed: The PDF is encrypted with a password."
    end = event_rows(worker.engine, run_id)[-1]
    assert end[2] == "run_failed" and end[3]["error_code"] == "extraction_failed"
    assert end[3]["extraction_code"] == "document_encrypted"
    assert chunk_stats(worker.engine, kb_tenant, doc_id) == []


def test_reindex_never_calls_the_sandbox(client, worker, store, kb_tenant, sandbox_calls):
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, _, run = ingest(client, worker, store, kb_tenant, kb_id, pdf=manual_pdf())
    assert run["status"] == "completed", run["error"]
    before = chunk_rows(worker.engine, kb_tenant, doc_id)
    sandbox_calls.clear()
    kb = store.start_reindex(kb_tenant, kb_id, embedding_model="test:hash768", dimensions=768)
    run_id = enqueue_kb_ingest(
        client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id,
        input_json=json.dumps({"embed_split": True, "reindex": kb["pending_generation"]}, sort_keys=True),
        app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
    assert run["status"] == "completed", run["error"]
    assert sandbox_calls == []
    after = chunk_rows(worker.engine, kb_tenant, doc_id)
    assert [r["metadata"] for r in after] == [r["metadata"] for r in before]  # `eap` copied as is
    assert reindex_input(1)  # the helper's old (flagless) shape still exists for old runs


# --------------------------------------------------------------------------- the step, re-executed


def _prepare(worker, store, tenant, pdf: bytes):
    kb_id = create_kb(store, tenant)
    doc_id, version, run_id = put(store, tenant, kb_id, pdf=pdf)
    worker.run_store.create_kb_ingest_run(tenant, kb_id, split_input(doc_id, version), run_id)
    return kb_id, doc_id, version, str(run_id)


def _step(run_id, tenant, kb_id, doc_id, version):
    return asyncio.run(kb_ingest.kb_extract_chunk_step(run_id, tenant, kb_id, doc_id, version, "pdf"))


def test_reexecution_after_commit_does_not_parse(worker, store, kb_tenant, sandbox_calls):
    kb_id, doc_id, version, run_id = _prepare(worker, store, kb_tenant, manual_pdf())
    first = _step(run_id, kb_tenant, kb_id, doc_id, version)
    assert first["status"] == "ok" and first["inserted"] == first["total"] > 0
    assert sandbox_calls == ["pdf"]
    again = _step(run_id, kb_tenant, kb_id, doc_id, version)  # as after a crash before the checkpoint
    assert sandbox_calls == ["pdf"]  # no parse
    assert {k: again[k] for k in ("status", "total", "embedded", "generation")} == {
        k: first[k] for k in ("status", "total", "embedded", "generation")
    }
    keys = [e[1] for e in event_rows(worker.engine, run_id)]
    assert keys == [f"extract:{version}"]


def test_crash_after_the_sandbox_reparses_once(worker, store, kb_tenant, sandbox_calls, monkeypatch):
    kb_id, doc_id, version, run_id = _prepare(worker, store, kb_tenant, manual_pdf())

    class Crash(Exception):
        pass

    def crash(name, **_):
        if name == "kb_extract_sandbox":
            raise Crash

    monkeypatch.setattr(kb_ingest, "crash_point", crash)
    with pytest.raises(Crash):
        _step(run_id, kb_tenant, kb_id, doc_id, version)
    assert chunk_stats(worker.engine, kb_tenant, doc_id) == [] and event_rows(worker.engine, run_id) == []
    monkeypatch.setattr(kb_ingest, "crash_point", lambda *a, **k: None)
    out = _step(run_id, kb_tenant, kb_id, doc_id, version)
    assert out["status"] == "ok" and sandbox_calls == ["pdf", "pdf"]
    assert [e[1] for e in event_rows(worker.engine, run_id)] == [f"extract:{version}"]


def test_crash_between_rows_and_event_reparses_and_writes_the_event_once(
    worker, store, kb_tenant, sandbox_calls, monkeypatch
):
    from eap_worker.runtime import get_runtime

    kb_id, doc_id, version, run_id = _prepare(worker, store, kb_tenant, manual_pdf())
    event_log = get_runtime().event_log
    real_append = event_log.append
    failed: list[str] = []

    def append(tenant_id, rid, key, type_, data=None, usage=None):
        if key.startswith("extract:") and not failed:
            failed.append(key)
            raise RuntimeError("lost before the event")
        return real_append(tenant_id, rid, key, type_, data, usage)

    monkeypatch.setattr(event_log, "append", append)
    with pytest.raises(RuntimeError):
        _step(run_id, kb_tenant, kb_id, doc_id, version)
    rows_after_crash = chunk_stats(worker.engine, kb_tenant, doc_id)[0]["n"]
    out = _step(run_id, kb_tenant, kb_id, doc_id, version)
    assert sandbox_calls == ["pdf", "pdf"] and out["inserted"] == 0 and out["total"] == rows_after_crash
    assert [e[1] for e in event_rows(worker.engine, run_id)] == [f"extract:{version}"]


def test_step_failure_results_are_text_free_and_not_retried(worker, store, kb_tenant, sandbox_calls):
    kb_id, doc_id, version, run_id = _prepare(worker, store, kb_tenant, b"%PDF-1.7\n<html>SECRET-TEXT</html>")
    out = _step(run_id, kb_tenant, kb_id, doc_id, version)
    assert out == {
        "status": "failed", "code": "extraction_failed", "error_type": "ExtractionFailed",
        "message": "The document could not be extracted.", "reason": "extract_failed",
    }
    assert sandbox_calls == ["pdf"]


def test_start_ingest_records_the_extractor_only_for_flagged_runs(worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, run_id = put(store, kb_tenant, kb_id, pdf=manual_pdf())
    worker.run_store.create_kb_ingest_run(kb_tenant, kb_id, split_input(doc_id, version), run_id)
    out = asyncio.run(kb_ingest.start_ingest_step(str(run_id), kb_tenant, kb_id, split_input(doc_id, version)))
    assert out["outcome"] == "ok" and out["extractor"] == "pdf"
    old = asyncio.run(kb_ingest.start_ingest_step(str(run_id), kb_tenant, kb_id, ingest_input(doc_id, version)))
    assert "extractor" not in old


def test_a_retry_after_an_extractor_upgrade_replaces_the_old_rows(worker, store, kb_tenant, sandbox_calls, monkeypatch):
    from eap_knowledge import EXTRACTOR_VERSIONS

    kb_id, doc_id, version, run_id = _prepare(worker, store, kb_tenant, manual_pdf())
    first = _step(run_id, kb_tenant, kb_id, doc_id, version)
    assert first["status"] == "ok"
    assert {r["metadata"]["eap"]["extractor_version"] for r in chunk_rows(worker.engine, kb_tenant, doc_id)} == {1}

    # Same version, a retry run: the rows are kept (nothing new inserted).
    same_run = str(uuid.uuid4())
    worker.run_store.create_kb_ingest_run(kb_tenant, kb_id, split_input(doc_id, version), uuid.UUID(same_run))
    same = _step(same_run, kb_tenant, kb_id, doc_id, version)
    assert same["inserted"] == 0 and same["total"] == first["total"]

    # The extractor is upgraded, then a retry run of the same version: only new-version rows remain.
    monkeypatch.setitem(EXTRACTOR_VERSIONS, "pdf", 2)
    retry_run = str(uuid.uuid4())
    worker.run_store.create_kb_ingest_run(kb_tenant, kb_id, split_input(doc_id, version), uuid.UUID(retry_run))
    out = _step(retry_run, kb_tenant, kb_id, doc_id, version)
    rows = chunk_rows(worker.engine, kb_tenant, doc_id)
    assert {r["metadata"]["eap"]["extractor_version"] for r in rows} == {2}
    assert [r["ordinal"] for r in rows] == list(range(len(rows))) and out["total"] == len(rows)
    assert out["inserted"] == len(rows)
    event = next(e for e in event_rows(worker.engine, retry_run) if e[1] == f"extract:{version}")
    assert event[3]["extractor_version"] == 2
    assert sandbox_calls == ["pdf", "pdf", "pdf"]

    # Rolling deploy: an OLD worker (extractor version 1) retrying the version never deletes the
    # newer rows; its insert finds them and inserts nothing.
    monkeypatch.setitem(EXTRACTOR_VERSIONS, "pdf", 1)
    old_run = str(uuid.uuid4())
    worker.run_store.create_kb_ingest_run(kb_tenant, kb_id, split_input(doc_id, version), uuid.UUID(old_run))
    old = _step(old_run, kb_tenant, kb_id, doc_id, version)
    assert old["inserted"] == 0 and old["total"] == len(rows)
    assert {r["metadata"]["eap"]["extractor_version"] for r in chunk_rows(worker.engine, kb_tenant, doc_id)} == {2}


def test_a_markdown_flavour_switch_never_mixes_rows(worker, store, kb_tenant):
    from eap_knowledge import chunk, extract

    kb_id = create_kb(store, kb_tenant)
    doc_id, version, _ = put(store, kb_tenant, kb_id, markdown=markdown_doc(3), key="h.md")
    generation = store.get_knowledge_base(kb_tenant, kb_id)["active_generation"]
    chunks = chunk(extract(markdown_doc(3), "text/markdown"))
    doxygen = [{"extractor": "markdown-doxygen", "extractor_version": 1}] * len(chunks)

    def stamps():
        return {(r["metadata"].get("eap") or {}).get("extractor") for r in chunk_rows(worker.engine, kb_tenant, doc_id)}

    store.upsert_chunks(kb_tenant, doc_id, version, generation, chunks, metadata={}, extract_metadata=doxygen)
    assert stamps() == {"markdown-doxygen"}
    # Back to plain Markdown (eap.kb_chunk): the worker-extracted rows go, plain rows come in.
    result = store.upsert_chunks(kb_tenant, doc_id, version, generation, chunks, metadata={})
    assert stamps() == {None} and result.inserted == len(chunks)
    # A plain kb_chunk re-execution keeps its own rows.
    assert store.upsert_chunks(kb_tenant, doc_id, version, generation, chunks, metadata={}).inserted == 0
    # Doxygen again: the plain rows (no `eap`) are older and go.
    store.upsert_chunks(kb_tenant, doc_id, version, generation, chunks, metadata={}, extract_metadata=doxygen)
    assert stamps() == {"markdown-doxygen"}
    # Another extractor id, even at a lower version, replaces them (only same-extractor NEWER rows stay).
    other = [{"extractor": "rst", "extractor_version": 1}] * len(chunks)
    store.upsert_chunks(kb_tenant, doc_id, version, generation, chunks, metadata={}, extract_metadata=other)
    assert stamps() == {"rst"}
    assert len(chunk_rows(worker.engine, kb_tenant, doc_id)) == len(chunks)
