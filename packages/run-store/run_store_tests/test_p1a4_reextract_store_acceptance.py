"""RAG P1.A4 store acceptance: the edges of the outdated-extractor query (generation, version, which
chunk, which knowledge base), what a new version leaves untouched, races on new versions and on the
chunker v2 switch, and the rollback of a bulk re-extract."""

from __future__ import annotations

import json
import threading
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import event, text

from eap_run_store.knowledge import IngestRunConflict, KnowledgeStore

from .test_knowledge_store import ingest, make_kb
from .test_p1a4_reextract_store import RST_MAP, _put, _record

MAP = {"text/x-rst": ("rst", 2)}


@pytest.fixture
def store(engine):
    return KnowledgeStore(engine)


@pytest.fixture
def tenant():
    return f"kb_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant):
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant})


def _rst(store, engine, tenant, kb, key, eap, version=1):
    doc, _ = ingest(store, tenant, kb, key, [f"body of {key}"], hot=[1])
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_documents SET mime_type = 'text/x-rst' WHERE id = :i"), {"i": doc["id"]})
    if eap is not None:
        _record(engine, doc["id"], eap)
    return doc


def _keys(store, tenant, kb, current=MAP):
    return [d["document_key"] for d in store.outdated_extraction_documents(tenant, kb["id"], current)]


def test_outdated_compares_the_version_exactly_and_any_chunk_may_carry_it(store, engine, tenant):
    kb = make_kb(store, tenant)
    _rst(store, engine, tenant, kb, "v1.rst", {"extractor": "rst", "extractor_version": 1})
    _rst(store, engine, tenant, kb, "v2.rst", {"extractor": "rst", "extractor_version": 2})
    _rst(store, engine, tenant, kb, "v3.rst", {"extractor": "rst", "extractor_version": 3})
    _rst(store, engine, tenant, kb, "none.rst", None)
    _rst(store, engine, tenant, kb, "float.rst", {"extractor": "rst", "extractor_version": 2.0})
    _rst(store, engine, tenant, kb, "null.rst", {"extractor": "rst", "extractor_version": None})
    _rst(store, engine, tenant, kb, "noextractor.rst", {"extractor_version": 5})
    _rst(store, engine, tenant, kb, "wrongtype.rst", {"extractor": "yaml", "extractor_version": 9})
    assert _keys(store, tenant, kb) == ["noextractor.rst", "none.rst", "null.rst", "v1.rst", "wrongtype.rst"]
    # One chunk with the record is enough (the others of the document may carry none).
    doc, _ = ingest(store, tenant, kb, "multi.rst", ["one", "two", "three"], hot=[1, 2, 3])
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_documents SET mime_type = 'text/x-rst' WHERE id = :i"), {"i": doc["id"]})
        conn.execute(
            text("UPDATE kb_chunks SET metadata = CAST(:m AS jsonb) WHERE document_id = :d AND ordinal = 2"),
            {"m": json.dumps({"eap": {"extractor": "rst", "extractor_version": 2}}), "d": doc["id"]},
        )
    assert "multi.rst" not in _keys(store, tenant, kb)


