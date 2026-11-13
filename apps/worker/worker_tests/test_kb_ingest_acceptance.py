"""Acceptance tests of eap.kb_ingest (RM.10): the API -> worker path, versions, cache and cost,
retries, reindex, concurrency, and that no document text leaks. Complements test_kb_ingest.py and
test_kb_ingest_crash.py (not duplicated here)."""

from __future__ import annotations

import asyncio
import json
import math
import os
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from pydantic_ai.exceptions import ModelHTTPError

from eap_run_store import KnowledgeStore
from worker_tests.conftest import APP_DB_URL, APP_VERSION, DBOS_DB_URL, event_rows, wait_for, wait_terminal
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
from worker_tests.test_kb_ingest import _count_embed_calls, _REAL_EMBED
from worker_tests.test_telemetry import _run_spans, _wait_for_run_span, telemetry  # noqa: F401 - fixture


def auth(tenant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer dev:{tenant}:alice"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    monkeypatch.delenv("EAP_KB_MAX_INGEST_TOKENS", raising=False)
    monkeypatch.delenv("EAP_KB_EMBED_BATCH_CHUNKS", raising=False)


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


@pytest.fixture
def api(worker, worker_settings) -> Iterator:
    """The real API app (TestClient) whose launcher enqueues on the worker's DBOS schema."""
    from dbos import DBOSClient
    from fastapi.testclient import TestClient

    from eap_api.app import create_app
    from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

    previous = os.environ.get("EAP_AUTH_MODE")
    os.environ["EAP_AUTH_MODE"] = "dev"
    dbos = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=worker_settings.dbos_system_schema)
    app = create_app(database_url=APP_DB_URL)
    launcher = DBOSRunLauncher(LauncherSettings(DBOS_DB_URL, worker_settings.dbos_system_schema, APP_VERSION), client=dbos)
    app.dependency_overrides[get_run_launcher] = lambda: launcher
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        dbos.destroy()
        app.state.database_engine.dispose()
        if previous is None:
            os.environ.pop("EAP_AUTH_MODE", None)
        else:
            os.environ["EAP_AUTH_MODE"] = previous


# ----------------------------------------------------------------------------- helpers


def query_vector(worker, model: str = "test:hash") -> list[float]:
    from eap_worker.embeddings import embed_query

    call = asyncio.run(
        embed_query(worker.settings, tenant_id="t_q", run_id=str(uuid.uuid4()), request_key="q", model_id=model,
                    text="what does the handbook explain")
    )
    return list(call.vector)


def hits(store, tenant, kb_id, vec, model="test:hash", **kw):
    return store.search(tenant, [kb_id], vec, embedding_model=model, top_k=100, **kw)


def run_ingest(client, worker, store, tenant, kb_id, content, *, key="handbook", metadata=None, timeout=90):
    doc_id, version, run_id = upload(store, tenant, kb_id, content, key=key, metadata=metadata)
    enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id,
                      input_json=ingest_input(doc_id, version), app_version=APP_VERSION, run_id=run_id)
    return doc_id, version, run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=timeout)


def progress_events(engine, run_id):
    return [e for e in event_rows(engine, run_id) if e[2] == "ingest_progress"]


def patch_embed(monkeypatch, behavior):
    """Wrap embeddings.embed_documents: `behavior(n, kw)` may raise before the real call (n is 1-based)."""
    from eap_worker import embeddings

    calls: list[str] = []

    async def wrapper(settings, **kw):
        calls.append(kw["request_key"])
        behavior(len(calls), kw)
        return await _REAL_EMBED(settings, **kw)

    monkeypatch.setattr(embeddings, "embed_documents", wrapper)
    return calls


# ----------------------------------------------------------------------------- API -> worker


