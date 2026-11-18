"""The knowledge-base source sync workflow (`eap.kb_source_sync`) against the fake catalogue.

The adapter is `FakeCatalogAdapter` (deterministic, records every call); the kb_ingest enqueue is
replaced by a recorder, so nothing here embeds or reaches a provider. Review focus: replay
(`test_segment_step_is_idempotent`, the child workflow ids), secrets
(`test_secret_values_absent_from_events_and_outputs`) and quota
(`test_full_sync_carries_the_cursor_when_the_budget_is_spent`)."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from eap_kb_sources.registry import register_adapter
from eap_run_store import KnowledgeStore
from eap_run_store.kb_sources import ConnectionStore, EntityStore, RecordStore, SourceStore, SyncStore
from eap_worker import kb_source_sync as sync
from worker_tests.conftest import DBOS_DB_URL
from worker_tests.kb_ingest_helpers import cleanup_knowledge, create_kb

REPORT_KEYS = {
    "source_id", "mode", "status", "discovered", "inserted", "updated", "unchanged", "documents_discovered",
    "documents_queued", "failed", "checkpoint", "errors", "complete", "stop_reason", "calls_used", "started_at",
    "completed_at", "removed", "budget", "source_revision",
}


@pytest.fixture
def fake():
    from eap_kb_sources.fake import FakeCatalogAdapter

    adapter = FakeCatalogAdapter(size=12, page_size=5)
    register_adapter("product_catalog", "fake", lambda: adapter)
    return adapter


@pytest.fixture
def launched(monkeypatch):
    """Every kb_ingest hand-off of the sync, instead of a DBOS enqueue."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sync, "_launch_kb_ingest", lambda **kw: calls.append(kw))
    return calls


@pytest.fixture
def env(worker, tenant, fake, launched):
    kb_store = KnowledgeStore(worker.engine)
    kb_id = create_kb(kb_store, tenant)
    connection = ConnectionStore(worker.engine).create(
        tenant, name="fake", kind="oauth2_client_credentials", base_url=None, config={}, credential_refs={}
    )
    source = SourceStore(worker.engine).create(
        tenant, knowledge_base_id=kb_id, connection_id=connection.id, name="parts", kind="product_catalog",
        provider="fake", config={"document_policy": {"types": ["datasheet"]}},
    )
    yield SimpleNamespace(
        tenant=tenant, kb_id=kb_id, connection=connection, source=source, engine=worker.engine, rt=worker,
        sources=SourceStore(worker.engine), records=RecordStore(worker.engine), entities=EntityStore(worker.engine),
        syncs=SyncStore(worker.engine), kb=kb_store, settings=worker.settings,
    )
    cleanup_knowledge(worker.engine, tenant)
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": tenant})


def start(env, request: dict[str, Any] | None = None, *, tenant: str | None = None):
    """Create the run row (as the API does) and run the workflow to its end: (run_id, result)."""
    from dbos import DBOS, SetWorkflowID

    request = {"mode": "full", **(request or {})}
    run_id = uuid.uuid4()
    env.rt.run_store.create_kb_source_sync_run(env.tenant, run_id, env.source.id, request)
    with SetWorkflowID(str(run_id)):
        handle = DBOS.start_workflow(
            sync.kb_source_sync, str(run_id), tenant or env.tenant, env.source.id, json.dumps(request)
        )
    return run_id, handle.get_result()


def report(result):
    return result["report"]


def keys(n: int, start_at: int = 1) -> list[str]:
    return [f"FAKE{i:04d}" for i in range(start_at, start_at + n)]


def scalar(env, sql: str, **params):
    with env.engine.connect() as conn:
        return conn.execute(text(sql), {"t": env.tenant, "s": env.source.id, **params}).scalar_one()


def doc_keys(env) -> list[str]:
    with env.engine.connect() as conn:
        return sorted(
            r[0] for r in conn.execute(text("SELECT document_key FROM kb_entity_documents WHERE tenant_id = :t"), {"t": env.tenant})
        )


# ------------------------------------------------------------------------------------------ targeted


