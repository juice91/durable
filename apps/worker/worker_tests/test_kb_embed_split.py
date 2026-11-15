"""P1.C5: the embed split. Runs whose input has `embed_split: true` embed each batch in
eap.kb_embed_call (select, cache, the provider call; checkpointed float32 vectors) and
eap.kb_embed_write (one transaction), so a database retry of the write never calls the provider
again. Runs without the flag keep eap.kb_embed_batch."""

from __future__ import annotations

import base64
import json
import math
import pickle
from collections.abc import Iterator

import psycopg
import pytest

from eap_run_store import KnowledgeStore
from eap_worker import embeddings as _embeddings_module
from eap_worker import kb_ingest
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
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
from worker_tests.test_conversation import step_names


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
def provider_calls(monkeypatch) -> list[int]:
    """Counts eap_worker.embeddings.embed_documents calls (the provider), passing them through."""
    calls: list[int] = []
    real = _embeddings_module.embed_documents

    async def spy(*args, **kwargs):
        calls.append(len(kwargs.get("texts") or ()))
        return await real(*args, **kwargs)

    monkeypatch.setattr(_embeddings_module, "embed_documents", spy)
    return calls


def split_input(document_id: str, version: int) -> str:
    params = json.loads(ingest_input(document_id, version))
    return json.dumps({**params, "embed_split": True}, sort_keys=True, separators=(",", ":"))


def _ingest(client, worker, store, tenant, kb_id, content, *, split: bool):
    doc_id, version, run_id = upload(store, tenant, kb_id, content)
    input_json = split_input(doc_id, version) if split else ingest_input(doc_id, version)
    enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=input_json,
                      app_version=APP_VERSION, run_id=run_id)
    return doc_id, run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=90)


# --------------------------------------------------------------------------- unit


def test_vectors_round_trip_as_float32():
    vectors = ((0.1, -0.25, 3.0), (1e-8, 0.0, -1.0))
    blob, dims = kb_ingest.pack_vectors(vectors)
    assert dims == 3 and len(blob) == 2 * 3 * 4
    back = kb_ingest.unpack_vectors(blob, dims)
    assert [[round(x, 6) for x in v] for v in back] == [[round(x, 6) for x in v] for v in vectors]
    assert kb_ingest.pack_vectors(()) == (b"", 0) and kb_ingest.unpack_vectors(b"", 0) == []
    with pytest.raises(ValueError):
        kb_ingest.pack_vectors(((1.0,), (1.0, 2.0)))


def test_the_call_steps_checkpoint_of_64_by_1536_vectors_is_bounded():
    import random

    vectors = tuple(tuple(random.uniform(-1, 1) for _ in range(1536)) for _ in range(64))
    blob, dims = kb_ingest.pack_vectors(vectors)
    output = {
        "status": "called", "chunk_ids": [f"{i:08d}-0000-0000-0000-000000000000" for i in range(64)],
        "slots": list(range(64)), "vectors": blob, "vector_format": "f32", "dimensions": dims,
        "usage": {"model_id": "litellm:x", "input_tokens": 99999, "output_tokens": 0}, "event_data": {"inputs": 64},
        "requests": 2, "filled": 0, "tokens": 99999,
    }
    pickled = pickle.dumps(output)
    stored = base64.b64encode(pickled)  # what DBOS's default serializer writes
    assert len(blob) == 64 * 1536 * 4 == 384 * 1024
    assert len(pickled) < 400 * 1024  # the pickled step output: about 384 KiB
    assert len(stored) < 540 * 1024  # base64 in operation_outputs: about 4/3 of that, far under 2 MB
    # The cap on EAP_KB_EMBED_BATCH_CHUNKS keeps the worst case (128 x 1536) under 1.1 MB stored.
    assert kb_ingest.MAX_BATCH_CHUNKS * 1536 * 4 * 4 / 3 < 1.1 * 1024 * 1024


def test_batch_chunks_are_capped(monkeypatch):
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", "100000")
    assert kb_ingest._batch_chunks() == kb_ingest.MAX_BATCH_CHUNKS
    monkeypatch.setenv("EAP_KB_EMBED_BATCH_CHUNKS", "0")
    assert kb_ingest._batch_chunks() == 1


