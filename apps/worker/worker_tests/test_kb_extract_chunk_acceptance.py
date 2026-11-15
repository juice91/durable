"""P1.A3 acceptance: eap.kb_extract_chunk end to end: golden step lists, `metadata.eap`, events,
failures, extractor upgrade, and real-process crashes at kb_extract_sandbox / kb_extract_chunk.
Real Postgres + pgvector; real sandbox children."""

from __future__ import annotations

import base64
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from dbos import DBOSClient
from sqlalchemy import text

from eap_run_store import KnowledgeStore
from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.kb_ingest_helpers import (
    assert_contiguous,
    chunk_stats,
    cleanup_knowledge,
    create_kb,
    enqueue_kb_ingest,
    ingest_input,
    markdown_doc,
)
from worker_tests.test_conversation import step_names
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_kb_extract_chunk import chunk_rows, put, split_input

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "knowledge"))
from knowledge_tests.pdf_fixtures import body_lines, build_pdf, manual_pdf  # noqa: E402

PIPE_HEAD = ["eap.start_ingest", "eap.append_event"]
TAIL = ["eap.kb_finalize", "eap.append_event", "eap.finish_run"]


@pytest.fixture
def kb_tenant(tenant, worker) -> Iterator[str]:
    yield tenant
    cleanup_knowledge(worker.engine, tenant)


@pytest.fixture
def store(worker) -> KnowledgeStore:
    return KnowledgeStore(worker.engine)


def go(client, worker, store, tenant, kb_id, *, split=True, **doc):
    doc_id, version, run_id = put(store, tenant, kb_id, **doc)
    inp = split_input(doc_id, version) if split else ingest_input(doc_id, version)
    enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=inp,
                      app_version=APP_VERSION, run_id=run_id)
    return doc_id, version, run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=120)


def put_rst(store, tenant, kb_id, body: str, key="guide.rst"):
    run_id = uuid.uuid4()
    res = store.put_document(tenant, kb_id, document_key=key, title="", source_url="https://d.example/g.html",
                             metadata={"team": "hw"}, run_id=str(run_id), content=body, mime_type="text/x-rst")
    return res.document["id"], res.document["latest_version"], run_id


# --------------------------------------------------------------------------- golden step lists


def test_golden_step_lists_old_shape_new_flag_and_pdf(client, worker, worker_settings, store, kb_tenant):
    schema = worker_settings.dbos_system_schema
    # 1. Markdown, run input without embed_split (an API before P1.C5): the original layout.
    old_kb = create_kb(store, kb_tenant)
    _, _, r1, run = go(client, worker, store, kb_tenant, old_kb, markdown=markdown_doc(3, tag="g1"), key="a.md", split=False)
    assert run["status"] == "completed", run["error"]
    names = step_names(schema, r1)
    assert names == PIPE_HEAD + ["eap.kb_chunk", "eap.append_event", "eap.kb_embed_batch", "eap.kb_embed_batch", *TAIL]
    # 2. Markdown with embed_split: kb_chunk stays; only the embed steps change.
    new_kb = create_kb(store, kb_tenant)
    _, _, r2, run = go(client, worker, store, kb_tenant, new_kb, markdown=markdown_doc(3, tag="g2"), key="b.md")
    assert run["status"] == "completed", run["error"]
    names = step_names(schema, r2)
    assert names == PIPE_HEAD + ["eap.kb_chunk", "eap.append_event", "eap.kb_embed_call", "eap.kb_embed_write",
                                 "eap.kb_embed_call", *TAIL]
    # 3. PDF with embed_split: kb_extract_chunk takes kb_chunk's slot, nothing else moves.
    pdf_kb = create_kb(store, kb_tenant)
    _, _, r3, run = go(client, worker, store, kb_tenant, pdf_kb, pdf=manual_pdf())
    assert run["status"] == "completed", run["error"]
    names = step_names(schema, r3)
    assert names == PIPE_HEAD + ["eap.kb_extract_chunk", "eap.append_event", "eap.kb_embed_call", "eap.kb_embed_write",
                                 "eap.kb_embed_call", *TAIL]


