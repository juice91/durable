"""Crash recovery of eap.kb_ingest in reindex mode (RM.10): kill a real worker process at
eap.kb_copy (after the copy, before its checkpoint) and inside an eap.kb_embed_batch (before and
after its commit), recover in a fresh process with the same executor id, and check the data.

The knowledge base is first ingested (in-process worker) at generation 1 with `test:hash` (1536),
then reindexed to `test:hash768` (768) in the crashing worker, so each case also checks that the
previous generation stays the searchable one until the switch."""

from __future__ import annotations

import json
import math
import uuid

import pytest
from dbos import DBOSClient

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
    reindex_input,
    upload,
    usage_rows,
)
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture

BATCH = 4


@pytest.mark.parametrize(
    "crash_at,event_key",
    [
        ("kb_chunk", None),  # in eap.kb_copy: chunks copied into G, step not yet checkpointed
        ("kb_batch_before_commit", "embed:batch:1"),
        ("kb_batch_after_commit", "embed:batch:1"),
    ],
)
def test_kb_reindex_recovers_from_crash(worker, client, tenant, crash_schema, tmp_path, monkeypatch, crash_at, event_key):
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", str(BATCH))  # in-process worker and both subprocesses
    store = KnowledgeStore(worker.engine)
    first: WorkerProcess | None = None
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        kb_id = create_kb(store, tenant)
        doc_id, v1, run1 = upload(store, tenant, kb_id, markdown_doc(10, tag="re"))
        enqueue_kb_ingest(
            client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=ingest_input(doc_id, v1),
            app_version=APP_VERSION, run_id=run1,
        )
        assert wait_terminal(worker.run_store, tenant, run1, timeout=60)["status"] == "completed"
        old = chunk_stats(worker.engine, tenant, doc_id)
        assert len(old) == 1
        old_gen, n = old[0]["generation"], old[0]["n"]
        assert n >= 10 and old[0]["embedded"] == n

        kb = store.start_reindex(tenant, kb_id, embedding_model="test:hash768", dimensions=768)
        g = kb["pending_generation"]
        assert g != old_gen

        executor_id = f"kb-reidx-{uuid.uuid4().hex[:8]}"
        extra = {"EAP_CRASH_AT": crash_at}
        if event_key:
            extra["EAP_CRASH_EVENT_KEY"] = event_key
        first = WorkerProcess(_env(crash_schema, executor_id, **extra), tmp_path / "worker1.log")
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_kb_ingest(
            dbos_client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=reindex_input(g),
            app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()

        # At the crash: the old generation is still the searchable one, fully embedded.
        kb = store.get_knowledge_base(tenant, kb_id)
        assert (kb["active_generation"], kb["pending_generation"], kb["dimensions"]) == (old_gen, g, 1536)
        at_crash = {s["generation"]: s for s in chunk_stats(worker.engine, tenant, doc_id)}
        assert at_crash[old_gen]["n"] == n and at_crash[old_gen]["embedded"] == n
        assert at_crash[g]["n"] == n and at_crash[g]["ordinals"] == n  # copied once, no duplicates
        if crash_at == "kb_batch_after_commit":
            assert at_crash[g]["embedded"] == 2 * BATCH  # batches 0 and 1 committed
        elif crash_at == "kb_batch_before_commit":
            assert at_crash[g]["embedded"] == BATCH  # only batch 0 committed
        else:
            assert at_crash[g]["embedded"] == 0

        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
        assert run["status"] == "completed", (run["error"], (tmp_path / "worker2.log").read_text())
        assert json.loads(run["output"])["status"] == "switched"

        # Switched exactly once, to G; only G's rows remain, one per ordinal, all embedded at 768.
        kb = store.get_knowledge_base(tenant, kb_id)
        assert (kb["active_generation"], kb["embedding_model"], kb["dimensions"], kb["status"]) == (
            g, "test:hash768", 768, "ready",
        )
        assert kb["pending_generation"] is None
        stats = chunk_stats(worker.engine, tenant, doc_id)
        assert [(s["generation"], s["n"], s["ordinals"], s["embedded"], s["dims"]) for s in stats] == [
            (g, n, n, n, 768)
        ]

        # One usage row per batch request key, and totals that match them.
        batches = math.ceil(n / BATCH)
        usage = usage_rows(worker.engine, run_id)
        assert [u["request_key"] for u in usage] == sorted(f"embed:batch:{i}" for i in range(batches))
        assert run["input_tokens"] == sum(u["input_tokens"] for u in usage)

        events = event_rows(worker.engine, run_id)
        assert_contiguous(events)
        keys = [e[1] for e in events]
        assert keys == ["start", "chunk:0"] + [f"embed:batch:{i}" for i in range(batches)] + ["finalize:0", "end"]
        assert events[0][3]["mode"] == "reindex" and events[0][3]["generation"] == g
        assert sum(1 for e in events if e[1].startswith("finalize:")) == 1
        assert [e[3]["embedded"] for e in events if e[1].startswith("embed:batch:")][-1] == n
    finally:
        if first is not None:
            first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()
        cleanup_knowledge(worker.engine, tenant)