def test_api_upload_runs_worker_and_makes_document_searchable(api, worker, store, kb_tenant):
    headers = auth(kb_tenant)
    r = api.post("/api/v1/knowledge-bases", json={"name": "Docs", "embedding_model": "test:hash"}, headers=headers)
    assert r.status_code == 201, r.text
    kb_id = r.json()["id"]
    put = api.put(
        f"/api/v1/knowledge-bases/{kb_id}/documents/handbook.md",
        json={"content": markdown_doc(200), "mime_type": "text/markdown", "metadata": {"team": "support"}},
        headers=headers,
    )
    assert put.status_code == 202, put.text
    run_id = put.json()["run_id"]
    doc_id = put.json()["document"]["id"]

    run = wait_terminal(worker.run_store, kb_tenant, uuid.UUID(run_id), timeout=120)
    assert run["status"] == "completed", run["error"]
    assert run["kind"] == "kb_ingest"
    doc = store.get_document_by_id(kb_tenant, doc_id)
    n = doc["chunk_count"]
    assert doc["status"] == "ready" and doc["active_version"] == 1 and doc["error"] is None and n >= 200

    events = event_rows(worker.engine, run_id)
    assert_contiguous(events)
    batches = math.ceil(n / 64)
    assert [e[1] for e in events] == (
        ["start", "chunk:0"] + [f"embed:batch:{i}" for i in range(batches)] + ["finalize:0", "end"]
    )
    embedded = [e[3]["embedded"] for e in progress_events(worker.engine, run_id) if e[1].startswith("embed:")]
    assert embedded == sorted(embedded) and len(set(embedded)) == batches and embedded[-1] == n
    assert all(e[3]["total"] == n for e in progress_events(worker.engine, run_id) if e[1] != "finalize:0")
    assert len(usage_rows(worker.engine, run_id)) == batches

    found = hits(store, kb_tenant, kb_id, query_vector(worker))
    assert len(found) == 100 and {h.doc_version for h in found} == {1}
    assert {h.document_key for h in found} == {"handbook.md"}
    assert all(h.metadata.get("team") == "support" for h in found)
    assert hits(store, kb_tenant, kb_id, query_vector(worker), metadata_filter={"team": "sales"}) == []
    listed = api.get(f"/api/v1/knowledge-bases/{kb_id}/documents", headers=headers).json()
    assert [(d["document_key"], d["status"], d["chunk_count"]) for d in listed["items"]] == [("handbook.md", "ready", n)]


# ----------------------------------------------------------------------------- versions


def test_search_serves_v1_during_v2_ingest_then_v2_only(client, worker, store, kb_tenant, small_batches, monkeypatch):
    kb_id = create_kb(store, kb_tenant)
    vec = query_vector(worker)
    _, _, _, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(6, tag="old"))
    assert run["status"] == "completed", run["error"]
    seen: list[set[int]] = []
    calls = patch_embed(monkeypatch, lambda n, kw: seen.append({h.doc_version for h in hits(store, kb_tenant, kb_id, vec)}))
    _, v2, _, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(9, tag="new"))
    assert run["status"] == "completed", run["error"]
    assert v2 == 2
    assert len(calls) >= 2 and seen == [{1}] * len(calls)  # every batch of v2 ran while only v1 was visible
    assert {h.doc_version for h in hits(store, kb_tenant, kb_id, vec)} == {2}


def test_failed_v2_keeps_v1_searchable_and_shows_progress(client, worker, store, kb_tenant, small_batches, monkeypatch):
    kb_id = create_kb(store, kb_tenant)
    vec = query_vector(worker)
    doc_id, _, _, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(5, tag="old"))
    assert run["status"] == "completed", run["error"]
    v1_hits = {h.chunk_id for h in hits(store, kb_tenant, kb_id, vec)}
    _count_embed_calls(monkeypatch, fail_on={2})
    _, v2, _, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(12, tag="new"))
    assert run["status"] == "failed"
    total = chunk_stats(worker.engine, kb_tenant, doc_id)[-1]["n"]
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["status"] == "failed" and doc["error"] == f"embedding_failed: 4 of {total} sections embedded"
    assert {h.chunk_id for h in hits(store, kb_tenant, kb_id, vec)} == v1_hits  # unchanged, no v2 leak


# ----------------------------------------------------------------------------- cache and cost


