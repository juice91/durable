"""P1.C4 acceptance, the in-flight race: a `knowledge.search` tester run whose search ALREADY
returned hits, when the document (or the knowledge base) is deleted before the run finishes.

The delete scrubs the runs that exist at that moment (eap_run_store.knowledge
`_scrub_search_tester_runs`: for a document, the runs with a `tool_result` naming it; for a base, its
runs by `runs.knowledge_base_id`). The run's later result writes (the `tool_result` and `end`
events, eap.finish_run) lock the knowledge base and the hits' documents FOR SHARE and leave out the
deleted ones (eap_worker.search_guard), so no deleted text is stored after the delete. A deleted
knowledge base fails the run cleanly (`knowledge_base_deleted`, no content).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text

from eap_run_store import KnowledgeStore
from eap_worker import platform_tools
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.test_retrieve import ingest, kb_tenant, make_kb  # noqa: F401 - fixture

SECRET = "zebrafruit"
SECTIONS = [("Fruit", f"The {SECRET} orchard stores {SECRET} crates in building nine, {SECRET} only.")]


def _enqueue(worker, client, tenant: str, kb_id: str) -> uuid.UUID:
    run_id = uuid.uuid4()
    args = {"knowledge_base_id": kb_id, "query": "where are the crates stored", "top_k": 3}
    input_json = json.dumps(args, sort_keys=True, separators=(",", ":"))
    worker.run_store.create_tool_run(tenant, "knowledge.search", input_json, run_id, knowledge_base_id=kb_id)
    client.enqueue(
        {"queue_name": "eap-agent-runs", "workflow_name": "eap.tool_run", "workflow_id": str(run_id),
         "app_version": APP_VERSION},
        str(run_id), tenant, "knowledge.search", input_json,
    )
    return run_id


def _delete_after_the_search(monkeypatch, delete) -> list[int]:
    """The real search runs; then `delete()` happens before the tool returns its hits."""
    real, seen = platform_tools.search_knowledge, []

    async def wrapper(*args, **kwargs):
        hits = await real(*args, **kwargs)
        seen.append(len(hits))
        await asyncio.to_thread(delete)
        return hits

    monkeypatch.setattr(platform_tools, "search_knowledge", wrapper)
    return seen


def _stored_text(worker, tenant: str, run_id) -> str:
    with worker.engine.connect() as conn:
        row = conn.execute(text("SELECT input, output, error FROM runs WHERE tenant_id = :t AND id = :r"),
                           {"t": tenant, "r": str(run_id)}).one()
    return json.dumps([list(row), [(k, t, d) for _, k, t, d in event_rows(worker.engine, run_id)]], default=str)


def _finish(worker, client, tenant, kb_id) -> tuple[uuid.UUID, dict]:
    run_id = _enqueue(worker, client, tenant, kb_id)
    return run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=60)


def test_the_race_setup_returns_hits_and_deletes_in_between(worker, client, kb_tenant, monkeypatch):  # noqa: F811
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "orchard.md", SECTIONS)
    store = KnowledgeStore(worker.engine)
    seen = _delete_after_the_search(monkeypatch, lambda: store.delete_document(kb_tenant, kb["id"], "orchard.md"))
    run_id, row = _finish(worker, client, kb_tenant, kb["id"])
    assert row["status"] == "completed", row
    assert seen == [1]
    assert store.get_document(kb_tenant, kb["id"], "orchard.md") is None  # deleted before the run finished


def test_a_tester_run_finishing_after_a_document_delete_stores_no_deleted_text(worker, client, kb_tenant, monkeypatch):  # noqa: F811
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "orchard.md", SECTIONS)
    store = KnowledgeStore(worker.engine)
    _delete_after_the_search(monkeypatch, lambda: store.delete_document(kb_tenant, kb["id"], "orchard.md"))
    run_id, row = _finish(worker, client, kb_tenant, kb["id"])
    assert row["status"] == "completed", row
    assert SECRET not in _stored_text(worker, kb_tenant, run_id)


def test_a_tester_run_finishing_after_a_knowledge_base_delete_stores_no_deleted_text(worker, client, kb_tenant, monkeypatch):  # noqa: F811
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "orchard.md", SECTIONS)
    store = KnowledgeStore(worker.engine)
    _delete_after_the_search(monkeypatch, lambda: store.delete_knowledge_base(kb_tenant, kb["id"]))
    run_id, row = _finish(worker, client, kb_tenant, kb["id"])
    # The knowledge base is gone: the run fails cleanly, with no content.
    assert row["status"] == "failed" and row["output"] is None, row
    events = {k: (t, d) for _, k, t, d in event_rows(worker.engine, run_id)}
    assert events["end"][0] == "run_failed" and events["end"][1]["error_code"] == "knowledge_base_deleted"
    assert events["tool:tool_run:result"][1]["status"] == "error"
    assert SECRET not in _stored_text(worker, kb_tenant, run_id)


KEPT = "quokkaberry"


def test_a_hit_list_mixing_deleted_and_kept_documents_keeps_only_the_kept_ones(worker, client, kb_tenant, monkeypatch):  # noqa: F811
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "orchard.md", SECTIONS)
    ingest(worker, kb_tenant, kb, "depot.md",
           [("Depot", f"Where are the crates stored? In the {KEPT} depot, building two, the crates are stored.")])
    store = KnowledgeStore(worker.engine)
    kept_id = store.get_document(kb_tenant, kb["id"], "depot.md")["id"]
    seen = _delete_after_the_search(monkeypatch, lambda: store.delete_document(kb_tenant, kb["id"], "orchard.md"))
    run_id, row = _finish(worker, client, kb_tenant, kb["id"])
    assert row["status"] == "completed", row
    assert seen == [2]  # the search returned both documents' sections
    stored = _stored_text(worker, kb_tenant, run_id)
    assert SECRET not in stored and KEPT in stored
    result = json.loads(row["output"])
    assert [h["document_id"] for h in result["hits"]] == [kept_id] and result["dropped"] == 1
    assert SECRET not in result["text"] and KEPT in result["text"]
    events = {k: (t, d) for _, k, t, d in event_rows(worker.engine, run_id)}
    assert [h["document_id"] for h in events["tool:tool_run:result"][1]["content"]["hits"]] == [kept_id]
    assert events["end"][0] == "run_completed" and events["end"][1]["result"]["dropped"] == 1


def test_a_replayed_guarded_finish_keeps_the_first_status(worker, client, kb_tenant):  # noqa: F811
    """MINOR-3 (review): eap.finish_run committed `completed`, the checkpoint was lost, the knowledge
    base was deleted, and the step re-executes: the run stays `completed` (its end event says so)."""
    from eap_worker.search_guard import finish_guarded, search_guard_for

    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "orchard.md", SECTIONS)
    run_id, row = _finish(worker, client, kb_tenant, kb["id"])
    assert row["status"] == "completed"
    content = json.loads(row["output"])
    guard = search_guard_for(content, kb["id"])
    KnowledgeStore(worker.engine).delete_knowledge_base(kb_tenant, kb["id"])
    assert finish_guarded(worker.engine, kb_tenant, str(run_id), row["output"], guard) == "completed"
    after = worker.run_store.get_run(kb_tenant, run_id)
    assert after["status"] == "completed" and after["error"] is None
    assert SECRET not in _stored_text(worker, kb_tenant, run_id)  # the delete's scrub stands
