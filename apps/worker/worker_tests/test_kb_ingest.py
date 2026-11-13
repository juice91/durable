"""eap.kb_ingest (RM.10) against real Postgres + pgvector and an in-process DBOS worker."""

from __future__ import annotations

import json
import math
import pickle
from collections.abc import Iterator

import pytest

from eap_run_store import KnowledgeStore
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.test_telemetry import dbos_style_log_handler, telemetry  # noqa: F401 - fixtures
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


from eap_worker import embeddings as _embeddings_module  # noqa: E402

_REAL_EMBED = _embeddings_module.embed_documents


@pytest.fixture
def kb_tenant(tenant, worker) -> Iterator[str]:
    yield tenant
    cleanup_knowledge(worker.engine, tenant)


@pytest.fixture
def store(worker) -> KnowledgeStore:
    return KnowledgeStore(worker.engine)


@pytest.fixture
def small_batches(monkeypatch):
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", "4")


def _ingest(client, worker, store, tenant, kb_id, content, **kw):
    doc_id, version, run_id = upload(store, tenant, kb_id, content, **kw)
    enqueue_kb_ingest(
        client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=ingest_input(doc_id, version),
        app_version=APP_VERSION, run_id=run_id,
    )
    return doc_id, version, run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=90)


def test_ingest_embeds_in_batches_and_switches(client, worker, store, kb_tenant, small_batches):
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, run_id, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(10))
    assert run["status"] == "completed", run["error"]

    stats = chunk_stats(worker.engine, kb_tenant, doc_id)
    assert len(stats) == 1
    n = stats[0]["n"]
    assert n >= 10 and stats[0]["embedded"] == n and stats[0]["ordinals"] == n and stats[0]["dims"] == 1536
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert (doc["status"], doc["active_version"], doc["chunk_count"], doc["error"]) == ("ready", version, n, None)

    batches = math.ceil(n / 4)
    usage = usage_rows(worker.engine, run_id)
    assert [u["request_key"] for u in usage] == sorted(f"embed:batch:{i}" for i in range(batches))
    assert all(u["operation"] == "embedding" and u["cost_usd"] is not None for u in usage)

    events = event_rows(worker.engine, run_id)
    assert_contiguous(events)
    assert [e[1] for e in events] == (
        ["start", "chunk:0"] + [f"embed:batch:{i}" for i in range(batches)] + ["finalize:0", "end"]
    )
    assert events[0][3]["mode"] == "ingest" and events[0][3]["doc_version"] == version
    embedded = [e[3]["embedded"] for e in events if e[1].startswith("embed:batch:")]
    assert embedded == sorted(embedded) and embedded[-1] == n
    last = [e for e in events if e[1].startswith("embed:batch:")][-1][3]
    assert last["message"] == f"{n} of {n} sections embedded" and last["total"] == n
    assert events[-1][2] == "run_completed"
    result = json.loads(run["output"])
    assert result["status"] == "switched" and result["chunk_count"] == n and result["batches"] == batches
    # Counts only: no document text in any event.
    assert "handbook explains" not in json.dumps([e[3] for e in events])
    # Usage totals on the run row.
    assert run["input_tokens"] == sum(u["input_tokens"] for u in usage)


def test_new_version_reuses_cached_embeddings(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    content = markdown_doc(6)
    doc_id, _, _, run = _ingest(client, worker, store, kb_tenant, kb_id, content)
    assert run["status"] == "completed", run["error"]
    changed = content.replace("Section 3 of the alpha handbook", "Section 3 of the CHANGED handbook")
    doc_id2, v2, run2_id, run2 = _ingest(client, worker, store, kb_tenant, kb_id, changed)
    assert doc_id2 == doc_id and v2 == 2
    assert run2["status"] == "completed", run2["error"]
    usage = usage_rows(worker.engine, run2_id)
    assert [u["request_key"] for u in usage] == ["embed:batch:0"]
    batch_event = [e for e in event_rows(worker.engine, run2_id) if e[1] == "embed:batch:0"][0][3]
    assert batch_event["inputs"] == 1 and batch_event["chunks"] == 1
    stats = chunk_stats(worker.engine, kb_tenant, doc_id)
    assert [s["doc_version"] for s in stats] == [2]  # version 1's chunks deleted at the switch
    assert store.get_document_by_id(kb_tenant, doc_id)["active_version"] == 2


def test_budget_exceeded_fails_before_any_provider_call(client, worker, store, kb_tenant, monkeypatch):
    monkeypatch.setenv("EAP_KB_MAX_INGEST_TOKENS", "10")
    calls = _count_embed_calls(monkeypatch)
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, run_id, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(3))
    assert run["status"] == "failed"
    assert run["error"].startswith("IngestBudgetExceeded: the ingest needs about ")
    assert calls == [] and usage_rows(worker.engine, run_id) == []
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["status"] == "failed" and doc["error"].startswith("ingest_budget_exceeded: 0 of ")
    end = event_rows(worker.engine, run_id)[-1]
    assert end[2] == "run_failed" and end[3]["error_code"] == "ingest_budget_exceeded"