def test_targeted_sync_stores_records_entities_documents_and_hands_off_to_kb_ingest(env, fake, launched):
    run_id, result = start(env, {"mode": "targeted", "part_numbers": ["FAKE0001", "FAKE0002"]})
    rep = report(result)
    assert result["status"] == "completed" and set(rep) == REPORT_KEYS
    assert (rep["mode"], rep["complete"], rep["stop_reason"], rep["status"]) == ("targeted", True, None, "completed")
    # per key: product, parametrics and quality raw records
    assert (rep["discovered"], rep["inserted"], rep["updated"], rep["unchanged"], rep["failed"]) == (2, 6, 0, 0, 0)
    assert (rep["documents_discovered"], rep["documents_queued"]) == (2, 2) and rep["errors"] == []
    assert rep["calls_used"] == 2 and fake.count("fetch_record") == 2 and fake.count("fetch_document") == 2

    entity = env.entities.find_by_identifier(env.tenant, env.source.id, "orderable_part_number", "FAKE0001-B")
    assert entity is not None and entity.canonical_name == "FAKE0001"
    assert env.entities.list_parameters(env.tenant, entity.id)[0]["canonical_name"] == "supply_voltage"
    assert doc_keys(env) == [f"source:{env.source.id}:product:FAKE0001:datasheet", f"source:{env.source.id}:product:FAKE0002:datasheet"]
    doc = env.entities.get_document(env.tenant, entity.id, doc_keys(env)[0])
    assert doc.status == "queued" and doc.content_hash and doc.etag == "rev1" and doc.kb_document_id

    assert len(launched) == 2 and launched[0]["tenant_id"] == env.tenant and launched[0]["knowledge_base_id"] == env.kb_id
    assert json.loads(launched[0]["input_json"])["doc_version"] == 1
    kb_run = env.rt.run_store.get_run(env.tenant, launched[0]["run_id"])
    assert kb_run["kind"] == "kb_ingest" and kb_run["status"] == "queued"

    row = env.rt.run_store.get_run(env.tenant, run_id)
    assert row["status"] == "completed" and row["kind"] == "kb_source_sync" and json.loads(row["output"]) == rep
    end = env.rt.event_log.get_event(env.tenant, str(run_id), "end")
    assert end.type == "run_completed" and end.data["report"] == rep
    sync_row = env.syncs.get_by_run(env.tenant, str(run_id))
    assert sync_row.status == "completed" and sync_row.mode == "targeted" and sync_row.counters["discovered"] == 2
    # a targeted sync never touches the walk cursor; it is the source's `last_successful_sync_at` that moves
    source = env.sources.get(env.tenant, env.source.id)
    assert source.last_successful_sync_at is not None and source.last_checkpoint["cursor"]["daily_budget"]["used"] == {"product": 2}


def test_a_repeated_targeted_sync_changes_nothing_and_downloads_nothing(env, fake, launched):
    start(env, {"mode": "targeted", "part_numbers": ["FAKE0001", "FAKE0002"]})
    _, result = start(env, {"mode": "targeted", "part_numbers": ["FAKE0001", "FAKE0002"]})
    rep = report(result)
    assert (rep["inserted"], rep["updated"], rep["unchanged"]) == (0, 0, 6)
    assert rep["documents_queued"] == 0 and rep["documents_discovered"] == 0
    assert fake.count("fetch_document") == 2 and len(launched) == 2  # the ETag matched: no download, no ingest
    assert len(doc_keys(env)) == 2


def test_targeted_runs_in_segments_of_twenty_parts_as_child_workflows(env, fake):
    fake.size = 60
    fake.revision = {f"FAKE{i:04d}": 1 for i in range(1, 61)}
    run_id, result = start(env, {"mode": "targeted", "part_numbers": keys(45)})
    rep = report(result)
    assert rep["discovered"] == 45 and rep["complete"] is True
    assert env.syncs.get_by_run(env.tenant, str(run_id)).checkpoint["segments"] == 3
    assert _workflow_ids(env, f"{run_id}:seg:%") == [f"{run_id}:seg:{n}" for n in (1, 2, 3)]