def test_unchanged_sections_are_not_re_embedded_and_usage_sums_only_new_ones(client, worker, store, kb_tenant, small_batches, monkeypatch):
    kb_id = create_kb(store, kb_tenant)
    content = markdown_doc(10)
    _, _, run1, run = run_ingest(client, worker, store, kb_tenant, kb_id, content)
    assert run["status"] == "completed", run["error"]
    u1 = usage_rows(worker.engine, run1)
    calls = _count_embed_calls(monkeypatch)
    changed = content.replace("Section 2 of the alpha", "Section 2 of the NEW").replace("Section 7 of the alpha", "Section 7 of the NEW")
    _, _, run2, run = run_ingest(client, worker, store, kb_tenant, kb_id, changed)
    assert run["status"] == "completed", run["error"]
    assert len(calls) == 1  # 2 changed sections fit one batch
    u2 = usage_rows(worker.engine, run2)
    assert len(u2) == 1 and u2[0]["operation"] == "embedding"
    event = [e for e in event_rows(worker.engine, run2) if e[1] == "embed:batch:0"][0][3]
    assert event["inputs"] == 2
    assert 0 < u2[0]["input_tokens"] < sum(u["input_tokens"] for u in u1) / 3
    assert run["input_tokens"] == u2[0]["input_tokens"]  # run total counts only the new embeddings


def test_metadata_only_change_makes_no_provider_call(client, worker, store, kb_tenant, monkeypatch):
    kb_id = create_kb(store, kb_tenant)
    vec = query_vector(worker)
    content = markdown_doc(5)
    _, _, _, run = run_ingest(client, worker, store, kb_tenant, kb_id, content, metadata={"team": "a"})
    assert run["status"] == "completed", run["error"]
    calls = _count_embed_calls(monkeypatch)
    _, v2, run2, run = run_ingest(client, worker, store, kb_tenant, kb_id, content, metadata={"team": "b"})
    assert run["status"] == "completed", run["error"]
    assert v2 == 2
    assert calls == [] and usage_rows(worker.engine, run2) == []
    found = hits(store, kb_tenant, kb_id, vec)
    assert {(h.doc_version, h.metadata["team"]) for h in found} == {(2, "b")}


def test_cost_cap_counts_only_uncached_tokens(client, worker, store, kb_tenant, monkeypatch):
    kb_id = create_kb(store, kb_tenant)
    content = markdown_doc(10)
    _, _, run1, run = run_ingest(client, worker, store, kb_tenant, kb_id, content)
    assert run["status"] == "completed", run["error"]
    full = event_rows(worker.engine, run1)[1][3]["pending_tokens"]
    assert full > 10
    monkeypatch.setenv("EAP_KB_MAX_INGEST_TOKENS", str(full - 1))  # v1 itself would now be refused
    calls = _count_embed_calls(monkeypatch)
    changed = content.replace("Section 4 of the alpha", "Section 4 of the NEW")
    _, _, run2, run = run_ingest(client, worker, store, kb_tenant, kb_id, changed)
    assert run["status"] == "completed", run["error"]  # only the uncached section counts
    assert len(calls) == 1
    other = markdown_doc(14, tag="beta")  # nothing cached: over the cap
    calls.clear()
    _, _, run3, run = run_ingest(client, worker, store, kb_tenant, kb_id, other)
    assert run["status"] == "failed" and run["error"].startswith("IngestBudgetExceeded")
    assert calls == [] and usage_rows(worker.engine, run3) == []


def test_usage_rows_carry_operation_and_cost(client, worker, store, kb_tenant, small_batches):
    kb_id = create_kb(store, kb_tenant)
    _, _, run_id, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(6))
    assert run["status"] == "completed", run["error"]
    usage = usage_rows(worker.engine, run_id)
    assert usage and all(u["operation"] == "embedding" and u["input_tokens"] > 0 for u in usage)
    for u in usage:  # test:hash costs 0.13 USD per million input tokens (config/model-prices.test.json)
        assert Decimal(str(u["cost_usd"])) == pytest.approx(Decimal(u["input_tokens"]) * Decimal("0.13") / 1_000_000, rel=Decimal("0.001"))
    batch_events = [e[3] for e in progress_events(worker.engine, run_id) if e[1].startswith("embed:batch:")]
    assert all(e["input_tokens"] > 0 for e in batch_events)


