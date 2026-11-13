"""Checkpoint review fixes on the run-store side (RAG and memory milestone, after wave 4):

- mark_running refuses a run detached by delete_conversation (it can never write output again);
- memory_updated `changed_keys` names schema properties only (other keys: a count);
- validate_resource_id refuses dot-only ids;
- KnowledgeStore.list_documents keyset pagination (limit, after);
- MemoryConfig.effective_redact merges the kinds of every input pii_redact processor."""

from __future__ import annotations

import uuid
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import (
    ENQUEUE_FAILED_ERROR,
    KnowledgeStore,
    MemoryEvent,
    MemoryStore,
    validate_resource_id,
)
from eap_schemas.memory import MemoryConfig
from eap_schemas.processors import PiiRedactConfig

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}}


@pytest.fixture
def cleanup(engine, test_tenant):
    yield
    with engine.begin() as conn:
        for table in ("execution_events", "side_effects", "working_memories", "runs", "conversations", "knowledge_bases"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": test_tenant})


# --------------------------------------------------------------------------- mark_running


def test_a_run_detached_by_a_conversation_delete_never_starts(run_store, engine, test_tenant, cleanup):
    conv, _ = run_store.create_conversation(test_tenant, uuid4(), "c")
    run_id, _ = run_store.create_turn(test_tenant, conv["id"], 1, "hello", uuid4())
    # The hand-off raised although DBOS accepted it: failed (terminal), so the delete is allowed.
    assert run_store.mark_enqueue_failed(test_tenant, run_id)
    assert MemoryStore(engine).delete_conversation(test_tenant, conv["id"]) is True
    detached = run_store.get_run(test_tenant, run_id)
    assert (detached["conversation_id"], detached["turn"], detached["input"]) == (None, None, None)
    # The workflow now starts: refused, the row stays as the delete left it.
    assert run_store.mark_running(test_tenant, run_id) is False
    after = run_store.get_run(test_tenant, run_id)
    assert (after["status"], after["error"], after["output"]) == ("failed", None, None)
    assert after["started_at"] is None


def test_runs_without_a_conversation_still_start(run_store, test_tenant, cleanup):
    run_id, _ = run_store.create_run(test_tenant, uuid4(), 1, "x", uuid4())
    assert run_store.mark_enqueue_failed(test_tenant, run_id)
    assert run_store.mark_running(test_tenant, run_id) is True
    assert (run_store.get_run(test_tenant, run_id)["status"], run_store.get_run(test_tenant, run_id)["error"]) == (
        "running", None,
    )
    tool_run, _ = run_store.create_tool_run(test_tenant, "clock.now", "{}", uuid4())
    assert run_store.mark_running(test_tenant, tool_run) is True
    # Still a live turn: unaffected.
    conv, _ = run_store.create_conversation(test_tenant, uuid4(), "c")
    turn, _ = run_store.create_turn(test_tenant, conv["id"], 1, "a", uuid4())
    assert run_store.mark_enqueue_failed(test_tenant, turn)
    assert run_store.get_run(test_tenant, turn)["error"] == ENQUEUE_FAILED_ERROR
    assert run_store.mark_running(test_tenant, turn) is True


# --------------------------------------------------------------------------- changed_keys


def test_changed_keys_name_schema_properties_only(run_store, engine, test_tenant, cleanup):
    agent = str(uuid4())
    memory = MemoryStore(engine)
    run_id, _ = run_store.create_run(test_tenant, uuid.UUID(agent), 1, "x", uuid4())
    write = memory.update_working_memory(
        test_tenant, agent, scope="resource", scope_key="user-1",
        patch={"name": "Ana", "inventedsecretkey": 1, "anotherone": None}, json_schema=SCHEMA,
        ledger_key=f"wm:{run_id}:t1", run_id=str(run_id),
        event=MemoryEvent(run_id=str(run_id), event_key="memory:wm:t1", data={"tool_call_id": "t1"}),
    )
    assert write.changed_keys == ("name",) and write.other_changed_keys == 2
    with engine.connect() as conn:
        data = conn.execute(
            text("SELECT data FROM execution_events WHERE run_id = :r AND event_key = 'memory:wm:t1'"), {"r": str(run_id)}
        ).scalar_one()
    assert data["changed_keys"] == ["name"] and data["other_changed_keys"] == 2
    assert "inventedsecretkey" not in str(data)
    # The ledger answers a replay with the same (key-free) result.
    replay = memory.ledger_result(test_tenant, agent, f"wm:{run_id}:t1", scope="resource", scope_key="user-1")
    assert replay.replayed and replay.changed_keys == ("name",) and replay.other_changed_keys == 2
    # A replace (Studio PUT) diff: schema keys listed, others counted; no other_changed_keys when 0.
    put = memory.update_working_memory(
        test_tenant, agent, scope="resource", scope_key="user-1", content={"name": "Bea", "age": 3}, json_schema=SCHEMA,
    )
    assert put.changed_keys == ("age", "name") and put.other_changed_keys == 1  # inventedsecretkey removed
    assert "other_changed_keys" not in memory.update_working_memory(
        test_tenant, agent, scope="resource", scope_key="user-1", content={"name": "Cy", "age": 3}, json_schema=SCHEMA,
    ).event_data()


# --------------------------------------------------------------------------- resource ids


@pytest.mark.parametrize("bad", [".", "..", "...", "." * 128])
def test_dot_only_resource_ids_are_refused(run_store, test_tenant, cleanup, bad):
    with pytest.raises(ValueError, match="dots only"):
        validate_resource_id(bad)
    with pytest.raises(ValueError):
        run_store.create_conversation(test_tenant, uuid4(), "c", resource_id=bad)


@pytest.mark.parametrize("good", ["a.b", ".a", "a..", "user-1", "x@y.z", "_"])
def test_ids_with_dots_and_other_characters_still_pass(good):
    assert validate_resource_id(good) == good


# --------------------------------------------------------------------------- list_documents


def test_list_documents_keyset_pages(engine, test_tenant, cleanup):
    store = KnowledgeStore(engine)
    kb = store.create_knowledge_base(test_tenant, name="Docs", embedding_model="test:hash", dimensions=1536)
    keys = ["b", "a", "c-1", "c", "d.e"]
    for key in keys:
        store.put_document(
            test_tenant, kb["id"], document_key=key, title=key, content=f"# {key}\n\nbody", mime_type="text/markdown",
        )
    everything = [d["document_key"] for d in store.list_documents(test_tenant, kb["id"])]
    assert everything == sorted(everything) and set(everything) == set(keys)
    pages, after = [], None
    while True:
        page = store.list_documents(test_tenant, kb["id"], limit=2, after=after)
        pages.append([d["document_key"] for d in page])
        if len(page) < 2:
            break
        after = page[-1]["document_key"]
    assert [k for p in pages for k in p] == everything  # nothing skipped or repeated
    assert all("content" not in d for d in store.list_documents(test_tenant, kb["id"], limit=1))
    assert store.list_documents(test_tenant, kb["id"], after=everything[-1]) == []
    assert store.list_documents("someone-else", kb["id"], limit=10) == []
    assert store.list_documents(test_tenant, "not-a-uuid", limit=10) == []
    with pytest.raises(ValueError):
        store.list_documents(test_tenant, kb["id"], limit=0)


# --------------------------------------------------------------------------- effective_redact


def test_effective_redact_merges_every_input_pii_redact_processor():
    from eap_schemas import AgentSpec

    spec = AgentSpec.model_validate({
        "spec_version": 1, "name": "a", "instructions": "i", "model": {"provider": "test", "name": "x"},
        "tools": [], "model_settings": {},
        "input_processors": [
            {"type": "pii_redact", "config": {"kinds": ["email"]}},
            {"type": "pii_redact", "config": {"kinds": ["phone", "email"], "action": "block"}},
        ],
        "memory": {"working": {}},
    })
    merged = spec.memory.effective_redact(tuple(spec.input_processors))
    assert merged == PiiRedactConfig(kinds=("email", "phone"), action="redact")
    explicit = MemoryConfig(redact=PiiRedactConfig(kinds=("email",)))
    assert explicit.effective_redact(tuple(spec.input_processors)).kinds == ("email",)
    assert MemoryConfig().effective_redact(()) is None