def _workflow_ids(env, like: str) -> list[str]:
    engine = create_engine(DBOS_DB_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(f'SELECT workflow_uuid FROM "{env.settings.dbos_system_schema}".workflow_status WHERE workflow_uuid LIKE :w ORDER BY 1'),
                {"w": like},
            )
            return [r[0] for r in rows]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "request_body",
    [
        {"mode": "targeted"},
        {"mode": "targeted", "part_numbers": []},
        {"mode": "targeted", "part_numbers": ["bad part"]},
        {"mode": "targeted", "part_numbers": [f"P{i}" for i in range(101)]},
        {"mode": "full", "part_numbers": ["FAKE0001"]},
        {"mode": "sideways"},
        {"mode": "full", "page_budget": 0},
    ],
)
def test_an_invalid_request_fails_the_run_cleanly(env, request_body):
    run_id, result = start(env, request_body)
    assert result["status"] == "failed" and result["error_code"] == "invalid_request"
    row = env.rt.run_store.get_run(env.tenant, run_id)
    assert row["status"] == "failed" and json.loads(row["output"])["error_code"] == "invalid_request"


def test_a_source_of_another_tenant_fails_with_source_not_found_and_reads_nothing(env, fake):
    other = f"t_{uuid.uuid4().hex[:10]}"
    run_id, result = start(env, {"mode": "full"}, tenant=other)
    assert result["status"] == "failed" and result["error_code"] == "source_not_found"
    assert fake.calls == [] and env.syncs.get_by_run(other, str(run_id)) is None


def test_a_disabled_connection_or_unknown_adapter_fails_the_run(env, fake):
    ConnectionStore(env.engine).update(env.tenant, env.connection.id, enabled=False)
    _, result = start(env, {"mode": "full"})
    assert result["error_code"] == "connection_disabled" and fake.calls == []
    ConnectionStore(env.engine).update(env.tenant, env.connection.id, enabled=True)
    with env.engine.begin() as conn:
        conn.execute(text("UPDATE kb_sources SET provider = 'nobody' WHERE tenant_id = :t AND id = :s"), {"t": env.tenant, "s": env.source.id})
    _, result = start(env, {"mode": "full"})
    assert result["error_code"] == "source_adapter_not_found"


# ------------------------------------------------------------------------------------ full and incremental


def test_full_sync_walks_segments_until_the_catalogue_is_done(env, fake):
    run_id, result = start(env, {"mode": "full", "page_budget": 1})
    rep = report(result)
    assert (rep["complete"], rep["discovered"], rep["failed"], rep["stop_reason"]) == (True, 12, 0, None)
    assert fake.count("sync_segment") == 3 and rep["checkpoint"]["segments"] == 3
    source = env.sources.get(env.tenant, env.source.id)
    assert source.last_successful_sync_at is not None and source.last_checkpoint["cursor"]["changed_since"] == fake._clock
    assert _workflow_ids(env, f"{run_id}:seg:%") == [f"{run_id}:seg:{n}" for n in (1, 2, 3)]
    assert env.syncs.get_by_run(env.tenant, str(run_id)).status == "completed"


def test_incremental_is_a_noop_then_fetches_only_what_changed(env, fake, launched):
    start(env, {"mode": "full"})
    fetched, handed = fake.count("fetch_record"), len(launched)
    _, result = start(env, {"mode": "incremental"})
    rep = report(result)
    assert (rep["complete"], rep["discovered"], rep["inserted"], rep["updated"]) == (True, 0, 0, 0)
    assert fake.count("fetch_record") == fetched and len(launched) == handed

    fake.bump("FAKE0002")
    fake.bump("FAKE0009")
    _, result = start(env, {"mode": "incremental"})
    rep = report(result)
    assert fake.count("fetch_record") == fetched + 2
    assert (rep["discovered"], rep["updated"], rep["unchanged"]) == (2, 4, 2)  # product and quality carry the revision
    assert rep["documents_queued"] == 2 and len(launched) == handed + 2
    assert sorted(json.loads(c["input_json"])["doc_version"] for c in launched[handed:]) == [2, 2]