# ----------------------------------------------------------------------------- retry policy


def test_dimension_mismatch_fails_at_once_with_one_provider_call(client, worker, store, kb_tenant, monkeypatch):
    kb_id = create_kb(store, kb_tenant, model="test:hash", dimensions=768)  # test:hash returns 1536
    calls = _count_embed_calls(monkeypatch)
    doc_id, _, run_id, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(3))
    assert run["status"] == "failed" and run["error"].startswith("EmbeddingDimensionMismatch:")
    assert len(calls) == 1
    total = chunk_stats(worker.engine, kb_tenant, doc_id)[0]["n"]
    assert store.get_document_by_id(kb_tenant, doc_id)["error"] == f"embedding_dimension_mismatch: 0 of {total} sections embedded"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "embedding_dimension_mismatch"


def test_503_is_retried_then_succeeds(client, worker, store, kb_tenant, monkeypatch):
    def flaky(n, kw):
        if n <= 2:
            raise ModelHTTPError(503, "test:hash", body="upstream unavailable")

    calls = patch_embed(monkeypatch, flaky)
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, run_id, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(4))
    assert run["status"] == "completed", run["error"]
    assert calls == ["embed:batch:0"] * 3  # two 503s, then the third attempt of the step
    assert [u["request_key"] for u in usage_rows(worker.engine, run_id)] == ["embed:batch:0"]
    assert store.get_document_by_id(kb_tenant, doc_id)["status"] == "ready"


def test_503_exhausts_three_attempts_and_fails_with_embedding_failed(client, worker, store, kb_tenant, monkeypatch):
    def down(n, kw):
        raise ModelHTTPError(503, "test:hash", body="upstream unavailable")

    calls = patch_embed(monkeypatch, down)
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, run_id, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(4))
    assert run["status"] == "failed" and len(calls) == 3
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "embedding_failed"
    assert store.get_document_by_id(kb_tenant, doc_id)["error"].startswith("embedding_failed: 0 of ")
    assert usage_rows(worker.engine, run_id) == []


def test_client_error_400_is_not_retried(client, worker, store, kb_tenant, monkeypatch):
    def bad(n, kw):
        raise ModelHTTPError(400, "test:hash", body="rejected")

    calls = patch_embed(monkeypatch, bad)
    kb_id = create_kb(store, kb_tenant)
    _, _, _, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(3))
    assert run["status"] == "failed" and len(calls) == 1
    assert "HTTP 400" in run["error"]


# ----------------------------------------------------------------------------- reindex


def _start_reindex(client, worker, store, tenant, kb_id, model="test:hash768", dims=768):
    kb = store.start_reindex(tenant, kb_id, embedding_model=model, dimensions=dims)
    g = kb["pending_generation"]
    run_id = enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id,
                               input_json=reindex_input(g), app_version=APP_VERSION)
    return g, run_id