def _count_embed_calls(monkeypatch, fail_on: set[int] | None = None, on_call=None) -> list[str]:
    """Wrap embeddings.embed_documents (what kb_ingest calls): record request keys, fail call k
    (1-based) with a non-retryable EmbeddingFailed when k is in `fail_on`."""
    from eap_worker import embeddings

    real = _REAL_EMBED
    calls: list[str] = []

    async def wrapper(settings, **kw):
        calls.append(kw["request_key"])
        if on_call is not None:
            on_call(len(calls))
        if fail_on and len(calls) in fail_on:
            raise embeddings.EmbeddingFailed(kw["model_id"], "scripted failure")
        return await real(settings, **kw)

    monkeypatch.setattr(embeddings, "embed_documents", wrapper)
    return calls


def test_partial_failure_keeps_old_version_and_retry_embeds_only_the_rest(
    client, worker, store, kb_tenant, small_batches, monkeypatch
):
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, _, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(4, tag="old"))
    assert run["status"] == "completed", run["error"]

    calls = _count_embed_calls(monkeypatch, fail_on={3})
    _, v2, run_id, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(18, tag="new"))
    assert run["status"] == "failed"
    assert run["error"] == "EmbeddingFailed: the embedding provider request failed"
    assert len(calls) == 3  # not retried: EmbeddingFailed is not transient
    stats = {s["doc_version"]: s for s in chunk_stats(worker.engine, kb_tenant, doc_id)}
    total = stats[v2]["n"]
    assert total >= 18 and stats[v2]["embedded"] == 8
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["status"] == "failed" and doc["active_version"] == 1
    assert doc["error"] == f"embedding_failed: 8 of {total} sections embedded"
    assert stats[1]["embedded"] == stats[1]["n"]  # the previous version is still searchable
    end = event_rows(worker.engine, run_id)[-1][3]
    assert (end["error_code"], end["embedded"], end["total"]) == ("embedding_failed", 8, total)
    assert len(usage_rows(worker.engine, run_id)) == 2

    # Retry (the API's reingest): a new run of the same version embeds only the remainder.
    calls = _count_embed_calls(monkeypatch)
    retry_id = enqueue_kb_ingest(
        client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=ingest_input(doc_id, v2),
        app_version=APP_VERSION,
    )
    retry = wait_terminal(worker.run_store, kb_tenant, retry_id, timeout=90)
    assert retry["status"] == "completed", retry["error"]
    assert len(calls) == math.ceil((total - 8) / 4)
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert (doc["status"], doc["active_version"], doc["chunk_count"], doc["error"]) == ("ready", v2, total, None)
    assert [s["doc_version"] for s in chunk_stats(worker.engine, kb_tenant, doc_id)] == [v2]


def test_document_deleted_mid_run_ends_cancelled(client, worker, store, kb_tenant, small_batches, monkeypatch):
    kb_id = create_kb(store, kb_tenant)

    def delete_on_first(n: int) -> None:
        if n == 1:
            assert store.delete_document(kb_tenant, kb_id, "handbook")

    _count_embed_calls(monkeypatch, on_call=delete_on_first)
    doc_id, _, run_id, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(10))
    assert run["status"] == "failed"
    assert run["error"] == "IngestCancelled: the document was deleted during the ingest"
    end = event_rows(worker.engine, run_id)[-1]
    assert end[2] == "run_failed" and end[3]["error_code"] == "ingest_cancelled"
    assert chunk_stats(worker.engine, kb_tenant, doc_id) == []
    assert_contiguous(event_rows(worker.engine, run_id))


def test_knowledge_base_deleted_before_start_ends_cancelled(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, run_id = upload(store, kb_tenant, kb_id, markdown_doc(2))
    assert store.delete_knowledge_base(kb_tenant, kb_id)
    enqueue_kb_ingest(
        client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=ingest_input(doc_id, version),
        app_version=APP_VERSION, run_id=run_id,
    )
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=60)
    assert run["error"] == "IngestCancelled: the knowledge base was deleted during the ingest"
    assert [e[1] for e in event_rows(worker.engine, run_id)] == ["start", "end"]


