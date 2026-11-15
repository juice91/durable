"""Acceptance: the partitioned `eap-kb-ingest-fair` queue (RAG P1 follow-up) with a real worker and the
real DBOS queue: fairness (no gate, bounded sizes, run twice), upgrade from the legacy queue with no
double execution, and the per-worker concurrency cap (doubled while the legacy queue drains)."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterator

import pytest

from eap_run_store import KnowledgeStore
from eap_worker import embeddings as _embeddings_module
from eap_worker.config import KB_INGEST_QUEUE_NAME, LEGACY_KB_INGEST_QUEUE_NAME, kb_ingest_concurrency
from worker_tests.conftest import APP_VERSION, wait_terminal
from worker_tests.kb_ingest_helpers import cleanup_knowledge, create_kb, enqueue_kb_ingest, ingest_input, markdown_doc, upload

_REAL_EMBED = _embeddings_module.embed_documents


@pytest.fixture
def kb_tenant(tenant, worker) -> Iterator[str]:
    yield tenant
    cleanup_knowledge(worker.engine, tenant)


@pytest.fixture
def store(worker) -> KnowledgeStore:
    return KnowledgeStore(worker.engine)


class Probe:
    """Wraps embed_documents: records the first start of each run, per-run call counts, and overlap
    (overall and per queue group)."""

    def __init__(self, delay: float, groups: dict[str, set[str]] | None = None) -> None:
        self.delay = delay
        self.lock = threading.Lock()
        self.started: list[str] = []
        self.calls: Counter[str] = Counter()
        self.active = 0
        self.max_active = 0
        self.groups = groups or {}
        self.group_active: Counter[str] = Counter()
        self.group_max: Counter[str] = Counter()

    def _groups_of(self, run_id: str) -> list[str]:
        return [g for g, ids in self.groups.items() if run_id in ids]

    async def __call__(self, settings, **kw):
        rid = str(kw["run_id"])
        with self.lock:
            self.calls[rid] += 1
            if rid not in self.started:
                self.started.append(rid)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            for g in self._groups_of(rid):
                self.group_active[g] += 1
                self.group_max[g] = max(self.group_max[g], self.group_active[g])
        try:
            await asyncio.sleep(self.delay)
            return await _REAL_EMBED(settings, **kw)
        finally:
            with self.lock:
                self.active -= 1
                for g in self._groups_of(rid):
                    self.group_active[g] -= 1


def _enqueue(client, worker, store, tenant, kb_id, tag, *, legacy=False) -> uuid.UUID:
    doc_id, version, run_id = upload(store, tenant, kb_id, markdown_doc(1, tag=tag), key=f"d-{tag}")
    return enqueue_kb_ingest(
        client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=ingest_input(doc_id, version),
        app_version=APP_VERSION, run_id=run_id, legacy_queue=legacy,
    )


@pytest.mark.parametrize("attempt", [1, 2])
def test_fairness_b_starts_early_behind_a_bulk_import(client, worker, store, kb_tenant, monkeypatch, attempt):
    """60 runs for KB A, then 1 for KB B, no gate: B starts well before the ~60th start (FIFO would be
    last). Randomised: each poll with room puts B first with probability 1/2, and a poll can start
    up to the cap (2) of A's runs, so 24 starts span at least 12 such polls: P(fail) <= 0.5^12 per
    attempt (see test_kb_ingest_queue's fairness test)."""
    probe = Probe(0.15)
    monkeypatch.setattr("eap_worker.embeddings.embed_documents", probe)
    kb_a, kb_b = create_kb(store, kb_tenant), create_kb(store, kb_tenant)
    a_runs = [_enqueue(client, worker, store, kb_tenant, kb_a, f"a{attempt}x{i}") for i in range(60)]
    with probe.lock:
        before_b = len(probe.started)
    b_run = _enqueue(client, worker, store, kb_tenant, kb_b, f"b{attempt}")
    run_b = wait_terminal(worker.run_store, kb_tenant, b_run, timeout=90)
    assert run_b["status"] == "completed", run_b["error"]
    with probe.lock:
        position = probe.started.index(str(b_run))
        done_a_when_b_started = position - 1
    assert position - before_b <= 12 * kb_ingest_concurrency(), (before_b, position)
    assert done_a_when_b_started < 40, position  # B did not wait behind most of the import
    runs = [wait_terminal(worker.run_store, kb_tenant, r, timeout=180) for r in a_runs]
    assert {r["status"] for r in runs} == {"completed"}


def test_legacy_queue_runs_drain_exactly_once_alongside_fair_ones(client, worker, store, kb_tenant, monkeypatch):
    """Upgrade: runs enqueued on `eap-kb-ingest` before the change finish, each executed once, with
    new fair-queue runs interleaved."""
    probe = Probe(0.1)
    monkeypatch.setattr("eap_worker.embeddings.embed_documents", probe)
    kb_id = create_kb(store, kb_tenant)
    legacy = [_enqueue(client, worker, store, kb_tenant, kb_id, f"l{i}", legacy=True) for i in range(6)]
    fair = [_enqueue(client, worker, store, kb_tenant, kb_id, f"f{i}") for i in range(6)]
    runs = [wait_terminal(worker.run_store, kb_tenant, r, timeout=90) for r in legacy + fair]
    assert [r["status"] for r in runs] == ["completed"] * 12, [r["error"] for r in runs]
    time.sleep(1.0)  # a duplicate dequeue would show up as a second embed call
    assert {rid: probe.calls[str(rid)] for rid in legacy + fair} == {rid: 1 for rid in legacy + fair}
    assert len(probe.started) == 12
    for i in range(6):
        for tag in (f"l{i}", f"f{i}"):
            doc = store.get_document(kb_tenant, kb_id, f"d-{tag}")
            assert doc is not None and doc["status"] == "ready" and doc["active_version"] == 1


def test_cap_is_per_queue_per_worker_and_doubles_while_legacy_drains(client, worker, store, kb_tenant, monkeypatch):
    """Each queue runs at most EAP_KB_INGEST_CONCURRENCY at once; with both loaded the worker runs
    twice the cap (documented in the worker README). Pinned here."""
    cap = kb_ingest_concurrency()
    from dbos import DBOS

    assert DBOS.retrieve_queue(KB_INGEST_QUEUE_NAME)._worker_concurrency == cap
    assert DBOS.retrieve_queue(LEGACY_KB_INGEST_QUEUE_NAME)._worker_concurrency == cap
    groups: dict[str, set[str]] = {"fair": set(), "legacy": set()}
    probe = Probe(1.5, groups)
    monkeypatch.setattr("eap_worker.embeddings.embed_documents", probe)
    kb_id = create_kb(store, kb_tenant)
    ids = []
    for i in range(cap * 3):
        for name, legacy in (("legacy", True), ("fair", False)):
            run = _enqueue(client, worker, store, kb_tenant, kb_id, f"{name}{i}", legacy=legacy)
            groups[name].add(str(run))
            ids.append(run)
    runs = [wait_terminal(worker.run_store, kb_tenant, r, timeout=120) for r in ids]
    assert {r["status"] for r in runs} == {"completed"}
    assert probe.group_max["fair"] == cap and probe.group_max["legacy"] == cap, probe.group_max
    assert probe.max_active == 2 * cap, probe.max_active