def test_full_sync_carries_the_cursor_when_the_budget_is_spent(env, fake):
    fake.daily_limit = 7
    from datetime import date

    fake.today = date(2026, 11, 18)
    run_id, first = start(env, {"mode": "full", "page_budget": 1})
    rep = report(first)
    assert first["status"] == "completed" and rep["complete"] is False and rep["stop_reason"] == "budget"
    assert rep["discovered"] == 7 and rep["calls_used"] == 7  # refused attempts are not calls
    source = env.sources.get(env.tenant, env.source.id)
    assert source.last_checkpoint["cursor"]["cursor"] == 5  # the second segment did not finish its page
    assert source.last_checkpoint["cursor"]["daily_budget"]["used"] == {"product": 7} and source.last_successful_sync_at is None
    assert env.syncs.get_by_run(env.tenant, str(run_id)).status == "completed"

    before = fake.count("fetch_record")
    _, again = start(env, {"mode": "full", "page_budget": 1})  # the same day: nothing left, stops at once
    assert report(again)["stop_reason"] == "budget" and report(again)["discovered"] == 0
    assert fake.count("fetch_record") == before + 1

    fake.daily_limit = 100  # a raised quota continues from the stored cursor, not from the start
    fake.calls.clear()
    _, last = start(env, {"mode": "full", "page_budget": 1})
    assert report(last)["complete"] is True
    assert [k for m, k in fake.calls if m == "fetch_record"] == keys(7, 6)
    assert env.sources.get(env.tenant, env.source.id).last_successful_sync_at is not None


def test_max_segments_stops_the_run_with_a_resumable_cursor(env, fake):
    _, result = start(env, {"mode": "full", "page_budget": 1, "max_segments": 2})
    rep = report(result)
    assert (rep["complete"], rep["stop_reason"], rep["discovered"]) == (False, "max_segments", 10)
    assert env.sources.get(env.tenant, env.source.id).last_checkpoint["cursor"]["cursor"] == 10


# ------------------------------------------------------------------------------------ failures


def test_a_missing_part_is_counted_not_raised(env, fake):
    fake.fail = {"FAKE0002": "not_found", "FAKE0004": "provider_error"}
    _, result = start(env, {"mode": "targeted", "part_numbers": keys(5)})
    rep = report(result)
    assert result["status"] == "completed" and rep["complete"] is True and (rep["discovered"], rep["failed"]) == (3, 2)
    assert {(e["external_id"], e["code"]) for e in rep["errors"]} == {("FAKE0002", "not_found"), ("FAKE0004", "provider_error")}


def test_rejected_credentials_end_the_run_needs_attention_and_keep_the_cursor(env, fake):
    start(env, {"mode": "full", "page_budget": 1, "max_segments": 1})
    stored = env.sources.get(env.tenant, env.source.id).last_checkpoint
    fake.unauthorized = True
    run_id, result = start(env, {"mode": "full", "page_budget": 1})
    assert result["status"] == "needs_attention" and result["error_code"] == "connection_auth_failed"
    row = env.rt.run_store.get_run(env.tenant, run_id)
    assert row["status"] == "needs_attention" and json.loads(row["output"])["error_code"] == "connection_auth_failed"
    assert env.syncs.get_by_run(env.tenant, str(run_id)).status == "needs_attention"
    assert env.sources.get(env.tenant, env.source.id).last_checkpoint == stored


def test_the_report_stays_bounded_with_many_errors(env, fake):
    fake.size = 90
    fake.revision = {f"FAKE{i:04d}": 1 for i in range(1, 91)}
    fake.fail = {f"FAKE{i:04d}": "not_found" for i in range(1, 91)}
    _, result = start(env, {"mode": "full", "page_budget": 25})
    rep = report(result)
    assert rep["failed"] == 90 and len(rep["errors"]) == 50
    assert all(len(e["message"]) <= 200 for e in rep["errors"]) and len(json.dumps(rep)) < 32 * 1024


def test_a_document_over_the_policy_cap_is_a_failed_document_not_a_failed_run(env, fake, launched):
    with env.engine.begin() as conn:
        conn.execute(
            text("UPDATE kb_sources SET config = CAST(:c AS jsonb) WHERE tenant_id = :t AND id = :s"),
            {"c": json.dumps({"document_policy": {"types": ["datasheet"], "max_bytes": 10}}), "t": env.tenant, "s": env.source.id},
        )
    _, result = start(env, {"mode": "targeted", "part_numbers": ["FAKE0001"]})
    rep = report(result)
    assert result["status"] == "completed" and rep["failed"] == 1 and launched == []
    assert rep["errors"][0]["code"] == "response_too_large"
    entity = env.entities.find_by_identifier(env.tenant, env.source.id, "generic_part_number", "FAKE0001")
    doc = env.entities.get_document(env.tenant, entity.id, doc_keys(env)[0])
    assert (doc.status, doc.error_code) == ("failed", "response_too_large")


