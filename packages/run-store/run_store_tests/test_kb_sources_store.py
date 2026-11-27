"""Knowledge-base source stores (0029_kb_sources): connections, sources, syncs, records, entities, run kind."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from eap_run_store import RUN_KINDS, KnowledgeStore, RunStore
from eap_run_store.kb_sources import (
    ConnectionInUse,
    ConnectionNameConflict,
    ConnectionStore,
    EntityStore,
    RecordStore,
    SourceNameConflict,
    SourceStore,
    SyncStore,
)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
REFS = {"client_id": "secret:conn-client-id", "client_secret": "secret:conn-client-secret"}
URL = "https://www.vendor.example/lit/ds/symlink/lm317.pdf"


@pytest.fixture
def tenants():
    return f"t_{uuid.uuid4().hex[:8]}", f"t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def kb(engine, tenants):
    a, b = tenants
    ks = KnowledgeStore(engine)
    ids = {t: ks.create_knowledge_base(t, name="kb", embedding_model="openai:m", dimensions=768)["id"] for t in (a, b)}
    yield ids
    with engine.begin() as conn:
        for t in (a, b):
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": t})


def _conn(engine, tenant, name="vendor"):
    return ConnectionStore(engine).create(
        tenant, name=name, kind="oauth2_client_credentials", base_url="https://api.vendor.example",
        config={"quota": 1}, credential_refs=REFS,
    )


def _source(engine, tenant, kb_id, conn_id, name="parts"):
    return SourceStore(engine).create(
        tenant, knowledge_base_id=kb_id, connection_id=conn_id, name=name, kind="product_catalog",
        provider="fake", config={"include_extended": False},
    )


# ------------------------------------------------------------------------------------ connections


def test_connection_crud_isolation_and_check(engine, tenants, kb):
    a, b = tenants
    store = ConnectionStore(engine)
    c = _conn(engine, a)
    assert c.credential_refs == REFS and c.enabled and c.last_status is None and c.last_checked_at is None
    assert c.base_url == "https://api.vendor.example" and c.kind == "oauth2_client_credentials"
    assert store.get(a, c.id).name == "vendor" and store.get(b, c.id) is None
    assert [x.id for x in store.list(a)] == [c.id] and store.list(b) == []
    with pytest.raises(ConnectionNameConflict):
        _conn(engine, a)
    _conn(engine, b)
    for bad in ({"k": "plain-value"}, {"k": "secret:Bad Name"}, {"": "secret:ok"}):
        with pytest.raises(ValueError):
            store.create(a, name="bad", kind="oauth2_client_credentials", base_url=None, config={}, credential_refs=bad)
    with pytest.raises(ValueError):
        store.create(a, name="k", kind="basic", base_url=None, config={}, credential_refs={})

    assert store.record_check(b, c.id, "ok", None) is False
    assert store.get(a, c.id).last_status is None
    assert store.record_check(a, c.id, "error", "unauthorized") is True
    got = store.get(a, c.id)
    assert (got.last_status, got.last_error_code) == ("error", "unauthorized") and got.last_checked_at
    assert store.record_check(a, c.id, "ok", None) and store.get(a, c.id).last_error_code is None
    with pytest.raises(ValueError):
        store.record_check(a, c.id, "bogus", None)

    u = store.update(a, c.id, name="ti2", credential_refs={"client_id": "secret:other"}, enabled=False)
    assert u.name == "ti2" and u.credential_refs == {"client_id": "secret:other"} and u.config == c.config
    assert u.enabled is False
    assert store.update(b, c.id, name="x") is None and store.delete(b, c.id) is False
    assert store.delete(a, c.id) is True and store.get(a, c.id) is None


def test_connection_delete_is_refused_while_a_source_uses_it(engine, tenants, kb):
    a, _ = tenants
    c = _conn(engine, a)
    s = _source(engine, a, kb[a], c.id)
    with pytest.raises(ConnectionInUse):
        ConnectionStore(engine).delete(a, c.id)
    assert SourceStore(engine).delete(a, s.id) is True
    assert ConnectionStore(engine).delete(a, c.id) is True


# --------------------------------------------------------------------------------------- sources


def test_source_crud_checkpoint_and_isolation(engine, tenants, kb):
    a, b = tenants
    store = SourceStore(engine)
    c = _conn(engine, a)
    s = _source(engine, a, kb[a], c.id)
    assert s.last_checkpoint == {} and s.enabled and s.last_successful_sync_at is None
    assert s.last_source_revision is None and s.knowledge_base_id == kb[a] and s.kind == "product_catalog"
    assert store.get(b, s.id) is None and store.get(a, s.id).id == s.id
    assert [x.id for x in store.list_for_kb(a, kb[a])] == [s.id] and store.list_for_kb(b, kb[a]) == []
    with pytest.raises(SourceNameConflict):
        _source(engine, a, kb[a], c.id)
    with pytest.raises(ValueError):  # another tenant's connection / knowledge base (composite FKs)
        _source(engine, b, kb[b], c.id, name="x")
    with pytest.raises(ValueError):
        _source(engine, a, kb[b], c.id, name="y")
    with pytest.raises(ValueError):
        store.create(a, knowledge_base_id=kb[a], connection_id=c.id, name="z", kind="other", provider="p")

    assert store.set_checkpoint(b, s.id, {"page": 9}, completed=True) is False
    assert store.get(a, s.id).last_checkpoint == {}
    assert store.set_checkpoint(a, s.id, {"page": 3}, last_source_revision="r1", completed=False)
    got = store.get(a, s.id)
    assert got.last_checkpoint == {"page": 3} and got.last_source_revision == "r1"
    assert got.last_successful_sync_at is None
    assert store.set_checkpoint(a, s.id, {"page": 4}, completed=True)
    got = store.get(a, s.id)
    assert got.last_successful_sync_at is not None and got.last_source_revision == "r1"  # None keeps it
    with pytest.raises(ValueError):
        store.set_checkpoint(a, s.id, {"blob": "x" * 9000}, completed=False)
    assert store.get(a, s.id).last_checkpoint == {"page": 4}

    assert store.update(a, s.id, enabled=False, config={"x": 1}).enabled is False
    assert store.update(b, s.id, enabled=True) is None
    assert store.delete(b, s.id) is False and store.delete(a, s.id) is True


# ----------------------------------------------------------------------------------------- syncs


def test_sync_lifecycle_idempotent_create_and_isolation(engine, tenants, kb):
    a, b = tenants
    s = _source(engine, a, kb[a], _conn(engine, a).id)
    syncs = SyncStore(engine)
    run_id, sync_id = str(uuid.uuid4()), str(uuid.uuid4())
    sync = syncs.create(a, sync_id, s.id, run_id, "targeted", "h1", parent_run_id="p1")
    assert (sync.id, sync.run_id, sync.status, sync.parent_run_id) == (sync_id, run_id, "running", "p1")
    assert sync.checkpoint == {} and sync.counters == {} and sync.bounded_errors == [] and sync.completed_at is None
    again = syncs.create(a, str(uuid.uuid4()), s.id, run_id, "targeted", "h1")  # replay of the same run
    assert again.id == sync_id
    assert syncs.get_by_run(b, run_id) is None and syncs.get_by_run(a, run_id).id == sync_id
    with pytest.raises(ValueError):
        syncs.create(a, str(uuid.uuid4()), s.id, str(uuid.uuid4()), "weird", "h")
    with pytest.raises(ValueError):
        syncs.create(b, str(uuid.uuid4()), s.id, str(uuid.uuid4()), "full", "h")

    errors = [{"key": "A", "code": "not_found"}]
    assert syncs.update_progress(b, run_id, checkpoint={"p": 1}, counters={"n": 1}, bounded_errors=errors) is False
    assert syncs.update_progress(a, run_id, checkpoint={"p": 1}, counters={"n": 1}, bounded_errors=errors)
    got = syncs.get_by_run(a, run_id)
    assert (got.checkpoint, got.counters, got.bounded_errors, got.status) == ({"p": 1}, {"n": 1}, errors, "running")
    with pytest.raises(ValueError):
        syncs.update_progress(a, run_id, checkpoint={}, counters={}, bounded_errors=[{"k": i} for i in range(51)])
    done = NOW + timedelta(minutes=5)
    assert syncs.finish(a, run_id, "incomplete", counters={"n": 2}, bounded_errors=[], completed_at=done)
    got = syncs.get_by_run(a, run_id)
    assert got.status == "incomplete" and got.counters == {"n": 2} and got.completed_at == done
    with pytest.raises(ValueError):
        syncs.finish(a, run_id, "bogus", counters={}, bounded_errors=[], completed_at=done)
    other = syncs.create(a, str(uuid.uuid4()), s.id, str(uuid.uuid4()), "full", "h2")
    assert [x.id for x in syncs.list_for_source(a, s.id, 10)] == [other.id, sync_id]  # newest first
    assert len(syncs.list_for_source(a, s.id, 1)) == 1 and syncs.list_for_source(b, s.id, 10) == []


# --------------------------------------------------------------------------------------- records


def test_record_upsert_hash_semantics_and_isolation(engine, tenants, kb):
    a, b = tenants
    s = _source(engine, a, kb[a], _conn(engine, a).id)
    records = RecordStore(engine)
    assert records.upsert(a, s.id, "LM317", "generic", {"b": 2, "a": [1, {"z": 1, "y": 2}]}, observed_at=NOW) == "inserted"
    assert records.upsert(a, s.id, "LM317", "generic", {"a": [1, {"y": 2, "z": 1}], "b": 2}, observed_at=NOW) == "unchanged"
    assert records.upsert(
        a, s.id, "LM317", "generic", {"b": 3}, source_updated_at=NOW, observed_at=NOW
    ) == "updated"
    assert records.upsert(a, s.id, "LM317", "orderable", {"b": 3}, observed_at=NOW) == "inserted"
    got = records.get(a, s.id, "LM317", "generic")
    assert got.raw_payload == {"b": 3} and got.source_updated_at == NOW and len(got.content_hash) == 64
    assert got.status == "active" and records.get(a, s.id, "LM317", "quality") is None
    assert records.get(b, s.id, "LM317", "generic") is None
    with pytest.raises(ValueError):
        records.upsert(a, s.id, "BIG", "generic", {"x": "y" * (256 * 1024)}, observed_at=NOW)
    with pytest.raises(ValueError):
        records.upsert(b, s.id, "LM317", "generic", {"b": 3}, observed_at=NOW)  # not b's source


def test_is_unchanged_is_a_read_only_hash_check_of_an_active_record(engine, tenants, kb):
    a, b = tenants
    s = _source(engine, a, kb[a], _conn(engine, a).id)
    records = RecordStore(engine)
    assert records.is_unchanged(a, s.id, "LM317", "listing", {"x": 1}) is False  # nothing stored
    records.upsert(a, s.id, "LM317", "listing", {"x": 1, "y": [2]}, observed_at=NOW)
    assert records.is_unchanged(a, s.id, "LM317", "listing", {"y": [2], "x": 1}) is True  # canonical JSON
    assert records.is_unchanged(a, s.id, "LM317", "listing", {"x": 2}) is False
    assert records.is_unchanged(a, s.id, "LM317", "detail", {"x": 1, "y": [2]}) is False  # another record type
    assert records.is_unchanged(b, s.id, "LM317", "listing", {"x": 1, "y": [2]}) is False  # another tenant
    assert records.get(a, s.id, "LM317", "listing").raw_payload == {"x": 1, "y": [2]}  # nothing was written
    records.mark_removed_except(a, s.id, ["OTHER"])
    assert records.is_unchanged(a, s.id, "LM317", "listing", {"x": 1, "y": [2]}) is False  # a removed row is not "unchanged"


def test_mark_removed_except_only_touches_unseen_active_rows(engine, tenants, kb):
    a, b = tenants
    s = _source(engine, a, kb[a], _conn(engine, a).id)
    sb = _source(engine, b, kb[b], _conn(engine, b).id)
    records = RecordStore(engine)
    for key in ("A", "B", "C"):
        records.upsert(a, s.id, key, "generic", {"k": key}, observed_at=NOW)
    records.upsert(b, sb.id, "B", "generic", {"k": "B"}, observed_at=NOW)
    assert records.mark_removed_except(a, s.id, iter(["A", "C"])) == 1
    assert records.get(a, s.id, "B", "generic").status == "removed"
    assert records.get(b, sb.id, "B", "generic").status == "active"
    assert records.mark_removed_except(a, s.id, ["A", "C"]) == 0
    assert records.upsert(a, s.id, "B", "generic", {"k": "B"}, observed_at=NOW) == "updated"
    assert records.get(a, s.id, "B", "generic").status == "active"
    with pytest.raises(ValueError):
        records.mark_removed_except(a, s.id, [])


def test_mark_removed_unobserved_since_removes_what_the_walk_did_not_observe(engine, tenants, kb):
    a, b = tenants
    s = _source(engine, a, kb[a], _conn(engine, a).id)
    sb = _source(engine, b, kb[b], _conn(engine, b).id)
    records = RecordStore(engine)
    walk = NOW + timedelta(hours=1)
    old = NOW
    for key in ("A", "B", "C"):
        records.upsert(a, s.id, key, "detail", {"k": key}, observed_at=old)
        records.upsert(a, s.id, key, "listing", {"k": key}, observed_at=old)
    records.upsert(b, sb.id, "B", "detail", {"k": "B"}, observed_at=old)
    # the walk observes A (re-upsert, same hash: only observed_at moves) and C's listing only
    assert records.upsert(a, s.id, "A", "detail", {"k": "A"}, observed_at=walk) == "unchanged"
    records.upsert(a, s.id, "C", "listing", {"k": "C"}, observed_at=walk)
    assert records.mark_removed_unobserved_since(a, s.id, walk) == 2  # only B (both its rows): A and C were observed by any record type
    assert records.get(a, s.id, "B", "detail").status == "removed" and records.get(a, s.id, "B", "listing").status == "removed"
    assert records.get(a, s.id, "A", "detail").status == "active" and records.get(a, s.id, "C", "detail").status == "active"
    assert records.get(b, sb.id, "B", "detail").status == "active"  # another tenant and source untouched
    assert records.mark_removed_unobserved_since(a, s.id, walk) == 0
    # nothing observed since: refuses rather than removing every record
    with pytest.raises(ValueError):
        records.mark_removed_unobserved_since(a, s.id, walk + timedelta(days=1))


# ---------------------------------------------------------------------------------------- entities


def test_entities_identifiers_parameters_and_isolation(engine, tenants, kb):
    a, b = tenants
    s = _source(engine, a, kb[a], _conn(engine, a).id)
    entities = EntityStore(engine)
    e = entities.upsert_entity(a, s.id, entity_type="generic_part", canonical_name="LM317",
                               lifecycle_status="active", attributes={"family": "ldo"})
    same = entities.upsert_entity(a, s.id, entity_type="generic_part", canonical_name="LM317",
                                  lifecycle_status="nrnd", attributes={"family": "reg"})
    assert same.id == e.id and same.lifecycle_status == "nrnd" and same.attributes == {"family": "reg"}
    assert entities.get_entity(b, e.id) is None and entities.get_entity(a, e.id).canonical_name == "LM317"
    with pytest.raises(ValueError):
        entities.upsert_entity(b, s.id, entity_type="generic_part", canonical_name="X")

    entities.set_identifiers(a, e.id, [("part_number", "LM317", True), ("gpn", "LM317", False)])
    entities.set_identifiers(a, e.id, [("part_number", "LM317", True), ("alias", "LM317T", False)])  # replaces
    assert sorted(entities.list_identifiers(a, e.id)) == [("alias", "LM317T", False), ("part_number", "LM317", True)]
    assert entities.find_by_identifier(a, s.id, "alias", "LM317T").id == e.id
    assert entities.find_by_identifier(b, s.id, "alias", "LM317T") is None
    with pytest.raises(ValueError):
        entities.set_identifiers(b, e.id, [("x", "y", True)])

    rows = [
        {"canonical_name": "vin_max", "source_name": "Vin (max)", "value_type": "numeric", "numeric_value": 40.0,
         "unit": "V", "normalized_value": 40.0, "normalized_unit": "V", "max_value": 40.0, "confidence": 0.9,
         "source_path": "Parametric[3]"},
        {"canonical_name": "package", "source_name": "Package", "value_type": "text", "text_value": "TO-220"},
    ]
    entities.replace_parameters(a, e.id, rows)
    got = entities.list_parameters(a, e.id)
    assert [p["canonical_name"] for p in got] == ["package", "vin_max"]
    assert got[1]["max_value"] == 40.0 and got[1]["unit"] == "V" and got[0]["text_value"] == "TO-220"
    entities.replace_parameters(a, e.id, rows[:1])
    assert [p["canonical_name"] for p in entities.list_parameters(a, e.id)] == ["vin_max"]
    with pytest.raises(ValueError):
        entities.replace_parameters(a, e.id, [rows[0], rows[0]])  # duplicate (canonical_name, source_name)
    assert [p["canonical_name"] for p in entities.list_parameters(a, e.id)] == ["vin_max"]  # unchanged
    with pytest.raises(ValueError):
        entities.replace_parameters(b, e.id, rows)
    with pytest.raises(ValueError):
        entities.replace_parameters(a, e.id, [{"canonical_name": "x", "source_name": "x", "value_type": "weird"}])


def test_entity_document_ledger_lifecycle(engine, tenants, kb):
    a, b = tenants
    s = _source(engine, a, kb[a], _conn(engine, a).id)
    entities = EntityStore(engine)
    e = entities.upsert_entity(a, s.id, entity_type="generic_part", canonical_name="LM317")
    key1 = f"source:{s.id}:generic_part:LM317:datasheet"
    key2 = f"source:{s.id}:generic_part:LM317:errata:abcd1234"
    assert entities.upsert_document(a, e.id, document_key=key1, relationship="datasheet", source_url=URL,
                                    source_revision="rev-a") is True
    assert entities.upsert_document(a, e.id, document_key=key1, relationship="datasheet", source_url=URL) is False
    assert entities.upsert_document(a, e.id, document_key=key2, relationship="errata", source_url=URL + "e") is True
    pending = entities.pending_documents(a, s.id, 10)
    assert {d.document_key for d in pending} == {key1, key2}
    assert all(d.status == "discovered" and d.entity_id == e.id for d in pending)
    assert len(entities.pending_documents(a, s.id, 1)) == 1 and entities.pending_documents(b, s.id, 10) == []
    first = entities.get_document(a, e.id, key1)
    assert first.source_revision == "rev-a" and first.etag is None and first.last_modified is None

    assert entities.mark_document_queued(
        b, e.id, key1, content_hash="h", etag='"x"', last_modified="Tue, 17 Nov 2026 10:00:00 GMT"
    ) is False
    assert entities.mark_document_queued(
        a, e.id, key1, content_hash="h1", etag='"abc"', last_modified="Tue, 17 Nov 2026 10:00:00 GMT",
        source_revision="rev-b",
    ) is True
    d = entities.get_document(a, e.id, key1)
    assert (d.status, d.content_hash, d.etag, d.source_revision) == ("queued", "h1", '"abc"', "rev-b")
    assert d.last_modified == "Tue, 17 Nov 2026 10:00:00 GMT" and d.queued_at is not None and d.error_code is None
    assert [x.document_key for x in entities.pending_documents(a, s.id, 10)] == [key2]
    assert entities.mark_document_failed(a, e.id, key2, "too_large") is True
    f = entities.get_document(a, e.id, key2)
    assert (f.status, f.error_code) == ("failed", "too_large") and entities.pending_documents(a, s.id, 10) == []
    with pytest.raises(ValueError):
        entities.upsert_document(a, e.id, document_key="x" * 257, relationship="r", source_url=URL)
    with pytest.raises(ValueError):
        entities.upsert_document(b, e.id, document_key="k", relationship="r", source_url=URL)


def test_deleting_the_knowledge_base_cascades_to_everything_under_its_sources(engine, tenants, kb):
    a, _ = tenants
    s = _source(engine, a, kb[a], _conn(engine, a).id)
    SyncStore(engine).create(a, str(uuid.uuid4()), s.id, str(uuid.uuid4()), "full", "h")
    RecordStore(engine).upsert(a, s.id, "LM317", "generic", {"x": 1}, observed_at=NOW)
    entities = EntityStore(engine)
    e = entities.upsert_entity(a, s.id, entity_type="generic_part", canonical_name="LM317")
    entities.set_identifiers(a, e.id, [("pn", "LM317", True)])
    entities.replace_parameters(a, e.id, [{"canonical_name": "c", "source_name": "s", "value_type": "text"}])
    entities.upsert_document(a, e.id, document_key="k1", relationship="datasheet", source_url=URL)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": a})
    with engine.connect() as conn:
        for table in ("kb_sources", "kb_source_syncs", "kb_source_records", "kb_entities", "kb_entity_identifiers",
                      "kb_entity_parameters", "kb_entity_documents"):
            assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": a}).scalar_one() == 0, table


# ------------------------------------------------------------------------------------- run kind


def test_sync_run_kind_is_created_for_the_sources_knowledge_base(engine, tenants, kb):
    a, b = tenants
    assert "kb_source_sync" in RUN_KINDS
    s = _source(engine, a, kb[a], _conn(engine, a).id)
    store = RunStore(engine)
    run_id = uuid.uuid4()
    request = {"mode": "targeted", "part_numbers": ["LM317"]}
    got, created = store.create_kb_source_sync_run(a, run_id, s.id, request)
    assert got == run_id and created is True
    run = store.get_run(a, run_id)
    assert run["kind"] == "kb_source_sync" and run["knowledge_base_id"] == kb[a] and run["status"] == "queued"
    body = json.loads(run["input"])
    assert body["source_id"] == s.id and body["mode"] == "targeted" and body["part_numbers"] == ["LM317"]
    assert store.create_kb_source_sync_run(a, run_id, s.id, request) == (run_id, False)
    with pytest.raises(ValueError):
        store.create_kb_source_sync_run(b, uuid.uuid4(), s.id, request)


def test_api_token_connection_takes_an_optional_token_ref_and_repository_docs_sources(engine, tenants, kb):
    a, _ = tenants
    store = ConnectionStore(engine)
    anonymous = store.create(a, name="tok0", kind="api_token", base_url="https://api.example.test", config={}, credential_refs={})
    with_token = store.create(
        a, name="tok1", kind="api_token", base_url=None, config={}, credential_refs={"token": "secret:example-token"}
    )
    assert anonymous.credential_refs == {} and with_token.credential_refs == {"token": "secret:example-token"}
    with pytest.raises(ValueError):
        store.create(a, name="tok2", kind="api_token", base_url=None, config={}, credential_refs={"client_id": "secret:x"})
    s = SourceStore(engine).create(
        a, knowledge_base_id=kb[a], connection_id=anonymous.id, name="docs", kind="repository_docs", provider="github",
        config={"repository": "o/r"},
    )
    assert s.kind == "repository_docs"
