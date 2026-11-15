"""P1.C5 acceptance, bounds. The accepted decision: the `eap.kb_embed_call` checkpoint of a full
64 x 1,536 batch is at most 512 KiB pickled (about 540 KiB as DBOS stores it: base64 text), and
EAP_KB_EMBED_BATCH_CHUNKS above 128 means 128. Measured on the real checkpoints of real runs."""

from __future__ import annotations

import asyncio
import base64
import json
import pickle
from collections.abc import Iterator

import psycopg
import pytest

from eap_run_store import KnowledgeStore
from eap_worker import kb_ingest
from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, wait_terminal
from worker_tests.kb_ingest_helpers import (
    chunk_stats,
    cleanup_knowledge,
    create_kb,
    enqueue_kb_ingest,
    ingest_input,
    markdown_doc,
    upload,
)
from worker_tests.test_kb_embed_split import split_input

PICKLED_MAX = 512 * 1024
STORED_MAX = 540 * 1024


@pytest.fixture
def kb_tenant(tenant, worker) -> Iterator[str]:
    yield tenant
    cleanup_knowledge(worker.engine, tenant)


def _call_checkpoints(schema: str, run_id) -> list[tuple[int, dict]]:
    """(stored size in bytes, unpickled output) of each eap.kb_embed_call checkpoint of the run."""
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        rows = conn.execute(
            f'SELECT output FROM "{schema}".operation_outputs WHERE workflow_uuid = %s'
            " AND function_name = 'eap.kb_embed_call' ORDER BY function_id", (str(run_id),)).fetchall()
    return [(len(raw), pickle.loads(base64.b64decode(raw))) for (raw,) in rows if raw]


def _ingest(client, worker, tenant, content: str):
    store = KnowledgeStore(worker.engine)
    kb_id = create_kb(store, tenant)
    doc_id, version, run_id = upload(store, tenant, kb_id, content)
    enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=split_input(doc_id, version),
                      app_version=APP_VERSION, run_id=run_id)
    return doc_id, run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=120)


def test_a_full_64_by_1536_batch_checkpoint_is_within_the_accepted_bound(client, worker, worker_settings, kb_tenant,
                                                                         monkeypatch):
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", "64")
    doc_id, run_id, run = _ingest(client, worker, kb_tenant, markdown_doc(70))
    assert run["status"] == "completed", run["error"]
    called = [c for c in _call_checkpoints(worker_settings.dbos_system_schema, run_id) if c[1]["status"] == "called"]
    stored, output = max(called, key=lambda c: len(c[1]["chunk_ids"]))
    assert len(output["chunk_ids"]) == 64, [len(c[1]["chunk_ids"]) for c in called]
    assert output["dimensions"] == 1536 and len(output["vectors"]) == 64 * 1536 * 4  # float32 bytes
    assert output["vector_format"] == "f32" and isinstance(output["vectors"], bytes)
    assert len(pickle.dumps(output)) <= PICKLED_MAX
    assert stored <= STORED_MAX, stored  # as DBOS stores it
    assert chunk_stats(worker.engine, kb_tenant, doc_id)[0]["embedded"] > 64


def test_the_checkpoint_of_a_fresh_call_step_pickles_within_the_bound(worker, kb_tenant, monkeypatch):
    """The step called directly: its returned dict, pickled as DBOS pickles it."""
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", "64")
    store = KnowledgeStore(worker.engine)
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, run_id = upload(store, kb_tenant, kb_id, markdown_doc(70))
    worker.run_store.create_kb_ingest_run(kb_tenant, kb_id, split_input(doc_id, version), run_id)
    chunked = asyncio.run(kb_ingest.kb_chunk_step(str(run_id), kb_tenant, kb_id, doc_id, version))
    assert chunked["total"] >= 64, chunked
    out = asyncio.run(kb_ingest.kb_embed_call_step(
        str(run_id), kb_tenant, kb_id, "ingest", chunked["generation"], "test:hash", 0, 0, chunked["total"],
        document_id=doc_id, doc_version=version))
    assert out["status"] == "called" and len(out["chunk_ids"]) == 64
    pickled = pickle.dumps(out)
    assert len(pickled) <= PICKLED_MAX and len(base64.b64encode(pickled)) <= STORED_MAX
    assert "handbook" not in json.dumps({k: v for k, v in out.items() if k != "vectors"}, default=str)  # no chunk text


def test_a_batch_setting_above_128_runs_in_batches_of_at_most_128(client, worker, worker_settings, kb_tenant,
                                                                  monkeypatch):
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", "100000")
    assert kb_ingest._batch_chunks() == 128 == kb_ingest.MAX_BATCH_CHUNKS
    doc_id, run_id, run = _ingest(client, worker, kb_tenant, markdown_doc(140))
    assert run["status"] == "completed", run["error"]
    called = [c for c in _call_checkpoints(worker_settings.dbos_system_schema, run_id) if c[1]["status"] == "called"]
    sizes = [len(c[1]["chunk_ids"]) for c in called]
    assert max(sizes) == 128 and sum(sizes) == chunk_stats(worker.engine, kb_tenant, doc_id)[0]["n"], sizes
    assert all(stored < 1.1 * 1024 * 1024 for stored, _ in called)  # < 1 MiB each, as stored


@pytest.mark.parametrize(("value", "expected"), [("128", 128), ("129", 128), ("1", 1), ("0", 1), ("-4", 1),
                                                 ("64", 64), ("", 64)])
def test_the_batch_chunk_setting_is_clamped(monkeypatch, value, expected):
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", value)
    assert kb_ingest._batch_chunks() == expected


def test_a_non_numeric_batch_setting_fails_loudly(monkeypatch):
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", "lots")
    with pytest.raises(ValueError, match="EAP_KB_EMBED_BATCH_CHUNKS"):
        kb_ingest._batch_chunks()
