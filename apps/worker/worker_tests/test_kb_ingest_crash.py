"""Crash recovery of eap.kb_ingest (RM.10): kill a real worker process at each ingest crash point
(eap_worker.crash), recover in a fresh process with the same executor id, and check the data.

Version 1 of the document is ingested first (in-process worker), so each case also checks that the
previous version stays the searchable one until the switch."""

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
    upload,
    usage_rows,
)
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture

BATCH = 4


@pytest.mark.parametrize(
    "crash_at,event_key,switched_at_crash",
    [
        ("kb_chunk", None, False),
        ("kb_batch_before_commit", "embed:batch:1", False),
        ("kb_batch_after_commit", "embed:batch:1", False),
        ("kb_before_finalize", None, False),
        ("kb_finalize", None, True),
    ],
)
def test_kb_ingest_recovers_from_crash(
    worker, client, tenant, crash_schema, tmp_path, monkeypatch, crash_at, event_key, switched_at_crash
):
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", str(BATCH))  # in-process worker and both subprocesses
    store = KnowledgeStore(worker.engine)
    first: WorkerProcess | None = None
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        kb_id = create_kb(store, tenant)
        doc_id, v1, run1 = upload(store, tenant, kb_id, markdown_doc(3, tag="old"))
        enqueue_kb_ingest(
            client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=ingest_input(doc_id, v1),
            app_version=APP_VERSION, run_id=run1,
        )
        assert wait_terminal(worker.run_store, tenant, run1, timeout=60)["status"] == "completed"
        v1_chunks = chunk_stats(worker.engine, tenant, doc_id)[0]["n"]

        executor_id = f"kb-crash-{uuid.uuid4().hex[:8]}"
        extra = {"EAP_CRASH_AT": crash_at}
        if event_key:
            extra["EAP_CRASH_EVENT_KEY"] = event_key
        first = WorkerProcess(_env(crash_schema, executor_id, **extra), tmp_path / "worker1.log")
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        _, v2, run_id = upload(store, tenant, kb_id, markdown_doc(10, tag="new"))
        enqueue_kb_ingest(
            dbos_client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=ingest_input(doc_id, v2),
            app_version=CRASH_APP_VERSION, run_id=run_id,
        )
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()

        # At the crash: version 1 is the searchable one unless the switch had committed.
        doc = store.get_document_by_id(tenant, doc_id)
        assert doc["active_version"] == (v2 if switched_at_crash else v1)
        if not switched_at_crash:
            v1_rows = [s for s in chunk_stats(worker.engine, tenant, doc_id) if s["doc_version"] == v1]
            assert v1_rows and v1_rows[0]["embedded"] == v1_chunks

        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
        assert run["status"] == "completed", (run["error"], (tmp_path / "worker2.log").read_text())

        # Exactly the expected chunk rows: version 2 only, one per ordinal, all embedded.
        stats = chunk_stats(worker.engine, tenant, doc_id)
        assert len(stats) == 1 and stats[0]["doc_version"] == v2
        n = stats[0]["n"]
        assert n >= 10 and stats[0]["ordinals"] == n and stats[0]["embedded"] == n
        doc = store.get_document_by_id(tenant, doc_id)
        assert (doc["status"], doc["active_version"], doc["chunk_count"]) == ("ready", v2, n)

        # One usage row per batch request key, and totals that match them.
        batches = math.ceil(n / BATCH)
        usage = usage_rows(worker.engine, run_id)
        assert [u["request_key"] for u in usage] == sorted(f"embed:batch:{i}" for i in range(batches))
        assert run["input_tokens"] == sum(u["input_tokens"] for u in usage)

        events = event_rows(worker.engine, run_id)
        assert_contiguous(events)
        assert [e[1] for e in events] == (
            ["start", "chunk:0"] + [f"embed:batch:{i}" for i in range(batches)] + ["finalize:0", "end"]
        )
        assert [e[3]["embedded"] for e in events if e[1].startswith("embed:batch:")][-1] == n
        assert json.loads(run["output"])["chunk_count"] == n
    finally:
        if first is not None:
            first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()
        cleanup_knowledge(worker.engine, tenant)
