"""KnowledgeStore (RM.4) lock order, generation numbering and search settings.

The interleaving tests pause one store call right after a chosen SQL statement (a cursor listener
on the engine, matched by thread name), run the competing call, then let the first finish. With the
knowledge-base-then-document lock order none of them deadlocks (Postgres would raise
`deadlock detected` after deadlock_timeout), and the final state is the expected one.
"""

from __future__ import annotations

import os
import pickle
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, event, text

from eap_db.testing import SCHEMA_ENV

from eap_run_store import (
    GenerationModelMismatch,
    GenerationNotWritable,
    KnowledgeStore,
    RunStore,
)

from .conftest import DATABASE_URL
from .test_knowledge_store import MODEL, MODEL_768, chunk, ingest, make_kb, search, vec

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

WAIT = 15  # seconds; a deadlock is reported by Postgres after 1 s, a hang fails the join


@pytest.fixture
def store(engine):
    return KnowledgeStore(engine)


@pytest.fixture
def tenant():
    return f"kbc_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant):
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant})


class Pause:
    """Pause the thread named `thread` right after its first statement matching all `needles`."""

    def __init__(self, engine, thread: str, *needles: str) -> None:
        self.engine, self.thread, self.needles = engine, thread, needles
        self.reached, self.go = threading.Event(), threading.Event()
        event.listen(engine, "after_cursor_execute", self._hook)

    def _hook(self, conn, cursor, statement, parameters, context, executemany):
        if threading.current_thread().name != self.thread or self.reached.is_set():
            return
        if all(n in statement for n in self.needles):
            self.reached.set()
            assert self.go.wait(WAIT), "paused call was never released"

    def close(self) -> None:
        self.go.set()
        event.remove(self.engine, "after_cursor_execute", self._hook)


class Call(threading.Thread):
    def __init__(self, name: str, fn) -> None:
        super().__init__(name=name, daemon=True)
        self.fn, self.result, self.error = fn, None, None

    def run(self) -> None:
        try:
            self.result = self.fn()
        except BaseException as exc:  # reported by finish()
            self.error = exc

    def finish(self):
        self.join(WAIT)
        assert not self.is_alive(), f"{self.name} did not finish (blocked)"
        if self.error is not None:
            raise self.error
        return self.result


def interleave(
    engine, first: tuple[str, object], needles: tuple[str, ...], second: tuple[str, object], *, blocks: bool
):
    """Run `first` until just after its statement matching `needles`, run `second` concurrently,
    check that it waits on a lock `first` holds (`blocks`) or finishes meanwhile, release `first`,
    and return both results."""
    pause = Pause(engine, first[0], *needles)
    try:
        a = Call(*first)
        a.start()
        assert pause.reached.wait(WAIT), f"{first[0]} never reached {needles}"
        b = Call(*second)
        b.start()
        b.join(1.0)  # it either finishes (no conflicting lock) or waits for one `first` holds
        assert b.is_alive() is blocks, f"{second[0]} {'did not wait' if blocks else 'waited'}"
        pause.go.set()
        return a.finish(), b.finish()
    finally:
        pause.close()


def _reupload(store, tenant, kb):
    return lambda: store.put_document(
        tenant, kb["id"], document_key="d", title="D", content="changed", mime_type="text/plain"
    )


def _doc(store, tenant, kb):
    return store.get_document(tenant, kb["id"], "d")


# --------------------------------------------------------------------- lock order


@pytest.mark.parametrize("paused", ["finalize", "put"])
def test_reupload_versus_finalize_document(store, engine, tenant, paused):
    kb = make_kb(store, tenant)
    doc, v1 = ingest(store, tenant, kb, "d", ["one"], hot=[1], finalize=False)
    finalize = ("finalize", lambda: store.finalize_document(tenant, doc["id"], v1, 1, embedding_model=MODEL))
    put = ("put", _reupload(store, tenant, kb))
    if paused == "finalize":  # holds the knowledge base FOR SHARE, document not yet locked
        fin, upload = interleave(engine, finalize, ("FROM knowledge_bases", "FOR SHARE"), put, blocks=False)
    else:  # the upload holds the document FOR NO KEY UPDATE
        upload, fin = interleave(engine, put, ("FROM kb_documents", "FOR NO KEY UPDATE"), finalize, blocks=True)
    assert fin.status == "switched" and upload.document["latest_version"] == 2
    after = _doc(store, tenant, kb)
    # v1 is active and searchable; v2 waits for its ingest (status stays pending).
    assert (after["active_version"], after["latest_version"], after["status"]) == (1, 2, "pending")
    assert [h.doc_version for h in search(store, tenant, [kb["id"]], vec(1536, 1))] == [1]