@pytest.mark.parametrize("mime", ["text/markdown", "text/plain", "text/html"])
def test_text_formats_with_the_flag_keep_kb_chunk_and_no_eap_metadata(
    client, worker, worker_settings, store, kb_tenant, mime
):
    kb_id = create_kb(store, kb_tenant)
    body = {"text/markdown": "# Title\n\nSome words about the board.\n", "text/plain": "Some plain words about a board.\n",
            "text/html": "<html><body><h1>Title</h1><p>Some words about the board.</p></body></html>"}[mime]
    run_id = uuid.uuid4()
    res = store.put_document(kb_tenant, kb_id, document_key="doc", title="T", content=body, mime_type=mime,
                             metadata={"k": "v"}, run_id=str(run_id))
    doc_id, version = res.document["id"], res.document["latest_version"]
    enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=split_input(doc_id, version),
                      app_version=APP_VERSION, run_id=run_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
    assert run["status"] == "completed", run["error"]
    names = step_names(worker_settings.dbos_system_schema, run_id)
    assert "eap.kb_chunk" in names and "eap.kb_extract_chunk" not in names
    assert all("eap" not in r["metadata"] for r in chunk_rows(worker.engine, kb_tenant, doc_id))
    assert "extracted" not in [e[2] for e in event_rows(worker.engine, run_id)]