def test_reindex_searches_old_generation_until_switch_then_new(client, worker, store, kb_tenant, small_batches, monkeypatch):
    kb_id = create_kb(store, kb_tenant)
    old_vec, new_vec = query_vector(worker), query_vector(worker, "test:hash768")
    doc_id, _, _, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(6))
    assert run["status"] == "completed", run["error"]
    old_gen = store.get_knowledge_base(kb_tenant, kb_id)["active_generation"]
    during: list[tuple[set[int], int]] = []
    calls = patch_embed(monkeypatch, lambda n, kw: during.append((
        {h.generation for h in hits(store, kb_tenant, kb_id, old_vec)},
        len(hits(store, kb_tenant, kb_id, new_vec, model="test:hash768")),
    )))
    g, run_id = _start_reindex(client, worker, store, kb_tenant, kb_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
    assert run["status"] == "completed", run["error"]
    assert calls and during == [({old_gen}, 0)] * len(calls)  # old model and generation served every time
    assert {h.generation for h in hits(store, kb_tenant, kb_id, new_vec, model="test:hash768")} == {g}
    assert hits(store, kb_tenant, kb_id, old_vec) == []  # the old model no longer matches the KB
    assert [s["generation"] for s in chunk_stats(worker.engine, kb_tenant, doc_id)] == [g]


def test_reindex_failure_leaves_reindex_failed_and_old_generation_searchable(client, worker, store, kb_tenant, small_batches, monkeypatch):
    kb_id = create_kb(store, kb_tenant)
    old_vec = query_vector(worker)
    _, _, _, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(6))
    assert run["status"] == "completed", run["error"]
    before = {h.chunk_id for h in hits(store, kb_tenant, kb_id, old_vec)}
    _count_embed_calls(monkeypatch, fail_on={2})
    g, run_id = _start_reindex(client, worker, store, kb_tenant, kb_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
    assert run["status"] == "failed"
    kb = store.get_knowledge_base(kb_tenant, kb_id)
    assert kb["status"] == "reindex_failed" and kb["pending_generation"] == g
    assert kb["error"].startswith("embedding_failed: 4 of ") and kb["error"].endswith(" sections embedded")
    assert (kb["embedding_model"], kb["dimensions"]) == ("test:hash", 1536)
    assert {h.chunk_id for h in hits(store, kb_tenant, kb_id, old_vec)} == before
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "embedding_failed"


def test_cancel_reindex_mid_run_ends_ingest_cancelled(client, worker, store, kb_tenant, small_batches, monkeypatch):
    kb_id = create_kb(store, kb_tenant)
    old_vec = query_vector(worker)
    doc_id, _, _, run = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(6))
    assert run["status"] == "completed", run["error"]
    before = {h.chunk_id for h in hits(store, kb_tenant, kb_id, old_vec)}
    patch_embed(monkeypatch, lambda n, kw: n == 1 and store.cancel_reindex(kb_tenant, kb_id))
    g, run_id = _start_reindex(client, worker, store, kb_tenant, kb_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
    assert run["status"] == "failed" and run["error"] == "IngestCancelled: the reindex was cancelled"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "ingest_cancelled"
    kb = store.get_knowledge_base(kb_tenant, kb_id)
    assert (kb["status"], kb["pending_generation"], kb["embedding_model"]) == ("ready", None, "test:hash")
    assert {h.chunk_id for h in hits(store, kb_tenant, kb_id, old_vec)} == before
    assert [s["generation"] for s in chunk_stats(worker.engine, kb_tenant, doc_id)] == [kb["active_generation"]]
    assert_contiguous(event_rows(worker.engine, run_id))


# ----------------------------------------------------------------------------- concurrency


def test_two_documents_ingest_in_parallel_in_one_kb(client, worker, store, kb_tenant, small_batches):
    kb_id = create_kb(store, kb_tenant)
    vec = query_vector(worker)
    runs = {}
    for key, tag in (("a", "aaa"), ("b", "bbb")):
        doc_id, version, run_id = upload(store, kb_tenant, kb_id, markdown_doc(12, tag=tag), key=key)
        runs[key] = (doc_id, run_id)
        enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id,
                          input_json=ingest_input(doc_id, version), app_version=APP_VERSION, run_id=run_id)
    for key, (doc_id, run_id) in runs.items():
        run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=120)
        assert run["status"] == "completed", run["error"]
        n = chunk_stats(worker.engine, kb_tenant, doc_id)[0]["n"]
        doc = store.get_document_by_id(kb_tenant, doc_id)
        assert (doc["status"], doc["chunk_count"], doc["error"]) == ("ready", n, None)
        assert chunk_stats(worker.engine, kb_tenant, doc_id)[0]["embedded"] == n
        events = event_rows(worker.engine, run_id)
        assert_contiguous(events)
        assert [e[1] for e in events][-2:] == ["finalize:0", "end"]
        assert len(usage_rows(worker.engine, run_id)) == math.ceil(n / 4)
    found = hits(store, kb_tenant, kb_id, vec)
    assert {h.document_key for h in found} == {"a", "b"} and {h.doc_version for h in found} == {1}
    total = sum(chunk_stats(worker.engine, kb_tenant, d)[0]["n"] for d, _ in runs.values())
    assert len(found) == total