def test_a_failed_enqueue_marks_the_ingest_run_failed_and_the_document_failed(env, fake, monkeypatch):
    def refuse(**kwargs):
        raise RuntimeError("queue down: postgresql://user:password@host/db")

    monkeypatch.setattr(sync, "_launch_kb_ingest", refuse)
    _, result = start(env, {"mode": "targeted", "part_numbers": ["FAKE0001"]})
    rep = report(result)
    assert rep["failed"] == 1 and rep["errors"][0]["code"] == "enqueue_failed" and "password" not in json.dumps(rep)
    run_id = scalar(env, "SELECT id FROM runs WHERE tenant_id = :t AND kind = 'kb_ingest'")
    assert env.rt.run_store.get_run(env.tenant, run_id)["status"] == "failed"


# ------------------------------------------------------------------------------------ cancel


def test_cancel_requested_stops_before_the_next_segment_and_keeps_the_checkpoint(env, fake):
    run_ids: list[uuid.UUID] = []

    def cancel_during_second_segment(method: str, key: str | None) -> None:
        if method == "sync_segment" and fake.count("sync_segment") == 2:
            env.rt.event_log.append(env.tenant, run_ids[0], "cancel", "cancel_requested", {"requested_by": "u1"})

    fake.hook = cancel_during_second_segment
    from dbos import DBOS, SetWorkflowID

    run_id = uuid.uuid4()
    run_ids.append(run_id)
    request = {"mode": "full", "page_budget": 1}
    env.rt.run_store.create_kb_source_sync_run(env.tenant, run_id, env.source.id, request)
    with SetWorkflowID(str(run_id)):
        result = DBOS.start_workflow(
            sync.kb_source_sync, str(run_id), env.tenant, env.source.id, json.dumps(request)
        ).get_result()
    rep = report(result)
    # the started segment finished its bounded step; the next one never began
    assert result["error_code"] == "run_cancelled" and fake.count("sync_segment") == 2 and rep["discovered"] == 10
    assert env.sources.get(env.tenant, env.source.id).last_checkpoint["cursor"]["cursor"] == 10
    row = env.rt.run_store.get_run(env.tenant, run_id)
    assert row["status"] == "failed" and env.syncs.get_by_run(env.tenant, str(run_id)).status == "failed"
    end = env.rt.event_log.get_event(env.tenant, str(run_id), "end")
    assert end.type == "run_failed" and end.data["error_code"] == "run_cancelled"


# ------------------------------------------------------------------------------------ replay


def _ctx(env) -> dict[str, Any]:
    return sync._load_context(env.tenant, env.source.id)


def _snapshot(env) -> dict[str, Any]:
    with env.engine.connect() as conn:
        records = conn.execute(
            text("SELECT external_id, record_type, content_hash, status FROM kb_source_records"
                 " WHERE tenant_id = :t ORDER BY 1, 2"), {"t": env.tenant}).all()
        docs = conn.execute(
            text("SELECT document_key, status, content_hash, etag, queued_at FROM kb_entity_documents"
                 " WHERE tenant_id = :t ORDER BY 1"), {"t": env.tenant}).all()
        entities = conn.execute(text("SELECT canonical_name, attributes FROM kb_entities WHERE tenant_id = :t ORDER BY 1"), {"t": env.tenant}).all()
    return {"records": [tuple(r) for r in records], "entities": [tuple(e) for e in entities],
            "docs_keys": [d[:4] for d in docs]}


