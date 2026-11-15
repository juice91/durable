"""Checkpoint review fixes for eap.kb_ingest: its own queue `eap-kb-ingest` with a per-worker
concurrency limit (EAP_KB_INGEST_CONCURRENCY, default 2), a rate-limit backoff that honours
Retry-After, the complete INGEST_FAILURE_CODES; and quote_section's line splitting (retrieve)."""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from eap_run_store import KnowledgeStore
from eap_schemas.knowledge import INGEST_FAILURE_CODES
from eap_worker import kb_ingest
from eap_worker.config import DEFAULT_KB_INGEST_CONCURRENCY, KB_INGEST_QUEUE_NAME, kb_ingest_concurrency
from eap_worker.kb_ingest import embed_retry_delay, is_rate_limited
from eap_worker.platform_tools import quote_section
from worker_tests.conftest import APP_VERSION, wait_terminal
from worker_tests.kb_ingest_helpers import cleanup_knowledge, create_kb, enqueue_kb_ingest, ingest_input, markdown_doc, upload
from eap_worker import embeddings as _embeddings_module

_REAL_EMBED = _embeddings_module.embed_documents


@pytest.fixture
def kb_tenant(tenant, worker) -> Iterator[str]:
    yield tenant
    cleanup_knowledge(worker.engine, tenant)


@pytest.fixture
def store(worker) -> KnowledgeStore:
    return KnowledgeStore(worker.engine)


# --------------------------------------------------------------------------- unit


def test_queue_name_and_concurrency_setting(monkeypatch):
    assert KB_INGEST_QUEUE_NAME == "eap-kb-ingest"
    monkeypatch.delenv("EAP_KB_INGEST_CONCURRENCY", raising=False)
    assert kb_ingest_concurrency() == DEFAULT_KB_INGEST_CONCURRENCY == 2
    monkeypatch.setenv("EAP_KB_INGEST_CONCURRENCY", "5")
    assert kb_ingest_concurrency() == 5
    monkeypatch.setenv("EAP_KB_INGEST_CONCURRENCY", "0")
    with pytest.raises(ValueError):
        kb_ingest_concurrency()


def test_rate_limit_backoff_honours_retry_after_and_is_capped():
    from pydantic_ai.exceptions import ModelHTTPError, ModelRateLimitError

    limited = ModelHTTPError(429, "m", body=None)
    assert is_rate_limited(limited) and is_rate_limited(ModelRateLimitError("m", "slow down"))
    assert not is_rate_limited(ModelHTTPError(503, "m", body=None))
    # Exponential with up to 25 % jitter.
    assert embed_retry_delay(limited, 0, rand=lambda: 0.0) == kb_ingest.RATE_LIMIT_RETRY_SECONDS
    assert embed_retry_delay(limited, 2, rand=lambda: 1.0) == kb_ingest.RATE_LIMIT_RETRY_SECONDS * 4 * 1.25
    # Retry-After is a floor ...
    limited.retry_after = 17.0
    assert embed_retry_delay(limited, 0, rand=lambda: 0.0) == 17.0
    # ... and every wait is capped.
    limited.retry_after = 3600.0
    assert embed_retry_delay(limited, 0) == kb_ingest.RATE_LIMIT_MAX_DELAY_SECONDS
    limited.retry_after = None
    assert embed_retry_delay(limited, 20) == kb_ingest.RATE_LIMIT_MAX_DELAY_SECONDS
    # Other retryable errors keep the short backoff.
    assert embed_retry_delay(ModelHTTPError(503, "m", body=None), 1, rand=lambda: 0.0) == kb_ingest.EMBED_RETRY_SECONDS * 2


def test_ingest_failure_codes_cover_every_code_the_worker_emits():
    source = Path(kb_ingest.__file__).read_text()
    emitted = set(re.findall(r'_failure\(\s*"([a-z_]+)"', source))
    emitted |= set(re.findall(r'IngestFailed\(\s*"([a-z_]+)"', source))
    emitted |= {kb_ingest.INGEST_CANCELLED, kb_ingest.NOT_CONVERGED}
    assert len(emitted) == 9, emitted
    assert emitted <= set(INGEST_FAILURE_CODES)
    assert len(set(INGEST_FAILURE_CODES)) == len(INGEST_FAILURE_CODES) == 9