@pytest.mark.parametrize("paused", ["upsert", "put"])
def test_reupload_versus_upsert_chunks(store, engine, tenant, paused):
    kb = make_kb(store, tenant)
    put0 = store.put_document(
        tenant, kb["id"], document_key="d", title="D", content="c", mime_type="text/plain", metadata={"v": 1}
    )
    did = put0.document["id"]
    upsert = ("upsert", lambda: store.upsert_chunks(tenant, did, 1, 1, [chunk(0, "a"), chunk(1, "b")], metadata={"v": 1}))
    put = ("put", _reupload(store, tenant, kb))
    if paused == "upsert":
        ups, upload = interleave(engine, upsert, ("FROM knowledge_bases", "FOR SHARE"), put, blocks=False)
    else:
        upload, ups = interleave(engine, put, ("FROM kb_documents", "FOR NO KEY UPDATE"), upsert, blocks=True)
    assert (ups.total, ups.inserted) == (2, 2) and upload.document["latest_version"] == 2
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT doc_version, metadata FROM kb_chunks WHERE tenant_id = :t ORDER BY ordinal"
        ), {"t": tenant}).all()
    # The chunks carry the snapshot's metadata, not the re-upload's.
    assert rows == [(1, {"v": 1}), (1, {"v": 1})]


@pytest.mark.parametrize("paused", ["reindex", "finalize"])
def test_finalize_reindex_versus_finalize_document(store, engine, tenant, paused):
    kb = make_kb(store, tenant)
    ingest(store, tenant, kb, "a", ["one"], hot=[1])
    b, vb = ingest(store, tenant, kb, "b", ["two"], hot=[2], finalize=False)  # embedded in gen 1, not active
    gen = store.start_reindex(tenant, kb["id"], embedding_model=MODEL_768, dimensions=768)["pending_generation"]
    store.copy_generation_chunks(tenant, kb["id"], gen)
    batch = store.next_unembedded_batch(tenant, kb["id"], gen)
    store.write_embeddings(tenant, kb["id"], gen, {c.id: vec(768, 1) for c in batch}, embedding_model=MODEL_768)
    reindex = ("reindex", lambda: store.finalize_reindex(tenant, kb["id"], gen, embedding_model=MODEL_768))
    finalize = ("finalize", lambda: store.finalize_document(tenant, b["id"], vb, 1, embedding_model=MODEL))
    if paused == "reindex":  # holds the knowledge base FOR UPDATE: the document finalise waits for it
        rx, fin = interleave(engine, reindex, ("FROM knowledge_bases", "FOR UPDATE"), finalize, blocks=True)
        # The switch happened first: b's chunks were in the old generation, so chunk again.
        assert rx.status == "switched" and fin.status == "stale_generation"
        assert _doc_by_key(store, tenant, kb, "b")["active_version"] is None
    else:  # the document finalise holds the knowledge base FOR SHARE and the document
        fin, rx = interleave(engine, finalize, ("FROM kb_documents", "FOR NO KEY UPDATE"), reindex, blocks=True)
        # b became active first: the reindex copies it into the new generation and is incomplete.
        assert fin.status == "switched" and rx.status == "incomplete" and rx.pending == 1
        assert store.get_knowledge_base(tenant, kb["id"])["active_generation"] == 1


def _doc_by_key(store, tenant, kb, key):
    return store.get_document(tenant, kb["id"], key)


# ------------------------------------------------------------- generation numbering