def test_segment_step_is_idempotent(env, fake, launched):
    """Re-executing the step after a crash (its checkpoint was never written) changes nothing: the
    same hashes, the same knowledge-base version, no second ingest run."""
    args = (str(uuid.uuid4()), env.tenant, env.source.id, 1, _ctx(env), "targeted", {}, keys(3), 10, 100)
    env.rt.run_store.create_kb_source_sync_run(env.tenant, uuid.UUID(args[0]), env.source.id, {"mode": "targeted"})
    first = asyncio.run(sync.segment_step(*args))
    assert first["counters"]["inserted"] == 9 and first["counters"]["documents_queued"] == 3 and len(launched) == 3
    snapshot = _snapshot(env)
    second = asyncio.run(sync.segment_step(*args))
    assert (second["counters"]["inserted"], second["counters"]["updated"], second["counters"]["unchanged"]) == (0, 0, 9)
    assert second["counters"]["documents_queued"] == 0
    assert _snapshot(env) == snapshot and len(launched) == 3
    assert scalar(env, "SELECT count(*) FROM runs WHERE tenant_id = :t AND kind = 'kb_ingest'") == 3


def test_a_crash_between_the_hand_off_and_the_ledger_row_replays_without_a_second_version(env, fake, launched):
    """The ledger still says `discovered` (the crash point kb_source_document_handed_off): the replay
    finds the same stored version, makes no second run and no second version, and completes the ledger."""
    args = (str(uuid.uuid4()), env.tenant, env.source.id, 1, _ctx(env), "targeted", {}, keys(2), 10, 100)
    env.rt.run_store.create_kb_source_sync_run(env.tenant, uuid.UUID(args[0]), env.source.id, {"mode": "targeted"})
    asyncio.run(sync.segment_step(*args))
    with env.engine.begin() as conn:
        conn.execute(text("UPDATE kb_entity_documents SET status = 'discovered', queued_at = NULL, etag = NULL, content_hash = NULL"
                          " WHERE tenant_id = :t"), {"t": env.tenant})
    replay = asyncio.run(sync.segment_step(*args))
    assert replay["counters"]["documents_queued"] == 0 and len(launched) == 2
    assert scalar(env, "SELECT count(*) FROM kb_documents WHERE tenant_id = :t AND latest_version <> 1") == 0
    with env.engine.connect() as conn:
        states = conn.execute(text("SELECT status, etag FROM kb_entity_documents WHERE tenant_id = :t"), {"t": env.tenant}).all()
    assert {tuple(r) for r in states} == {("queued", "rev1")}


def test_a_stored_version_whose_run_row_was_never_created_gets_its_run(env, fake, launched):
    """A hand-off that died after the document write and before its run insert: the replay starts the run."""
    args = (str(uuid.uuid4()), env.tenant, env.source.id, 1, _ctx(env), "targeted", {}, keys(1), 10, 100)
    env.rt.run_store.create_kb_source_sync_run(env.tenant, uuid.UUID(args[0]), env.source.id, {"mode": "targeted"})
    asyncio.run(sync.segment_step(*args))
    with env.engine.begin() as conn:
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t AND kind = 'kb_ingest'"), {"t": env.tenant})
        conn.execute(text("UPDATE kb_entity_documents SET status = 'discovered', etag = NULL WHERE tenant_id = :t"), {"t": env.tenant})
    replay = asyncio.run(sync.segment_step(*args))
    assert replay["counters"]["documents_queued"] == 1 and len(launched) == 2
    assert scalar(env, "SELECT count(*) FROM runs WHERE tenant_id = :t AND kind = 'kb_ingest'") == 1


# ------------------------------------------------------------------------------------ secrets