def test_reupload_during_ingest_supersedes_the_older_run(client, worker, store, kb_tenant, small_batches, monkeypatch):
    kb_id = create_kb(store, kb_tenant)
    vec = query_vector(worker)
    state: dict = {}

    def reupload(n, kw):
        if n == 1:
            doc_id, v2, run2 = upload(store, kb_tenant, kb_id, markdown_doc(8, tag="two"))
            state.update(doc_id=doc_id, v2=v2, run2=run2)
            enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id,
                              input_json=ingest_input(doc_id, v2), app_version=APP_VERSION, run_id=run2)

    patch_embed(monkeypatch, reupload)
    doc_id, v1, run1, r1 = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(10, tag="one"))
    r2 = wait_terminal(worker.run_store, kb_tenant, state["run2"], timeout=120)
    assert state["v2"] == v1 + 1
    assert r1["status"] == "completed" and r2["status"] == "completed", (r1["error"], r2["error"])
    assert json.loads(r1["output"])["status"] in ("switched", "superseded")  # depends on who finishes first
    assert json.loads(r2["output"])["status"] == "switched"
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert (doc["status"], doc["active_version"], doc["error"]) == ("ready", state["v2"], None)
    assert [s["doc_version"] for s in chunk_stats(worker.engine, kb_tenant, doc_id)] == [state["v2"]]
    assert {h.doc_version for h in hits(store, kb_tenant, kb_id, vec)} == {state["v2"]}
    for rid in (run1, state["run2"]):
        assert_contiguous(event_rows(worker.engine, rid))


def test_older_run_finishing_after_the_newer_switch_is_superseded(client, worker, store, kb_tenant, small_batches, monkeypatch):
    """v1's embedding is held while v2 (uploaded meanwhile) runs to completion: v1 then ends superseded,
    switches nothing, and leaves no v1 chunks behind."""
    from eap_worker import embeddings

    kb_id = create_kb(store, kb_tenant)
    vec = query_vector(worker)
    state: dict = {}

    async def hold(settings, **kw):
        if "run2" not in state:  # v1's first batch: upload v2, start its run, wait until v2 is active
            doc_id, v2, run2 = upload(store, kb_tenant, kb_id, markdown_doc(5, tag="two"))
            state.update(doc_id=doc_id, v2=v2, run2=run2)
            enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id,
                              input_json=ingest_input(doc_id, v2), app_version=APP_VERSION, run_id=run2)
            for _ in range(300):
                doc = await asyncio.to_thread(store.get_document_by_id, kb_tenant, doc_id)
                if doc["active_version"] == v2:
                    break
                await asyncio.sleep(0.2)
        return await _REAL_EMBED(settings, **kw)

    monkeypatch.setattr(embeddings, "embed_documents", hold)
    doc_id, v1, run1, r1 = run_ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(10, tag="one"), timeout=120)
    assert r1["status"] == "completed", r1["error"]
    assert json.loads(r1["output"]) == {"mode": "ingest", "status": "superseded"}
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert (doc["status"], doc["active_version"]) == ("ready", state["v2"])
    assert [s["doc_version"] for s in chunk_stats(worker.engine, kb_tenant, doc_id)] == [state["v2"]]
    assert {h.doc_version for h in hits(store, kb_tenant, kb_id, vec)} == {state["v2"]}
    assert "finalize:0" not in [e[1] for e in event_rows(worker.engine, run1)]
    assert_contiguous(event_rows(worker.engine, run1))


# ----------------------------------------------------------------------------- no text in events and spans

SECRET = "ZEBRA-SECRET-7731"