def test_a_cancelled_reindex_never_shares_its_generation_with_the_next(store, tenant):
    model_a, model_b = MODEL_768, "test:other768"
    kb = make_kb(store, tenant)
    ingest(store, tenant, kb, "d", ["one", "two"], hot=[1, 2])
    gen_a = store.start_reindex(tenant, kb["id"], embedding_model=model_a, dimensions=768)["pending_generation"]
    store.copy_generation_chunks(tenant, kb["id"], gen_a)
    a_batch = store.next_unembedded_batch(tenant, kb["id"], gen_a)  # A's run is in flight with this batch
    assert store.cancel_reindex(tenant, kb["id"])
    gen_b = store.start_reindex(tenant, kb["id"], embedding_model=model_b, dimensions=768)["pending_generation"]
    assert gen_b == gen_a + 1
    assert store.get_knowledge_base(tenant, kb["id"])["last_generation"] == gen_b
    store.copy_generation_chunks(tenant, kb["id"], gen_b)
    # A's in-flight batch can write neither into its own (cancelled) generation nor into B's.
    with pytest.raises(GenerationNotWritable):
        store.write_embeddings(tenant, kb["id"], gen_a, {c.id: vec(768, 1) for c in a_batch}, embedding_model=model_a)
    b_batch = store.next_unembedded_batch(tenant, kb["id"], gen_b)
    with pytest.raises(GenerationModelMismatch) as err:
        store.write_embeddings(tenant, kb["id"], gen_b, {c.id: vec(768, 1) for c in b_batch}, embedding_model=model_a)
    assert (err.value.expected, err.value.actual) == (model_b, model_a)
    assert store.embedding_progress(tenant, kb["id"], gen_b).embedded == 0
    with pytest.raises(GenerationModelMismatch):
        store.finalize_reindex(tenant, kb["id"], gen_b, embedding_model=model_a)
    assert store.finalize_reindex(tenant, kb["id"], gen_a, embedding_model=model_a).status == "missing"
    # B finishes normally.
    store.write_embeddings(tenant, kb["id"], gen_b, {c.id: vec(768, 1) for c in b_batch}, embedding_model=model_b)
    assert store.finalize_reindex(tenant, kb["id"], gen_b, embedding_model=model_b).status == "switched"
    after = store.get_knowledge_base(tenant, kb["id"])
    assert (after["active_generation"], after["embedding_model"]) == (gen_b, model_b)
    with pytest.raises(GenerationModelMismatch):  # also once B's generation is active
        store.finalize_reindex(tenant, kb["id"], gen_b, embedding_model=model_a)
    # The next reindex takes a fresh number again.
    assert store.start_reindex(tenant, kb["id"], embedding_model=MODEL, dimensions=1536)["pending_generation"] == gen_b + 1


def test_finalize_document_rejects_another_model(store, tenant):
    kb = make_kb(store, tenant)
    doc, v = ingest(store, tenant, kb, "d", ["one"], hot=[1], finalize=False)
    with pytest.raises(GenerationModelMismatch):
        store.finalize_document(tenant, doc["id"], v, 1, embedding_model=MODEL_768)
    assert store.get_document(tenant, kb["id"], "d")["active_version"] is None
    assert pickle.loads(pickle.dumps(GenerationModelMismatch("k", 2, "a", "b"))).actual == "b"


# ------------------------------------------------------------------- search settings


def test_hnsw_settings_apply_inside_the_search_transaction_only(tenant):
    eng = create_engine(DATABASE_URL, pool_size=1, max_overflow=0)  # one connection: the reset is visible
    store = KnowledgeStore(eng)
    seen: list[tuple[str, str]] = []

    def probe(conn, cursor, statement, parameters, context, executemany):
        if "OPERATOR(public.<=>)" in statement and not statement.startswith("EXPLAIN"):
            seen.append(tuple(cursor.connection.execute(
                "SELECT current_setting('hnsw.ef_search'), current_setting('hnsw.iterative_scan')"
            ).fetchone()))

    try:
        kb = make_kb(store, tenant)
        ingest(store, tenant, kb, "d", ["one"], hot=[1])
        event.listen(eng, "after_cursor_execute", probe)
        try:
            assert len(search(store, tenant, [kb["id"]], vec(1536, 1), top_k=20)) == 1
            assert len(search(store, tenant, [kb["id"]], vec(1536, 1), top_k=5)) == 1
        finally:
            event.remove(eng, "after_cursor_execute", probe)
        assert seen == [("80", "relaxed_order"), ("40", "relaxed_order")]  # max(40, 4 * top_k)
        with eng.connect() as conn:  # the same pooled connection, after the search committed
            after = conn.execute(text(
                "SELECT current_setting('hnsw.ef_search'), current_setting('hnsw.iterative_scan')"
            )).one()
        assert tuple(after) == ("40", "off")
    finally:
        eng.dispose()


# ------------------------------------------------------------------------ tool runs


def test_a_tool_run_may_name_its_knowledge_base(store, engine, tenant):
    kb = make_kb(store, tenant)
    runs = RunStore(engine)
    run_id = uuid.uuid4()
    runs.create_tool_run(tenant, "knowledge.search", "{}", run_id, knowledge_base_id=kb["id"])
    assert runs.get_run(tenant, run_id)["knowledge_base_id"] == kb["id"]
    assert [r["id"] for r in runs.list_runs(tenant, knowledge_base_id=kb["id"])] == [str(run_id)]