def test_secret_values_absent_from_events_and_outputs(env, fake, worker, monkeypatch):
    from eap_run_store.secrets import SecretStore
    from eap_secrets import KEY_ENV, generate_key, load_keyring

    monkeypatch.setenv(KEY_ENV, generate_key())
    secret, ident = "client-secret-VALUE-0123456789", "client-id-VALUE-9876543210"
    store = SecretStore(worker.engine, load_keyring())
    store.put(env.tenant, "conn-secret", secret, "u1")
    store.put(env.tenant, "conn-id", ident, "u1")
    ConnectionStore(env.engine).update(
        env.tenant, env.connection.id, credential_refs={"client_id": "secret:conn-id", "client_secret": "secret:conn-secret"}
    )
    fake.fail = {"FAKE0002": "provider_error"}
    run_id, result = start(env, {"mode": "targeted", "part_numbers": ["FAKE0001", "FAKE0002"]})
    assert result["status"] == "completed"
    assert {"client_id": ident, "client_secret": secret} in fake.secret_values  # the adapter did get them
    haystack: list[str] = []
    with env.engine.connect() as conn:
        for sql in (
            "SELECT * FROM execution_events WHERE run_id = :r", "SELECT * FROM runs WHERE tenant_id = :t",
            "SELECT * FROM kb_source_syncs WHERE tenant_id = :t", "SELECT * FROM kb_sources WHERE tenant_id = :t",
            "SELECT * FROM kb_source_records WHERE tenant_id = :t", "SELECT * FROM kb_entity_documents WHERE tenant_id = :t",
            "SELECT * FROM kb_entities WHERE tenant_id = :t",
        ):
            haystack += [str(r) for r in conn.execute(text(sql), {"r": str(run_id), "t": env.tenant})]
    schema = env.settings.dbos_system_schema
    dbos_engine = create_engine(DBOS_DB_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with dbos_engine.connect() as conn:
            for table in ("operation_outputs", "workflow_status"):
                haystack += [
                    str(r) for r in conn.execute(
                        text(f'SELECT * FROM "{schema}".{table} WHERE workflow_uuid LIKE :w'), {"w": f"{run_id}%"}
                    )
                ]
    finally:
        dbos_engine.dispose()
    blob = "\n".join(haystack)
    assert blob and secret not in blob and ident not in blob


def test_an_unstored_secret_ends_the_run_needs_attention_without_naming_a_value(env, fake, monkeypatch):
    from eap_secrets import KEY_ENV, generate_key

    monkeypatch.setenv(KEY_ENV, generate_key())
    ConnectionStore(env.engine).update(env.tenant, env.connection.id, credential_refs={"client_secret": "secret:never-stored"})
    _, result = start(env, {"mode": "targeted", "part_numbers": ["FAKE0001"]})
    assert result["status"] == "needs_attention" and result["error_code"] == "secret_missing"
    assert fake.calls == [("sync_segment", None)] or fake.calls == []


# ------------------------------------------------------------------------------------ misc


def test_tenant_isolation_of_the_stores(env, fake):
    start(env, {"mode": "targeted", "part_numbers": ["FAKE0001"]})
    other = "t_other_" + uuid.uuid4().hex[:6]
    assert env.records.get(other, env.source.id, "FAKE0001", "product") is None
    assert env.entities.find_by_identifier(other, env.source.id, "generic_part_number", "FAKE0001") is None
    assert SourceStore(env.engine).get(other, env.source.id) is None and env.syncs.list_for_source(other, env.source.id, 10) == []


def test_the_workflow_and_step_names_are_frozen():
    assert sync.WORKFLOW_NAME == "eap.kb_source_sync" and sync.SEGMENT_WORKFLOW_NAME == "eap.kb_source_segment"
    assert sync.STEP_NAMES == (
        "eap.kb_source_start", "eap.kb_source_sync_segment", "eap.kb_source_checkpoint", "eap.kb_source_finalize",
        "eap.kb_source_mark_failed",
    )


def test_adapter_diagnostics_reach_the_report_and_the_run_output_bounded_and_scrubbed(env, fake):
    from dataclasses import replace

    original = fake.sync_segment

    async def with_diagnostics(*args, **kwargs):
        result = await original(*args, **kwargs)
        page = {"page": 0, "http_status": 200, "json_type": "object", "keys": ["Rows"], "expected": "expected key Content"}
        return replace(result, diagnostics={"listing": [page] * 10})

    fake.sync_segment = with_diagnostics
    run_id, result = start(env, {"mode": "full"})
    rep = report(result)
    assert 1 <= len(rep["diagnostics"]["listing"]) <= 3 and rep["diagnostics"]["listing"][0]["keys"] == ["Rows"]
    stored = json.loads(env.rt.run_store.get_run(env.tenant, run_id)["output"])
    assert stored["diagnostics"] == rep["diagnostics"]


def test_a_sync_without_diagnostics_has_no_diagnostics_key(env, fake):
    _, result = start(env, {"mode": "full"})
    assert "diagnostics" not in report(result)