def test_reindex_switches_generation_atomically(client, worker, store, kb_tenant, small_batches):
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, _, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(6))
    assert run["status"] == "completed", run["error"]
    n = chunk_stats(worker.engine, kb_tenant, doc_id)[0]["n"]
    kb = store.start_reindex(kb_tenant, kb_id, embedding_model="test:hash768", dimensions=768)
    g = kb["pending_generation"]
    run_id = enqueue_kb_ingest(
        client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=reindex_input(g), app_version=APP_VERSION
    )
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
    assert run["status"] == "completed", run["error"]
    kb = store.get_knowledge_base(kb_tenant, kb_id)
    assert (kb["active_generation"], kb["embedding_model"], kb["dimensions"], kb["status"]) == (g, "test:hash768", 768, "ready")
    assert kb["pending_generation"] is None
    stats = chunk_stats(worker.engine, kb_tenant, doc_id)
    assert [(s["generation"], s["n"], s["embedded"], s["dims"]) for s in stats] == [(g, n, n, 768)]
    events = event_rows(worker.engine, run_id)
    assert_contiguous(events)
    assert events[0][3]["mode"] == "reindex" and events[0][3]["generation"] == g
    assert len(usage_rows(worker.engine, run_id)) == math.ceil(n / 4)
    assert json.loads(run["output"])["status"] == "switched"


def test_transient_and_pickle_safety():
    from eap_worker.kb_ingest import IngestFailed, _transient

    class Orig(Exception):
        sqlstate = "40P01"

    class Wrapped(Exception):
        orig = Orig()

    assert _transient(Wrapped())
    assert not _transient(ValueError("x"))
    from eap_worker.embeddings import EmbeddingFailed

    assert not _transient(EmbeddingFailed("test:hash", "bad"))
    err = pickle.loads(pickle.dumps(IngestFailed("embedding_failed", "EmbeddingFailed", "the request failed")))
    assert (err.code, err.error) == ("embedding_failed", "EmbeddingFailed: the request failed")