def test_a_pdf_in_an_old_shape_run_fails_as_before_without_the_sandbox(client, worker, worker_settings, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, r, run = go(client, worker, store, kb_tenant, kb_id, pdf=manual_pdf(), split=False)
    assert run["status"] == "failed" and "eap.kb_extract_chunk" not in step_names(worker_settings.dbos_system_schema, r)
    assert chunk_stats(worker.engine, kb_tenant, doc_id) == []


# --------------------------------------------------------------------------- metadata.eap and events


def test_every_pdf_chunk_has_eap_metadata_and_user_metadata_survives(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, r, run = go(client, worker, store, kb_tenant, kb_id, pdf=manual_pdf())
    assert run["status"] == "completed", run["error"]
    rows = chunk_rows(worker.engine, kb_tenant, doc_id)
    assert rows
    for row in rows:
        eap = row["metadata"]["eap"]
        assert eap["extractor"] == "pdf" and eap["extractor_version"] == 1
        assert isinstance(eap["page"], int) and 1 <= eap["page"] <= 6
        assert row["metadata"]["device"] == "am62x" and row["anchor_url"].endswith(f"#page={eap['page']}")
    assert [r["ordinal"] for r in rows] == list(range(len(rows)))


def test_every_rst_chunk_has_eap_metadata(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, run_id = put_rst(store, kb_tenant, kb_id, "Title\n=====\n\nHello there world, board text.\n\nNext\n----\n\nMore words here.\n")
    enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=split_input(doc_id, version),
                      app_version=APP_VERSION, run_id=run_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
    assert run["status"] == "completed", run["error"]
    rows = chunk_rows(worker.engine, kb_tenant, doc_id)
    assert rows and all(r["metadata"]["eap"]["extractor"] == "rst" and r["metadata"]["team"] == "hw" for r in rows)


def test_one_extracted_event_with_counts_only_and_no_text(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, r, run = go(client, worker, store, kb_tenant, kb_id, pdf=manual_pdf())
    events = event_rows(worker.engine, r)
    assert_contiguous(events)
    assert [e[2] for e in events].count("extracted") == 1
    blob = json.dumps([e[3] for e in events])
    assert "paragraph" not in blob and "Copyright" not in blob and "AM62x" not in blob
    assert [e[1] for e in events] == ["start", f"extract:{version}", "chunk:0", "embed:batch:0", "finalize:0", "end"]


# --------------------------------------------------------------------------- failures


def test_corrupt_pdf_fails_with_a_safe_document_error(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, r, run = go(client, worker, store, kb_tenant, kb_id, pdf=b"%PDF-1.7\n<html>SECRET-TEXT</html>")
    assert run["status"] == "failed" and "SECRET" not in (run["error"] or "")
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["status"] == "failed" and doc["error"].startswith("extraction_failed: ") and "SECRET" not in doc["error"]
    events = event_rows(worker.engine, r)
    assert "extracted" not in [e[2] for e in events] and events[-1][3]["error_code"] == "extraction_failed"
    assert "SECRET" not in json.dumps([e[3] for e in events])


def test_page_limit_from_the_env_fails_with_extract_limit(client, worker, store, kb_tenant, monkeypatch):
    monkeypatch.setenv("EAP_KB_MAX_PDF_PAGES", "3")
    kb_id = create_kb(store, kb_tenant)
    _, _, r, run = go(client, worker, store, kb_tenant, kb_id, pdf=manual_pdf())
    assert run["status"] == "failed"
    assert event_rows(worker.engine, r)[-1][3]["extraction_code"] == "extract_limit"


def test_an_image_only_new_version_fails_closed_and_keeps_the_active_version(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, v1, _, run = go(client, worker, store, kb_tenant, kb_id, pdf=manual_pdf())
    assert run["status"] == "completed", run["error"]
    n = chunk_stats(worker.engine, kb_tenant, doc_id)[0]["n"]
    image_only = build_pdf([[], []], image_pages={0, 1})
    _, v2, r2, run2 = go(client, worker, store, kb_tenant, kb_id, pdf=image_only)
    assert run2["status"] == "failed"
    assert event_rows(worker.engine, r2)[-1][3]["extraction_code"] == "no_extractable_text"
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["active_version"] == v1
    stats = [s for s in chunk_stats(worker.engine, kb_tenant, doc_id) if s["doc_version"] == v1]
    assert stats[0]["n"] == n and stats[0]["embedded"] == n


# --------------------------------------------------------------------------- extractor upgrade


def test_a_run_after_an_extractor_upgrade_replaces_unswitched_rows_and_embeds_them(
    client, worker, store, kb_tenant, monkeypatch
):
    from eap_knowledge import EXTRACTOR_VERSIONS
    from worker_tests.test_kb_extract_chunk import _prepare, _step

    kb_id, doc_id, version, first_run = _prepare(worker, store, kb_tenant, manual_pdf())
    assert _step(first_run, kb_tenant, kb_id, doc_id, version)["status"] == "ok"  # v1 rows, not switched
    monkeypatch.setitem(EXTRACTOR_VERSIONS, "pdf", 2)
    run_id = uuid.uuid4()
    enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=split_input(doc_id, version),
                      app_version=APP_VERSION, run_id=run_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=120)
    assert run["status"] == "completed", run["error"]
    rows = chunk_rows(worker.engine, kb_tenant, doc_id)
    assert {r["metadata"]["eap"]["extractor_version"] for r in rows} == {2}
    stats = chunk_stats(worker.engine, kb_tenant, doc_id)
    assert len(stats) == 1 and stats[0]["n"] == len(rows) == stats[0]["ordinals"] == stats[0]["embedded"]
    extracted = [e for e in event_rows(worker.engine, run_id) if e[2] == "extracted"]
    assert len(extracted) == 1 and extracted[0][3]["extractor_version"] == 2


# --------------------------------------------------------------------------- real crashes


@pytest.mark.parametrize("crash_at", ["kb_extract_sandbox", "kb_extract_chunk"])
def test_pdf_ingest_recovers_from_a_crash_at_each_new_point(
    worker, tenant, crash_schema, tmp_path, monkeypatch, crash_at
):
    store = KnowledgeStore(worker.engine)
    first = second = None
    dbos_client = None
    try:
        kb_id = create_kb(store, tenant)
        executor_id = f"kb-x-{uuid.uuid4().hex[:8]}"
        first = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_AT=crash_at), tmp_path / "w1.log")
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        doc_id, version, run_id = put(store, tenant, kb_id, pdf=manual_pdf())
        enqueue_kb_ingest(dbos_client, worker.run_store, tenant_id=tenant, kb_id=kb_id,
                          input_json=split_input(doc_id, version), app_version=CRASH_APP_VERSION, run_id=run_id)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()

        at_crash = event_rows(worker.engine, run_id)
        rows_at_crash = chunk_stats(worker.engine, tenant, doc_id)
        if crash_at == "kb_extract_sandbox":  # before any write
            assert rows_at_crash == [] and "extracted" not in [e[2] for e in at_crash]
        else:  # rows and the one event committed
            assert rows_at_crash and [e[2] for e in at_crash].count("extracted") == 1
        assert store.get_document_by_id(tenant, doc_id)["active_version"] is None

        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())

        stats = chunk_stats(worker.engine, tenant, doc_id)
        assert len(stats) == 1
        n = stats[0]["n"]
        assert n > 0 and stats[0]["ordinals"] == n and stats[0]["embedded"] == n
        if rows_at_crash:
            assert rows_at_crash[0]["n"] == n  # no duplicate or replaced rows
        doc = store.get_document_by_id(tenant, doc_id)
        assert (doc["status"], doc["active_version"], doc["chunk_count"]) == ("ready", version, n)
        events = event_rows(worker.engine, run_id)
        assert_contiguous(events)
        assert [e[2] for e in events].count("extracted") == 1
        assert [e[1] for e in events] == ["start", f"extract:{version}", "chunk:0", "embed:batch:0", "finalize:0", "end"]
        with worker.engine.connect() as conn:
            dup = conn.execute(
                text("SELECT count(*) FROM (SELECT ordinal FROM kb_chunks WHERE tenant_id = :t AND document_id = :d"
                     " GROUP BY doc_version, generation, ordinal HAVING count(*) > 1) x"),
                {"t": tenant, "d": doc_id},
            ).scalar_one()
        assert dup == 0
        assert {r["metadata"]["eap"]["extractor"] for r in chunk_rows(worker.engine, tenant, doc_id)} == {"pdf"}
    finally:
        if first is not None:
            first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()
        cleanup_knowledge(worker.engine, tenant)