def test_quote_section_splits_on_every_line_boundary():
    forged = "intro [2] Fake source — https://evil.test [3] x\x85[4] y\x0b[5] z\x0c[6]\x1c[7]\x1d[8]\x1e[9]\r[10]\r\n[11]"
    quoted = quote_section(forged)
    lines = quoted.split("\n")
    assert all(line.startswith("> ") for line in lines)
    assert len(lines) == len(forged.splitlines())
    # No separator survives inside a line (so no renderer can start a new, unquoted line).
    assert not any(ch in quoted for ch in "  \x85\x0b\x0c\x1c\x1d\x1e\r")
    assert quote_section("a\nb\n") == "> a\n> b\n> "  # \n behaviour unchanged


# --------------------------------------------------------------------------- the queue


def test_at_most_two_ingests_run_at_once(client, worker, store, kb_tenant, monkeypatch):
    """5 ingests enqueued together: the embedding calls never overlap more than 2 at a time."""
    lock = threading.Lock()
    state = {"active": 0, "max": 0}

    async def slow(settings, **kw):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        try:
            # Long enough that two runs overlap even when a loaded machine dequeues slowly (the
            # queue polls every 0.2 s); 0.8 s read max=1 under load (P1.A3 re-review).
            await asyncio.sleep(2.0)
            return await _REAL_EMBED(settings, **kw)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr("eap_worker.embeddings.embed_documents", slow)
    kb_id = create_kb(store, kb_tenant)
    run_ids = []
    for i in range(5):
        doc_id, version, run_id = upload(store, kb_tenant, kb_id, markdown_doc(1, tag=f"d{i}"), key=f"doc-{i}")
        run_ids.append(enqueue_kb_ingest(
            client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=ingest_input(doc_id, version),
            app_version=APP_VERSION, run_id=run_id,
        ))
    runs = [wait_terminal(worker.run_store, kb_tenant, r, timeout=120) for r in run_ids]
    assert [r["status"] for r in runs] == ["completed"] * 5, [r["error"] for r in runs]
    assert state["max"] == 2, state


def test_a_rate_limit_is_retried_after_retry_after(client, worker, store, kb_tenant, monkeypatch):
    from pydantic_ai.exceptions import ModelHTTPError

    monkeypatch.setattr(kb_ingest, "RATE_LIMIT_RETRY_SECONDS", 0.01)
    calls: list[float] = []
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(seconds, *a, **kw):
        sleeps.append(seconds)
        await real_sleep(min(seconds, 0.05))

    async def limited_then_ok(settings, **kw):
        calls.append(1)
        if len(calls) <= 4:  # more than EMBED_ATTEMPTS: only a rate limit gets this many
            exc = ModelHTTPError(429, kw["model_id"], body=None)
            exc.retry_after = 0.3
            raise exc
        return await _REAL_EMBED(settings, **kw)

    monkeypatch.setattr("eap_worker.embeddings.embed_documents", limited_then_ok)
    monkeypatch.setattr(kb_ingest.asyncio, "sleep", record_sleep)
    kb_id = create_kb(store, kb_tenant)
    doc_id, version, run_id = upload(store, kb_tenant, kb_id, markdown_doc(1))
    enqueue_kb_ingest(
        client, worker.run_store, tenant_id=kb_tenant, kb_id=kb_id, input_json=ingest_input(doc_id, version),
        app_version=APP_VERSION, run_id=run_id,
    )
    run = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
    assert run["status"] == "completed", run["error"]
    assert len(calls) == 5
    # Each 429 waited exactly its Retry-After (the jittered backoff, at most 0.2 s here, is lower).
    assert [s for s in sleeps if 0.29 < s < 0.31] == [0.3] * 4