# --------------------------------------------------------------------------- end to end


def test_split_runs_use_the_call_and_write_steps(client, worker, worker_settings, store, kb_tenant, small_batches,
                                                 provider_calls):
    kb_id = create_kb(store, kb_tenant)
    doc_id, run_id, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(10), split=True)
    assert run["status"] == "completed", run["error"]
    n = chunk_stats(worker.engine, kb_tenant, doc_id)[0]["n"]
    batches = math.ceil(n / 4)
    names = step_names(worker_settings.dbos_system_schema, run_id)
    assert "eap.kb_embed_batch" not in names
    assert names.count("eap.kb_embed_call") == batches + 1  # the last call finds nothing left ("done")
    assert names.count("eap.kb_embed_write") == batches
    # Each write follows its call.
    pairs = [(a, b) for a, b in zip(names, names[1:]) if b == "eap.kb_embed_write"]
    assert all(a == "eap.kb_embed_call" for a, _ in pairs)
    assert len(provider_calls) == batches
    usage = usage_rows(worker.engine, run_id)
    assert [u["request_key"] for u in usage] == sorted(f"embed:batch:{i}" for i in range(batches))
    events = event_rows(worker.engine, run_id)
    assert_contiguous(events)
    assert [e[1] for e in events] == ["start", "chunk:0"] + [f"embed:batch:{i}" for i in range(batches)] + [
        "finalize:0", "end"]
    assert "handbook explains" not in json.dumps([e[3] for e in events])


def test_runs_without_the_flag_keep_kb_embed_batch(client, worker, worker_settings, store, kb_tenant, small_batches):
    kb_id = create_kb(store, kb_tenant)
    _, run_id, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(6), split=False)
    assert run["status"] == "completed", run["error"]
    names = step_names(worker_settings.dbos_system_schema, run_id)
    assert "eap.kb_embed_batch" in names
    assert "eap.kb_embed_call" not in names and "eap.kb_embed_write" not in names


def test_a_deadlock_in_the_write_retries_without_a_second_provider_call(
    client, worker, store, kb_tenant, small_batches, provider_calls, monkeypatch
):
    real_write = KnowledgeStore.write_embeddings
    raised: list[str] = []

    def flaky(self, *args, **kwargs):
        if kwargs.get("event_key") == "embed:batch:0" and not raised:
            raised.append("40P01")
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(KnowledgeStore, "write_embeddings", flaky)
    kb_id = create_kb(store, kb_tenant)
    doc_id, run_id, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(6), split=True)
    assert run["status"] == "completed", run["error"]
    assert raised == ["40P01"]
    n = chunk_stats(worker.engine, kb_tenant, doc_id)[0]["n"]
    batches = math.ceil(n / 4)
    assert len(provider_calls) == batches  # the retried write re-used the checkpointed vectors
    assert [u["request_key"] for u in usage_rows(worker.engine, run_id)] == sorted(
        f"embed:batch:{i}" for i in range(batches))


def test_ragged_provider_vectors_fail_with_their_usage_recorded(client, worker, store, kb_tenant, small_batches, monkeypatch):
    """MINOR-4 (review): vectors of different lengths after a paid call end the batch as a
    dimension-mismatch refusal: an `embedding` event (phase refused) with the call's usage row."""
    import dataclasses

    real = _embeddings_module.embed_documents

    async def ragged(*args, **kwargs):
        call = await real(*args, **kwargs)
        first, *rest = call.vectors
        return dataclasses.replace(call, vectors=(tuple(first[:-1]), *rest))

    monkeypatch.setattr(_embeddings_module, "embed_documents", ragged)
    kb_id = create_kb(store, kb_tenant)
    _, run_id, run = _ingest(client, worker, store, kb_tenant, kb_id, markdown_doc(6), split=True)
    assert run["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    refused = [(t, d) for _, k, t, d in events if k == "embed:batch:0"]
    assert refused and refused[0][0] == "embedding" and refused[0][1]["refused"] == "embedding_dimension_mismatch"
    assert [u["request_key"] for u in usage_rows(worker.engine, run_id)] == ["embed:batch:0"]
    assert events[-1][2] == "run_failed" and events[-1][3]["error_code"] == "embedding_dimension_mismatch"
