"""P1.C4: knowledge base and document deletes scrub the knowledge base's search-tester runs
(`knowledge.search` tool runs, `runs.knowledge_base_id`), like DELETE /conversations/{id} scrubs a turn."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from eap_run_store import EventLog, KnowledgeStore, RunStore
from eap_schemas.tools import KNOWLEDGE_SEARCH_TOOL_ID

CHUNK_TEXT = "the secret chunk text about penguins"
QUERY = "where do penguins live"


@pytest.fixture
def tenant():
    return f"t_scrub_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def store(engine):
    return KnowledgeStore(engine)


def _kb(store: KnowledgeStore, tenant: str, name: str = "Docs") -> dict:
    return store.create_knowledge_base(tenant, name=name, embedding_model="test:hash", dimensions=1024)


def _doc(store: KnowledgeStore, tenant: str, kb_id: str, key: str) -> dict:
    return store.put_document(tenant, kb_id, document_key=key, title=key, content="# H\n\nbody", mime_type="text/markdown").document


def _tester_run(engine, tenant: str, kb_id: str, document_ids: list[str]) -> str:
    """A finished knowledge.search tool run whose hits name `document_ids`, with a usage row."""
    runs, log = RunStore(engine), EventLog(engine)
    run_id = uuid.uuid4()
    args = {"knowledge_base_id": kb_id, "query": QUERY}
    runs.create_tool_run(tenant, KNOWLEDGE_SEARCH_TOOL_ID, json.dumps(args), run_id, knowledge_base_id=kb_id)
    hits = [{"n": i + 1, "document_id": d, "chunk_id": str(uuid.uuid4()), "score": 0.9, "content": CHUNK_TEXT}
            for i, d in enumerate(document_ids)]
    content = {"knowledge_base_id": kb_id, "hits": hits, "text": f"[1] {CHUNK_TEXT}"}
    usage = {"model_id": "test:hash", "input_tokens": 7, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "usage_measured": True, "request_key": "embed:retrieve:tool_run",
             "operation": "embedding"}
    log.append(tenant, run_id, "start", "run_started", {"tool_id": KNOWLEDGE_SEARCH_TOOL_ID, "input": json.dumps(args)})
    log.append(tenant, run_id, "tool:call", "tool_call", {"tool_name": KNOWLEDGE_SEARCH_TOOL_ID, "args": args})
    log.append(tenant, run_id, "embed:retrieve:tool_run", "embedding", {"model_id": "test:hash", "inputs": 1}, usage=usage)
    log.append(tenant, run_id, "tool:result", "tool_result", {"tool_name": KNOWLEDGE_SEARCH_TOOL_ID, "status": "ok",
                                                              "content": content})
    output = json.dumps(content)
    log.append(tenant, run_id, "end", "run_completed", {"output": output, "result": content})
    runs.set_status(tenant, run_id, "completed", output, None)
    with engine.begin() as conn:  # a tool result's ledger row (the tool run's `{run_id}:tool` key)
        conn.execute(text("INSERT INTO side_effects (tenant_id, key, state, result, created_at) VALUES (:t, :k, 'completed', CAST(:r AS jsonb), now())"),
                     {"t": tenant, "k": f"{run_id}:tool", "r": json.dumps(content)})
    return str(run_id)


def _state(engine, tenant: str, run_id: str) -> dict:
    with engine.connect() as conn:
        run = conn.execute(text("SELECT input, output FROM runs WHERE tenant_id = :t AND id = :r"),
                           {"t": tenant, "r": run_id}).mappings().one()
        events = [r.data for r in conn.execute(
            text("SELECT data FROM execution_events WHERE tenant_id = :t AND run_id = :r ORDER BY seq"),
            {"t": tenant, "r": run_id})]
        usage = conn.execute(text("SELECT count(*) FROM run_model_requests WHERE tenant_id = :t AND run_id = :r"),
                             {"t": tenant, "r": run_id}).scalar()
        ledger = conn.execute(text("SELECT count(*) FROM side_effects WHERE tenant_id = :t AND key LIKE :k"),
                              {"t": tenant, "k": f"{run_id}:%"}).scalar()
    return {"input": run["input"], "output": run["output"], "events": events, "usage": usage, "ledger": ledger}


def _has_text(state: dict) -> bool:
    blob = json.dumps(state, default=str)
    return CHUNK_TEXT in blob or "penguins" in blob


def test_knowledge_base_delete_scrubs_its_tester_runs_only(engine, store, tenant):
    kb, other = _kb(store, tenant), _kb(store, tenant, "Other")
    doc = _doc(store, tenant, kb["id"], "a.md")
    run = _tester_run(engine, tenant, kb["id"], [doc["id"]])
    keep = _tester_run(engine, tenant, other["id"], [str(uuid.uuid4())])
    other_tenant = f"{tenant}_x"
    kb_x = _kb(store, other_tenant)
    keep_x = _tester_run(engine, other_tenant, kb_x["id"], [str(uuid.uuid4())])
    assert _has_text(_state(engine, tenant, run))

    assert store.delete_knowledge_base(tenant, kb["id"]) is True
    after = _state(engine, tenant, run)
    assert after["input"] is None and after["output"] is None
    assert not _has_text(after), after
    assert all(e.get("redacted") is True for e in after["events"])
    assert after["usage"] == 1  # the cost record stays
    assert after["ledger"] == 0  # the tool result's side_effects row is gone
    assert _has_text(_state(engine, tenant, keep))  # another knowledge base's runs are untouched
    assert _has_text(_state(engine, other_tenant, keep_x))  # another tenant's too


def test_document_delete_scrubs_only_runs_whose_hits_name_it(engine, store, tenant):
    kb = _kb(store, tenant)
    a, b = _doc(store, tenant, kb["id"], "a.md"), _doc(store, tenant, kb["id"], "b.md")
    names_a = _tester_run(engine, tenant, kb["id"], [b["id"], a["id"]])
    names_b = _tester_run(engine, tenant, kb["id"], [b["id"]])

    assert store.delete_document(tenant, kb["id"], "a.md") is True
    scrubbed = _state(engine, tenant, names_a)
    assert scrubbed["input"] is None and scrubbed["output"] is None and not _has_text(scrubbed)
    assert scrubbed["usage"] == 1
    assert _has_text(_state(engine, tenant, names_b))
    assert store.delete_document(tenant, kb["id"], "a.md") is False  # idempotent, scrubs nothing more


def test_a_failed_delete_rolls_back_the_scrub(engine, store, tenant, monkeypatch):
    import eap_run_store.memory as memory

    kb = _kb(store, tenant)
    doc = _doc(store, tenant, kb["id"], "a.md")
    run = _tester_run(engine, tenant, kb["id"], [doc["id"]])

    def boom(*args, **kwargs):
        raise RuntimeError("forced")

    monkeypatch.setattr(memory, "_scrub_runs", boom)
    with pytest.raises(RuntimeError):
        store.delete_knowledge_base(tenant, kb["id"])
    assert store.get_knowledge_base(tenant, kb["id"]) is not None  # the delete rolled back too
    assert _has_text(_state(engine, tenant, run))