def test_reindex_within_dry_run_estimate_is_accepted(client, worker, store, kb_tenant, small_batches, monkeypatch):
    """Duplicated texts count once in the worker's cap, exactly as in the API's dry run."""
    kb_id = create_kb(store, kb_tenant)
    content = markdown_doc(5, tag="shared")
    for i in range(5):  # five documents sharing every section
        _, _, _, run = _ingest(client, worker, store, kb_tenant, kb_id, content, key=f"copy-{i}")
        assert run["status"] == "completed", run["error"]
    kb = store.start_reindex(kb_tenant, kb_id, embedding_model="test:hash768", dimensions=768)
    estimate = store.estimate_reindex(kb_tenant, kb_id)
    assert estimate.chunks >= 25
    monkeypatch.setenv("EAP_KB_MAX_INGEST_TOKENS", str(estimate.approx_tokens))  # exactly the dry run's figure
    calls = _count_embed_calls(monkeypatch)
    run_id = enqueue_kb_ingest(
        client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id,
        input_json=reindex_input(kb["pending_generation"]), app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"])["approx_tokens"] == estimate.approx_tokens
    assert len(calls) == math.ceil(estimate.chunks / 5 / 4)  # each distinct text embedded once
    assert store.get_knowledge_base(kb_tenant, kb_id)["dimensions"] == 768


def test_classify_never_blames_the_provider_for_database_errors():
    from eap_worker.kb_ingest import IngestFailed, _classify

    class Orig(Exception):
        sqlstate = "40P01"

    class Deadlock(Exception):
        orig = Orig()

    assert _classify(ConnectionError("db gone")).code == "internal_error"
    assert _classify(TimeoutError("db slow")).code == "internal_error"
    assert (_classify(Deadlock()).code, _classify(Deadlock()).error_type) == ("internal_error", "DeadlockDetected")
    known = IngestFailed("embedding_failed", "ModelHTTPError", "the embedding provider returned HTTP 503")
    assert _classify(known).error == "ModelHTTPError: the embedding provider returned HTTP 503"


def test_retryable_provider_error_echoing_input_leaks_nothing(
    client, worker, store, kb_tenant, monkeypatch, telemetry, dbos_style_log_handler
):
    """A 5xx whose body echoes the input: retried inside the step, then a text-free failure. No
    document text in the run, its events, exported spans, span events or log records (ours or DBOS's)."""
    from pydantic_ai.exceptions import ModelHTTPError

    from eap_worker import kb_ingest
    from worker_tests.test_telemetry import _run_spans, _wait_for_run_span

    monkeypatch.setattr(kb_ingest, "EMBED_RETRY_SECONDS", 0.01)
    calls: list[int] = []

    async def failing(settings, **kw):
        calls.append(1)
        raise ModelHTTPError(503, kw["model_id"], body={"error": f"overloaded, input was: {kw['texts'][0]}"})

    monkeypatch.setattr("eap_worker.embeddings.embed_documents", failing)
    kb_id = create_kb(store, kb_tenant)
    content = markdown_doc(2, tag="SENTINELSECRET")
    doc_id, _, run_id, run = _ingest(client, worker, store, kb_tenant, kb_id, content)
    assert run["status"] == "failed"
    assert run["error"] == "ModelHTTPError: the embedding provider returned HTTP 503"
    assert len(calls) == 3  # retried in the step, never by DBOS (which would make 9)
    assert "SENTINEL" not in json.dumps([e[3] for e in event_rows(worker.engine, run_id)])
    assert store.get_document_by_id(kb_tenant, doc_id)["error"].startswith("embedding_failed: 0 of ")

    _, finished = telemetry
    spans, logs = _wait_for_run_span(finished, run_id)
    assert _run_spans(spans, run_id)
    for s in spans:
        text = repr(dict(s.attributes or {})) + repr([(e.name, dict(e.attributes or {})) for e in s.events])
        assert "SENTINEL" not in text + repr(s.status.description), s.name
    for r in logs:
        assert "SENTINEL" not in repr(r.body) + repr(dict(r.attributes or {}))


def _drive_with(monkeypatch, *, chunk_results, batch_results, mode="ingest"):
    """Run kb_ingest._drive with its steps replaced by scripted async fakes (no DB, no DBOS)."""
    import asyncio

    from eap_worker import kb_ingest

    chunks, batches = list(chunk_results), list(batch_results)

    async def fake_chunk(*args, **kwargs):
        return chunks.pop(0)

    async def fake_batch(*args, **kwargs):
        return batches.pop(0)

    async def fake_finalize(*args, **kwargs):
        return {"status": "switched", "chunk_count": 2, "pending": 0}

    async def fake_event(*args, **kwargs):
        return 0

    monkeypatch.setattr(kb_ingest, "kb_chunk_step", fake_chunk)
    monkeypatch.setattr(kb_ingest, "kb_embed_batch_step", fake_batch)
    monkeypatch.setattr(kb_ingest, "kb_finalize_step", fake_finalize)
    monkeypatch.setattr(kb_ingest, "append_event_step", fake_event)
    ctx = kb_ingest._Ctx(mode, "00000000-0000-0000-0000-000000000001", 1)
    start = {"outcome": "ok", "generation": 1, "embedding_model": "test:hash"}
    return asyncio.run(kb_ingest._drive("run", "tenant", "kb", ctx, start)), chunks, batches


def _prep(pending: int, *, inserted: int = 2) -> dict:
    return {"status": "ok", "generation": 1, "embedding_model": "test:hash", "total": 2, "embedded": 0,
            "pending_tokens": pending, "limit": 100, "inserted": inserted}


def test_cost_cap_is_per_run_across_rounds(monkeypatch):
    from eap_worker.kb_ingest import IngestFailed

    with pytest.raises(IngestFailed) as err:
        _drive_with(
            monkeypatch,
            chunk_results=[_prep(60), _prep(60)],  # round 0 fits (60 <= 100); round 1: 60 spent + 60 > 100
            batch_results=[
                {"status": "embedded", "embedded": 2, "total": 2, "written": 2, "filled": 0, "tokens": 60},
                {"status": "stale"},
            ],
        )
    assert err.value.code == "ingest_budget_exceeded"
    assert "about 120 embedding tokens, over the limit of 100" in err.value.message


def test_rounds_without_progress_end_not_converged(monkeypatch):
    from eap_worker.kb_ingest import MAX_IDLE_ROUNDS, IngestFailed

    stale = {"status": "stale", "generation": 1, "embedding_model": "test:hash"}
    with pytest.raises(IngestFailed) as err:
        _, left, _ = _drive_with(monkeypatch, chunk_results=[stale] * 3 + [_prep(1)], batch_results=[])
    assert MAX_IDLE_ROUNDS == 3
    assert err.value.code == "ingest_not_converged"
    assert err.value.message == "the ingest made no progress in 3 rounds"


def test_progressing_rounds_do_not_count_as_idle(monkeypatch):
    stale_after_embedding = [
        {"status": "embedded", "embedded": 1, "total": 2, "written": 1, "filled": 0, "tokens": 5},
        {"status": "stale"},
    ]
    result, _, _ = _drive_with(
        monkeypatch,
        chunk_results=[_prep(5)] * 4 + [_prep(0)],
        batch_results=stale_after_embedding * 4 + [{"status": "done", "embedded": 2, "total": 2}],
    )
    assert result["status"] == "switched" and result["embedded"] == 2