def test_outdated_looks_only_at_the_active_version_and_generation(store, engine, tenant):
    kb = make_kb(store, tenant)
    good = {"extractor": "rst", "extractor_version": 2}
    stale = _rst(store, engine, tenant, kb, "stale.rst", good)
    wrong_gen = _rst(store, engine, tenant, kb, "gen.rst", good)
    fresh = _rst(store, engine, tenant, kb, "fresh.rst", good)
    assert _keys(store, tenant, kb) == []
    with engine.begin() as conn:
        # Records on chunks of a version that is not the active one do not count...
        conn.execute(text("UPDATE kb_chunks SET doc_version = 2 WHERE document_id = :d"), {"d": stale["id"]})
        conn.execute(text("UPDATE kb_documents SET latest_version = 2 WHERE id = :d"), {"d": stale["id"]})
        # ...nor do chunks of another generation than the active one.
        conn.execute(text("UPDATE kb_chunks SET generation = generation + 1 WHERE document_id = :d"), {"d": wrong_gen["id"]})
    assert _keys(store, tenant, kb) == ["gen.rst", "stale.rst"]
    assert fresh["document_key"] not in _keys(store, tenant, kb)
    # Moving the knowledge base to the next generation makes the generation-matching chunks the old ones.
    with engine.begin() as conn:
        conn.execute(text("UPDATE knowledge_bases SET last_generation = last_generation + 1,"
                 " active_generation = active_generation + 1 WHERE id = :k"), {"k": kb["id"]})
    assert _keys(store, tenant, kb) == ["fresh.rst", "stale.rst"]


def test_outdated_is_scoped_to_one_knowledge_base_and_to_the_mapped_types(store, engine, tenant):
    kb = make_kb(store, tenant)
    other = make_kb(store, tenant, name="Other")
    _rst(store, engine, tenant, kb, "mine.rst", None)
    _rst(store, engine, tenant, other, "theirs.rst", None)
    assert _keys(store, tenant, kb) == ["mine.rst"] and _keys(store, tenant, other) == ["theirs.rst"]
    assert _keys(store, tenant, kb, {"application/yaml": ("yaml", 1)}) == []  # a different type is never selected
    assert _keys(store, tenant, kb, {"TEXT/X-RST": ("rst", 2)}) == []  # the map is keyed by lower-case types
    assert store.outdated_extraction_documents(tenant, str(uuid.uuid4()), MAP) == []
    assert store.outdated_extraction_documents(tenant, "not-a-uuid", MAP) == []
    assert _keys(store, tenant, kb, RST_MAP) == ["mine.rst"]


def test_a_new_version_leaves_everything_but_its_bookkeeping_alone(store, engine, tenant):
    kb = make_kb(store, tenant)
    put = store.put_document(
        tenant, kb["id"], document_key="a.rst", title="A", content="x\n=\n\nbody", mime_type="text/x-rst",
        source_url="https://e.test/a", metadata={"device": "am62x"},
    )
    with engine.begin() as conn:
        conn.execute(text("UPDATE kb_documents SET status = 'failed', error = 'extraction_failed: x' WHERE id = :i"), {"i": put.document["id"]})
    before = store.get_document(tenant, kb["id"], "a.rst", include_content=True)
    run = str(uuid.uuid4())
    after = store.new_document_version(tenant, kb["id"], "a.rst", run_id=run)
    full = store.get_document(tenant, kb["id"], "a.rst", include_content=True)
    for key in ("title", "content", "content_hash", "metadata", "source_url", "mime_type", "active_version", "chunk_count", "created_at"):
        assert full[key] == before[key], key
    assert (after["status"], after["error"], after["last_run_id"], after["latest_version"]) == ("pending", None, run, 2)
    assert after["updated_at"] >= before["updated_at"]