def _secret_doc() -> str:
    return markdown_doc(4).replace("Section 1 of the alpha handbook explains", f"Section 1 {SECRET} explains")


def _assert_no_secret(engine, run_id, finished, run):
    dump = json.dumps([e[3] for e in event_rows(engine, run_id)]) + json.dumps(usage_rows(engine, run_id), default=str)
    dump += json.dumps({k: v for k, v in run.items() if k not in ("created_at", "updated_at")}, default=str)
    spans, logs = finished()
    dump += repr([(s.name, dict(s.attributes or {}), [(e.name, dict(e.attributes or {})) for e in s.events]) for s in spans])
    dump += repr([(r.body, dict(r.attributes or {})) for r in logs])
    assert SECRET not in dump


def test_no_document_text_in_events_usage_spans_or_logs(client, worker, store, kb_tenant, telemetry):
    _, finished = telemetry[0], telemetry[1]
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, run_id, run = run_ingest(client, worker, store, kb_tenant, kb_id, _secret_doc())
    assert run["status"] == "completed", run["error"]
    spans, _ = _wait_for_run_span(finished, run_id)
    assert _run_spans(spans, run_id)[0].attributes["eap.run_kind"] == "kb_ingest"
    _assert_no_secret(worker.engine, run_id, finished, run)
    assert store.get_document_by_id(kb_tenant, doc_id)["error"] is None


def test_no_document_text_when_the_provider_error_echoes_it(client, worker, store, kb_tenant, telemetry, monkeypatch):
    _, finished = telemetry[0], telemetry[1]

    def echo(n, kw):
        raise ModelHTTPError(400, "test:hash", body=f"input rejected: {kw['texts'][0]} {SECRET}")

    patch_embed(monkeypatch, echo)
    kb_id = create_kb(store, kb_tenant)
    doc_id, _, run_id, run = run_ingest(client, worker, store, kb_tenant, kb_id, _secret_doc())
    assert run["status"] == "failed"
    _wait_for_run_span(finished, run_id)
    _assert_no_secret(worker.engine, run_id, finished, run)
    assert SECRET not in (store.get_document_by_id(kb_tenant, doc_id)["error"] or "")


# ----------------------------------------------------------------------------- extraction failures


def test_unsupported_mime_type_fails_with_extraction_failed(client, worker, store, kb_tenant):
    kb_id = create_kb(store, kb_tenant)
    run_id = uuid.uuid4()
    result = store.put_document(kb_tenant, kb_id, document_key="blob", title="Blob", content="%PDF-1.4 text",
                                mime_type="application/pdf", metadata={}, run_id=str(run_id))
    doc_id, v = result.document["id"], result.document["latest_version"]
    enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=ingest_input(doc_id, v),
                      app_version=APP_VERSION, run_id=run_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=60)
    assert run["status"] == "failed" and run["error"].startswith("UnsupportedMimeType:")
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert doc["status"] == "failed" and doc["error"] == "extraction_failed: 0 of 0 sections embedded"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "extraction_failed"
    assert usage_rows(worker.engine, run_id) == []


def test_empty_extraction_fails_and_never_goes_ready(client, worker, store, kb_tenant):
    """Only reachable around the API (it answers 422 'no extractable text'): a document that extracts
    to nothing must fail, not become a 'ready' document with 0 chunks."""
    kb_id = create_kb(store, kb_tenant)
    run_id = uuid.uuid4()
    result = store.put_document(kb_tenant, kb_id, document_key="empty", title="Empty",
                                content="<html><body><script>var x = 1;</script></body></html>",
                                mime_type="text/html", metadata={}, run_id=str(run_id))
    doc_id, v = result.document["id"], result.document["latest_version"]
    enqueue_kb_ingest(client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=ingest_input(doc_id, v),
                      app_version=APP_VERSION, run_id=run_id)
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=60)
    doc = store.get_document_by_id(kb_tenant, doc_id)
    assert run["status"] == "failed", (run["status"], doc["status"], doc["chunk_count"])
    assert doc["status"] == "failed" and doc["active_version"] is None
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "extraction_failed"