def test_expected_latest_version_lets_exactly_one_of_many_racers_win(store, tenant):
    kb = make_kb(store, tenant)
    _put(store, tenant, kb, "a.rst")
    barrier, outcomes = threading.Barrier(6), []

    def racer():
        barrier.wait(timeout=10)
        try:
            store.new_document_version(tenant, kb["id"], "a.rst", run_id=str(uuid.uuid4()), expected_latest_version=1)
            outcomes.append("won")
        except IngestRunConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=racer) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(outcomes) == ["conflict"] * 5 + ["won"]
    assert store.get_document(tenant, kb["id"], "a.rst")["latest_version"] == 2
    # Without the guard, every call is its own version (no lost update).
    outcomes.clear()
    barrier = threading.Barrier(4)

    def plain():
        barrier.wait(timeout=10)
        outcomes.append(store.new_document_version(tenant, kb["id"], "a.rst", run_id=str(uuid.uuid4()))["latest_version"])

    threads = [threading.Thread(target=plain) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert sorted(outcomes) == [3, 4, 5, 6]


def test_concurrent_chunker_switches_change_the_config_exactly_once(store, tenant):
    kb = make_kb(store, tenant)
    for key in ("a.rst", "b.rst"):
        _put(store, tenant, kb, key)
    # make_kb creates a knowledge base on the store default; force v1 first.
    store.start_reextract(tenant, kb["id"], document_ids=[], chunker_version=1)
    barrier, results = threading.Barrier(4), []

    def switch():
        barrier.wait(timeout=10)
        results.append(store.start_reextract(tenant, kb["id"], document_ids=None, chunker_version=2))

    threads = [threading.Thread(target=switch) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert len(results) == 4 and sum(changed for _, _, changed in results) == 1
    versions = {d["document_key"]: [] for d in results[0][1]}
    for _, docs, _ in results:
        for d in docs:
            versions[d["document_key"]].append(d["latest_version"])
    assert all(sorted(v) == [2, 3, 4, 5] for v in versions.values())  # serialised by the knowledge base lock: no lost bump
    assert store.get_knowledge_base(tenant, kb["id"])["chunking"]["chunker_version"] == 2


def test_start_reextract_rolls_back_the_config_when_the_bump_fails(store, engine, tenant):
    kb = make_kb(store, tenant)
    store.start_reextract(tenant, kb["id"], document_ids=[], chunker_version=1)
    _put(store, tenant, kb, "a.rst")
    before_kb = store.get_knowledge_base(tenant, kb["id"])
    before_doc = store.get_document(tenant, kb["id"], "a.rst")

    def fail(conn, cursor, statement, parameters, context, executemany):
        if "UPDATE kb_documents SET latest_version" in statement:
            raise RuntimeError("boom")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(Exception, match="boom"):
            store.start_reextract(tenant, kb["id"], document_ids=None, chunker_version=2)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert store.get_knowledge_base(tenant, kb["id"]) == before_kb
    assert store.get_document(tenant, kb["id"], "a.rst") == before_doc
    # An invalid target version is refused before anything changes.
    with pytest.raises((ValidationError, ValueError)):
        store.start_reextract(tenant, kb["id"], document_ids=None, chunker_version=3)
    assert store.get_knowledge_base(tenant, kb["id"]) == before_kb
    assert store.get_document(tenant, kb["id"], "a.rst") == before_doc


def test_latest_extraction_ignores_other_documents_and_other_event_types(store, engine, tenant):
    from eap_run_store import RunStore

    kb = make_kb(store, tenant)
    mine, theirs = _put(store, tenant, kb, "a.pdf", mime="application/pdf"), _put(store, tenant, kb, "b.pdf", mime="application/pdf")
    runs = RunStore(engine)
    for doc, events in ((mine, [("started", "extract:1", {"x": 1})]), (theirs, [("extracted", "extract:1", {"extractor": "pdf", "pages": 99})])):
        run_id = uuid.uuid4()
        runs.create_kb_ingest_run(tenant, kb["id"], json.dumps({"document_id": doc["id"], "doc_version": 1}), run_id)
        with engine.begin() as conn:
            for seq, (kind, key, data) in enumerate(events, start=1):
                conn.execute(
                    text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                         " VALUES (:t, :r, :s, :k, :ty, CAST(:d AS jsonb), now())"),
                    {"t": tenant, "r": str(run_id), "s": seq, "k": key, "ty": kind, "d": json.dumps(data)},
                )
    assert store.latest_extraction(tenant, kb["id"], mine["id"]) is None
    assert store.latest_extraction(tenant, kb["id"], theirs["id"])["pages"] == 99
    assert store.latest_extraction(tenant, kb["id"], "junk") is None
    assert store.latest_extraction(tenant, "junk", theirs["id"]) is None
