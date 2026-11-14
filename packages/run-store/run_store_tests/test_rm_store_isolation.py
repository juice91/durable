"""RM.20: tenant isolation and deletion completeness of the RAG and memory stores.

Two tenants hold data of the identical shape: the same agent id, knowledge base name, document key,
resource id and (where ids are generated) the same kind of rows: a knowledge base with an active,
embedded document; a conversation of resource `u1` with two completed turns; a resource-scoped and a
conversation-scoped working memory note (with a ledger row); an observation; the turn's message
embeddings. Each row's text carries its tenant's marker.

1. Tenant B calls every KnowledgeStore / MemoryStore read, write and delete method with tenant A's
   ids. Each call must answer None / empty / False / zero (or raise the store's not-found error),
   return no A text, and change nothing: every RM table (and the run tables the stores write) is
   snapshotted for both tenants before and after.
2. Each deletion method, run by A on A's data, leaves zero rows for its target (one query per table)
   while B's identical rows are untouched.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from eap_db.testing import SCHEMA_ENV
from eap_run_store import (
    ChunkInput,
    ConversationNotFound,
    DocumentNotFound,
    KnowledgeStore,
    MemoryEvent,
    MemoryStore,
    RunNotFound,
    RunStore,
    TurnText,
    working_memory_ledger_key,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

DIMS = 1536
MODEL = "test:hash"
AGENT = str(uuid.uuid4())  # the same agent id in both tenants
RESOURCE = "u1"
SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}}

# Every table an RM store method reads or writes, per tenant, in a stable order (vectors as text).
SNAPSHOT = {
    "knowledge_bases": "SELECT to_jsonb(x) FROM knowledge_bases x WHERE tenant_id = :t ORDER BY id",
    "kb_documents": "SELECT to_jsonb(x) FROM kb_documents x WHERE tenant_id = :t ORDER BY id",
    "kb_chunks": (
        "SELECT to_jsonb(x) - 'embedding' || jsonb_build_object('e', x.embedding::text)"
        " FROM kb_chunks x WHERE tenant_id = :t ORDER BY id"
    ),
    "working_memories": (
        "SELECT to_jsonb(x) FROM working_memories x WHERE tenant_id = :t ORDER BY agent_id, scope, scope_key"
    ),
    "memory_observations": "SELECT to_jsonb(x) FROM memory_observations x WHERE tenant_id = :t ORDER BY id",
    "message_embeddings": (
        "SELECT to_jsonb(x) - 'embedding' || jsonb_build_object('e', x.embedding::text)"
        " FROM message_embeddings x WHERE tenant_id = :t ORDER BY id"
    ),
    "memory_forget_watermarks": (
        "SELECT to_jsonb(x) FROM memory_forget_watermarks x WHERE tenant_id = :t ORDER BY conversation_id"
    ),
    "conversations": "SELECT to_jsonb(x) FROM conversations x WHERE tenant_id = :t ORDER BY id",
    "runs": "SELECT to_jsonb(x) FROM runs x WHERE tenant_id = :t ORDER BY id",
    "execution_events": "SELECT to_jsonb(x) FROM execution_events x WHERE tenant_id = :t ORDER BY run_id, seq",
    "side_effects": "SELECT to_jsonb(x) FROM side_effects x WHERE tenant_id = :t ORDER BY key",
    "run_model_requests": (
        "SELECT to_jsonb(x) FROM run_model_requests x WHERE tenant_id = :t ORDER BY run_id, request_key"
    ),
}

NOT_FOUND = (ConversationNotFound, DocumentNotFound, RunNotFound, LookupError)


def vec(i: int) -> list[float]:
    v = [0.0] * DIMS
    v[i % DIMS] = 1.0
    return v


@dataclass
class World:
    tenant: str
    marker: str
    kb_id: str = ""
    doc_id: str = ""
    doc_version: int = 0
    generation: int = 0
    conv_id: str = ""
    run1: str = ""
    run2: str = ""
    observation_id: str = ""


def _turn(engine, tenant: str, conv: str, n: int, marker: str) -> str:
    run_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, output, messages,"
                " conversation_id, turn, created_at, updated_at)"
                " VALUES (:id, :t, 'agent', :a, 1, 'completed', :i, :o, CAST(:m AS jsonb), :c, :n, now(), now())"
            ),
            {"id": run_id, "t": tenant, "a": AGENT, "i": f"{marker} input {n}", "o": f"{marker} answer {n}",
             "m": json.dumps([{"kind": "request", "text": f"{marker} turn {n}"}]), "c": conv, "n": n},
        )
    return run_id


def seed(engine, tenant: str) -> World:
    w = World(tenant=tenant, marker=f"MARK{tenant[-6:]}")
    ks, ms, rs = KnowledgeStore(engine), MemoryStore(engine), RunStore(engine)
    kb = ks.create_knowledge_base(tenant, name="Docs", embedding_model=MODEL, dimensions=DIMS)
    w.kb_id, w.generation = kb["id"], kb["active_generation"]
    put = ks.put_document(tenant, w.kb_id, document_key="d1", title="Doc", content=f"{w.marker} body",
                          mime_type="text/markdown", metadata={"team": "x"})
    w.doc_id, w.doc_version = put.document["id"], put.document["latest_version"]
    chunk = ChunkInput(ordinal=0, title="T", text=f"{w.marker} chunk", embed_text=f"{w.marker} embed",
                       embed_text_hash=uuid.uuid4().hex, approx_tokens=5)
    ks.upsert_chunks(tenant, w.doc_id, w.doc_version, w.generation, [chunk], metadata={"team": "x"})
    [pending] = ks.next_unembedded_batch(tenant, w.kb_id, w.generation, document_id=w.doc_id, doc_version=w.doc_version)
    ks.write_embeddings(tenant, w.kb_id, w.generation, {pending.id: vec(1)}, embedding_model=MODEL)
    assert ks.finalize_document(tenant, w.doc_id, w.doc_version, w.generation, embedding_model=MODEL).status == "switched"

    conv, _ = rs.create_conversation(tenant, AGENT, title="c", resource_id=RESOURCE)
    w.conv_id = conv["id"]
    w.run1 = _turn(engine, tenant, w.conv_id, 1, w.marker)
    w.run2 = _turn(engine, tenant, w.conv_id, 2, w.marker)
    ms.update_working_memory(tenant, AGENT, scope="resource", scope_key=RESOURCE, patch={"name": w.marker},
                             json_schema=SCHEMA, ledger_key=working_memory_ledger_key(w.run1, "call_1"), run_id=w.run1,
                             event=MemoryEvent(run_id=w.run1, event_key="memory:wm:call_1"))
    ms.update_working_memory(tenant, AGENT, scope="conversation", scope_key=w.conv_id, content=f"{w.marker} note")
    obs = ms.insert_observation(tenant, AGENT, w.conv_id, covers_from_turn=1, covers_to_turn=1,
                                content=f"- {w.marker} observed", run_id=w.run2)
    w.observation_id = obs.observation.id if hasattr(obs, "observation") and obs.observation else ""
    ms.index_turn(tenant, AGENT, w.conv_id, w.run1,
                  [TurnText("user", f"{w.marker} input 1", vec(2)), TurnText("assistant", f"{w.marker} answer 1", vec(3))],
                  embedding_model=MODEL)
    if not w.observation_id:
        w.observation_id = ms.list_observations(tenant, w.conv_id)[0].id
    return w


def cleanup(engine, tenant: str) -> None:
    with engine.begin() as conn:
        for sql in (
            "DELETE FROM side_effects WHERE tenant_id = :t",
            "DELETE FROM working_memories WHERE tenant_id = :t",
            "DELETE FROM memory_observations WHERE tenant_id = :t",
            "DELETE FROM message_embeddings WHERE tenant_id = :t",
            "DELETE FROM memory_forget_watermarks WHERE tenant_id = :t",
            "DELETE FROM run_model_requests WHERE tenant_id = :t",
            "DELETE FROM execution_events WHERE tenant_id = :t",
            "DELETE FROM runs WHERE tenant_id = :t",
            "DELETE FROM conversations WHERE tenant_id = :t",
            "DELETE FROM knowledge_bases WHERE tenant_id = :t",
        ):
            conn.execute(text(sql), {"t": tenant})


@pytest.fixture
def worlds(engine):
    a_t, b_t = f"iso_a_{uuid.uuid4().hex[:8]}", f"iso_b_{uuid.uuid4().hex[:8]}"
    try:
        yield seed(engine, a_t), seed(engine, b_t)
    finally:
        cleanup(engine, a_t)
        cleanup(engine, b_t)


def snapshot(engine, *tenants: str) -> dict[str, dict[str, list[Any]]]:
    with engine.connect() as conn:
        return {t: {table: [r[0] for r in conn.execute(text(sql), {"t": t})] for table, sql in SNAPSHOT.items()}
                for t in tenants}


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def _empty(value: Any) -> bool:
    """None / False / 0 / an empty collection, or a result dataclass whose counts are all zero/empty."""
    value = _plain(value)
    if value in (None, False, 0) or (isinstance(value, (list, tuple, set, dict, str)) and not value):
        return True
    if isinstance(value, dict):
        return all(_empty(v) for k, v in value.items() if k not in ("status",)) and value.get("status") in (
            None, "not_found", "missing", "gone", "deleted", "superseded", "stale", "not_active",
        )
    return False


# --------------------------------------------------------------------------- 1. cross-tenant calls


def _cross_tenant_calls(a: World, b: World, ks: KnowledgeStore, ms: MemoryStore) -> dict[str, Callable[[], Any]]:
    B = b.tenant
    return {
        # KnowledgeStore reads
        "get_knowledge_base": lambda: ks.get_knowledge_base(B, a.kb_id),
        "get_knowledge_bases": lambda: ks.get_knowledge_bases(B, [a.kb_id]),
        "existing_knowledge_base_ids": lambda: ks.existing_knowledge_base_ids(B, [a.kb_id]),
        "list_documents": lambda: ks.list_documents(B, a.kb_id),
        "get_document": lambda: ks.get_document(B, a.kb_id, "d1", include_content=True),
        "get_document_by_id": lambda: ks.get_document_by_id(B, a.doc_id, include_content=True),
        "get_ingest_source": lambda: ks.get_ingest_source(B, a.doc_id, a.doc_version),
        "next_unembedded_batch": lambda: ks.next_unembedded_batch(B, a.kb_id, a.generation),
        "search": lambda: ks.search(B, [a.kb_id], vec(1), embedding_model=MODEL, top_k=10),
        "search_with_filter": lambda: ks.search(B, [a.kb_id], vec(1), embedding_model=MODEL, top_k=10,
                                                metadata_filter={"team": "x"}),
        # Neither hits nor `moved` (A's knowledge base under another model) for B.
        "search_snapshot": lambda: ks.search_snapshot(B, [a.kb_id], vec(1), embedding_model="test:other", top_k=10),
        # KnowledgeStore writes / deletes
        "update_knowledge_base": lambda: ks.update_knowledge_base(B, a.kb_id, name="pwned"),
        "delete_knowledge_base": lambda: ks.delete_knowledge_base(B, a.kb_id),
        "delete_document": lambda: ks.delete_document(B, a.kb_id, "d1"),
        "request_reingest": lambda: ks.request_reingest(B, a.kb_id, "d1"),
        "mark_document_ingesting": lambda: ks.mark_document_ingesting(B, a.doc_id, a.doc_version, str(uuid.uuid4())),
        "mark_document_failed": lambda: ks.mark_document_failed(B, a.doc_id, a.doc_version, "x"),
        "upsert_chunks": lambda: ks.upsert_chunks(
            B, a.doc_id, a.doc_version, a.generation,
            [ChunkInput(ordinal=5, title="x", text="x", embed_text="x", embed_text_hash="h", approx_tokens=1)],
            metadata={}),
        "apply_cached_embeddings": lambda: ks.apply_cached_embeddings(B, a.kb_id, a.generation),
        "write_embeddings": lambda: ks.write_embeddings(B, a.kb_id, a.generation, {}, embedding_model=MODEL),
        "finalize_document": lambda: ks.finalize_document(B, a.doc_id, a.doc_version, a.generation,
                                                          embedding_model=MODEL),
        "start_reindex": lambda: ks.start_reindex(B, a.kb_id, embedding_model="test:hash768", dimensions=768),
        "copy_generation_chunks": lambda: ks.copy_generation_chunks(B, a.kb_id, a.generation + 1),
        "mark_reindex_failed": lambda: ks.mark_reindex_failed(B, a.kb_id, a.generation + 1, "x"),
        "claim_reindex_retry": lambda: ks.claim_reindex_retry(B, a.kb_id, a.generation + 1),
        "cancel_reindex": lambda: ks.cancel_reindex(B, a.kb_id),
        # MemoryStore reads
        "get_working_memory_conversation": lambda: ms.get_working_memory(B, AGENT, "conversation", a.conv_id),
        "list_observations": lambda: ms.list_observations(B, a.conv_id, include_superseded=True, include_markers=True),
        "load_snapshot": lambda: ms.load_snapshot(B, AGENT, a.conv_id, observations=True),
        "search_recall": lambda: ms.search_recall(B, AGENT, a.conv_id, vec(2), embedding_model=MODEL),
        "recall_rows_exist": lambda: ms.recall_rows_exist(B, AGENT, a.conv_id, embedding_model=MODEL),
        "recall_neighbours": lambda: ms.recall_neighbours(B, AGENT, a.conv_id, 1, 1, current_conversation_id=b.conv_id),
        "recall_neighbours_many": lambda: ms.recall_neighbours_many(B, AGENT, [(a.conv_id, 1)], 1,
                                                                    current_conversation_id=b.conv_id),
        "ledger_result": lambda: ms.ledger_result(B, AGENT, working_memory_ledger_key(a.run1, "call_1"),
                                                  scope="resource", scope_key=RESOURCE),
        # MemoryStore writes / deletes
        "update_working_memory_conversation": lambda: ms.update_working_memory(
            B, AGENT, scope="conversation", scope_key=a.conv_id, content="pwned"),
        "insert_observation": lambda: ms.insert_observation(B, AGENT, a.conv_id, covers_from_turn=2,
                                                            covers_to_turn=2, content="pwned"),
        "reflect": lambda: ms.reflect(B, AGENT, a.conv_id, supersede_ids=[a.observation_id], content="pwned"),
        "index_turn": lambda: ms.index_turn(B, AGENT, a.conv_id, a.run2, [TurnText("user", "pwned", vec(4))],
                                            embedding_model=MODEL),
        "append_conversation_event": lambda: ms.append_conversation_event(
            B, AGENT, a.conv_id, MemoryEvent(run_id=a.run2, event_key="memory:pwned")),
        "delete_observation": lambda: ms.delete_observation(B, a.conv_id, a.observation_id),
        "delete_observations": lambda: ms.delete_observations(B, a.conv_id),
        "delete_working_memory_conversation": lambda: ms.delete_working_memory(B, AGENT, "conversation", a.conv_id),
        "delete_conversation": lambda: ms.delete_conversation(B, a.conv_id),
        # KnowledgeStore: the ingest and reindex internals, the API's upload, the estimate, the plan
        "put_document": lambda: ks.put_document(B, a.kb_id, document_key="d1", title="pwned", content="pwned",
                                                mime_type="text/plain", run_id=str(uuid.uuid4())),
        "attach_ingest_run": lambda: ks.attach_ingest_run(B, a.doc_id, str(uuid.uuid4()), doc_version=a.doc_version),
        "embedding_progress": lambda: ks.embedding_progress(B, a.kb_id, a.generation, document_id=a.doc_id,
                                                            doc_version=a.doc_version),
        "fill_cached_embeddings": lambda: ks.fill_cached_embeddings(B, a.kb_id, a.generation, _chunk_ids(ks, a)),
        "finalize_reindex": lambda: ks.finalize_reindex(B, a.kb_id, a.generation + 1, embedding_model="test:hash768"),
        "estimate_reindex": lambda: ks.estimate_reindex(B, a.kb_id),
        "explain_search": lambda: ks.explain_search(B, [a.kb_id], vec(1), embedding_model=MODEL, top_k=10),
        # MemoryStore: lists, usage, the recall plan
        "list_working_memories": lambda: ms.list_working_memories(B, AGENT),
        "record_usage": lambda: ms.record_usage(B, a.run1, {
            "request_key": "embed:pwned", "model_id": MODEL, "provider_model_name": "hash", "operation": "embedding",
            "input_tokens": 1, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "usage_measured": True, "cost_usd": None, "price_source": None}),
        "explain_search_recall": lambda: ms.explain_search_recall(B, AGENT, a.conv_id, vec(2), embedding_model=MODEL),
        # Last: it deletes B's own resource memory (B owns a resource of the same name).
        "forget_resource": lambda: ms.forget_resource(B, RESOURCE, include_conversations=True),
    }


def _chunk_ids(ks: KnowledgeStore, a: World) -> list[str]:
    """Tenant A's real chunk ids (B passes them as if they were its own)."""
    with ks.engine.connect() as conn:
        return [str(r[0]) for r in conn.execute(text("SELECT id FROM kb_chunks WHERE tenant_id = :t"), {"t": a.tenant})]


# Public store methods not called with the other tenant's ids above, each with the reason.
EXEMPT_FROM_CROSS_TENANT = {
    "create_knowledge_base": "takes no foreign id; it creates in the caller's tenant (API test_rm_cross_tenant: "
                             "the other tenant's name touches only the caller's tenant)",
    "list_knowledge_bases": "takes only the caller's tenant (test_b_reads_of_its_own_ids_return_only_b_data)",
    "retention_tenants": "the operator-only retention sweep enumerates tenants by design (no tenant argument)",
    "message_embedding_tenants_before": "the operator-only retention sweep enumerates tenants by design",
    "delete_retention_batch": "tenant and cutoff only, no foreign id (test_retention_batches_are_per_tenant)",
    "delete_message_embeddings_before": "tenant and cutoff only, no foreign id (test_retention_batches_are_per_tenant)",
}


# Writes that B may legitimately perform in B's OWN namespace with A's id as a key string: they must
# not touch A, but may create a row of B's (asserted separately below).
WRITES_INTO_B = {"update_working_memory_conversation", "forget_resource"}
# Results that are B's own data or a query plan by design: they must not contain A's marker.
RESULT_IS_BS_OWN = {"list_working_memories", "explain_search", "explain_search_recall"}


def test_the_cross_tenant_calls_cover_every_public_store_method(engine, worlds):
    """A new store method must be added to _cross_tenant_calls or exempted with a reason."""
    import inspect
    import re

    a, b = worlds
    source = inspect.getsource(_cross_tenant_calls)
    called = set(re.findall(r"\b(?:ks|ms)\.([a-z_]+)\(", source))
    public = {
        name for cls in (KnowledgeStore, MemoryStore)
        for name, member in inspect.getmembers(cls, callable) if not name.startswith("_")
    }
    assert called <= public, called - public
    assert set(EXEMPT_FROM_CROSS_TENANT) <= public, set(EXEMPT_FROM_CROSS_TENANT) - public
    assert called.isdisjoint(EXEMPT_FROM_CROSS_TENANT), called & set(EXEMPT_FROM_CROSS_TENANT)
    assert public - set(EXEMPT_FROM_CROSS_TENANT) == called, sorted(public - set(EXEMPT_FROM_CROSS_TENANT) - called)


def test_every_store_method_called_by_tenant_b_with_tenant_a_ids_changes_and_returns_nothing(engine, worlds):
    a, b = worlds
    ks, ms = KnowledgeStore(engine), MemoryStore(engine)
    calls = _cross_tenant_calls(a, b, ks, ms)
    leaked: list[str] = []
    for name, call in calls.items():
        before = snapshot(engine, a.tenant, b.tenant)
        try:
            result = call()
        except NOT_FOUND:
            result = None
        except ValueError as exc:  # a refused argument (e.g. an id that is not the tenant's) never leaks text
            assert a.marker not in str(exc), name
            result = None
        after = snapshot(engine, a.tenant, b.tenant)
        assert after[a.tenant] == before[a.tenant], f"{name}: tenant A's rows changed"
        if name in WRITES_INTO_B:
            assert a.marker not in repr(_plain(result)), name
            continue
        if name in RESULT_IS_BS_OWN:
            assert a.marker not in repr(_plain(result)) and a.kb_id not in repr(_plain(result)), name
            assert after[b.tenant] == before[b.tenant], f"{name}: tenant B's rows changed"
            continue
        assert after[b.tenant] == before[b.tenant], f"{name}: tenant B's rows changed"
        if a.marker in repr(_plain(result)):
            leaked.append(name)
        elif not _empty(result):
            leaked.append(f"{name} -> {_plain(result)!r}"[:300])
    assert leaked == [], leaked
    # The one B write keyed by A's conversation id landed in B's namespace only (and is not A's note).
    note = ms.get_working_memory(b.tenant, AGENT, "conversation", a.conv_id)
    if note is not None:
        assert ms.get_working_memory(a.tenant, AGENT, "conversation", a.conv_id).content == f"{a.marker} note"


def test_b_reads_of_its_own_ids_return_only_b_data(engine, worlds):
    a, b = worlds
    ks, ms = KnowledgeStore(engine), MemoryStore(engine)
    dump = json.dumps([
        _plain(x) for x in (
            ks.list_knowledge_bases(b.tenant),
            ks.list_documents(b.tenant, b.kb_id),
            ks.get_document(b.tenant, b.kb_id, "d1", include_content=True),
            ks.search(b.tenant, [b.kb_id, a.kb_id], vec(1), embedding_model=MODEL, top_k=10),
            ms.get_working_memory(b.tenant, AGENT, "resource", RESOURCE),
            ms.list_working_memories(b.tenant, AGENT),
            ms.list_observations(b.tenant, b.conv_id),
            ms.load_snapshot(b.tenant, AGENT, b.conv_id, observations=True),
            ms.search_recall(b.tenant, AGENT, b.conv_id, vec(2), embedding_model=MODEL),
        )
    ], default=str)
    assert b.marker in dump and a.marker not in dump
    hits = ks.search(b.tenant, [b.kb_id, a.kb_id], vec(1), embedding_model=MODEL, top_k=10)
    assert {h.kb_id for h in hits} == {b.kb_id}


def test_filter_values_are_bound_json_never_sql(engine, worlds):
    """`metadata @>` takes the spec filter as one bound JSON parameter: SQL-looking keys and values
    match nothing (and break nothing); the plain filter still matches."""
    a, b = worlds
    ks = KnowledgeStore(engine)
    before = snapshot(engine, a.tenant, b.tenant)
    for hostile in (
        {"team": "x' OR '1'='1"},
        {"team') OR true --": "x"},
        {"team": "x\"}' ) OR tenant_id <> '"},
        {"team": ["x", "') OR 1=1 --"]},
    ):
        try:
            hits = ks.search(b.tenant, [b.kb_id], vec(1), embedding_model=MODEL, top_k=10, metadata_filter=hostile)
        except ValueError:
            continue
        assert all(h.kb_id == b.kb_id for h in hits)
        assert a.marker not in json.dumps([_plain(h) for h in hits], default=str)
    assert [h.kb_id for h in ks.search(b.tenant, [b.kb_id], vec(1), embedding_model=MODEL, top_k=10,
                                       metadata_filter={"team": "x"})] == [b.kb_id]
    assert snapshot(engine, a.tenant, b.tenant) == before


def test_a_forged_chunk_row_cannot_point_into_another_tenants_knowledge_base(engine, worlds):
    """kb_chunks' composite (tenant_id, …) foreign keys refuse a tenant-B row naming A's document."""
    from sqlalchemy.exc import IntegrityError

    a, b = worlds
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO kb_chunks (tenant_id, kb_id, document_id, doc_version, generation, ordinal,"
                    " title, content, embed_text_hash, approx_tokens, metadata, dimensions, embedding)"
                    " VALUES (:t, :kb, :d, :v, :g, 9, 'x', 'FORGED', 'h', 1, '{}'::jsonb, 1536, CAST(:e AS public.vector))"
                ),
                {"t": b.tenant, "kb": a.kb_id, "d": a.doc_id, "v": a.doc_version, "g": a.generation,
                 "e": str(vec(1))},
            )
    for table, col in (("message_embeddings", "conversation_id"), ("memory_observations", "conversation_id"),
                       ("memory_forget_watermarks", "conversation_id")):
        cols = {
            "message_embeddings": "(tenant_id, agent_id, resource_id, conversation_id, turn, run_id, role, text,"
                                  " embedding_model, dimensions, embedding) VALUES (:t, :a, 'u1', :c, 1, :r, 'user',"
                                  " 'FORGED', 'test:hash', 1536, CAST(:e AS public.vector))",
            "memory_observations": "(tenant_id, agent_id, conversation_id, kind, covers_from_turn, covers_to_turn,"
                                   " content, approx_tokens) VALUES (:t, :a, :c, 'observation', 5, 5, 'FORGED', 1)",
            "memory_forget_watermarks": "(tenant_id, conversation_id, forgotten_through_turn) VALUES (:t, :c, 9)",
        }[table]
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(f"INSERT INTO {table} {cols}"),
                             {"t": b.tenant, "a": AGENT, "c": a.conv_id, "r": a.run1, "e": str(vec(1))})


# --------------------------------------------------------------------------- 2. deletions leave zero rows


def _count(engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def _conversation_rows(engine, w: World) -> dict[str, int]:
    p = {"t": w.tenant, "c": w.conv_id}
    return {
        "conversations": _count(engine, "SELECT count(*) FROM conversations WHERE tenant_id = :t AND id = :c", **p),
        "working_memories(conversation)": _count(
            engine, "SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'conversation'"
            " AND scope_key = :c", **p),
        "memory_observations": _count(
            engine, "SELECT count(*) FROM memory_observations WHERE tenant_id = :t AND conversation_id = :c", **p),
        "message_embeddings": _count(
            engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND conversation_id = :c", **p),
        "runs with text": _count(
            engine, "SELECT count(*) FROM runs WHERE tenant_id = :t AND id IN (:r1, :r2) AND (input IS NOT NULL"
            " OR output IS NOT NULL OR messages IS NOT NULL OR conversation_id IS NOT NULL)",
            t=w.tenant, r1=w.run1, r2=w.run2),
        "events with the marker": _count(
            engine, "SELECT count(*) FROM execution_events WHERE tenant_id = :t AND run_id IN (:r1, :r2)"
            " AND data::text LIKE :m", t=w.tenant, r1=w.run1, r2=w.run2, m=f"%{w.marker}%"),
        "side_effects of the turns": _count(
            engine, "SELECT count(*) FROM side_effects WHERE tenant_id = :t AND (key LIKE :p1 OR key LIKE :p2)",
            t=w.tenant, p1=f"%{w.run1}%", p2=f"%{w.run2}%"),
    }


def _resource_rows(engine, w: World) -> dict[str, int]:
    p = {"t": w.tenant, "k": RESOURCE, "a": AGENT}
    return {
        "working_memories(resource)": _count(
            engine, "SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'resource'"
            " AND scope_key = :k AND agent_id = :a", **p),
        "message_embeddings(resource)": _count(
            engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND resource_id = :k"
            " AND agent_id = :a", **p),
        "memory_updated events": _count(
            engine, "SELECT count(*) FROM execution_events WHERE tenant_id = :t AND type = 'memory_updated'"
            " AND data::text <> '{}' AND data ? 'changed_keys'", t=w.tenant),
        "ledger rows": _count(engine, "SELECT count(*) FROM side_effects WHERE tenant_id = :t AND key LIKE 'wm:%'",
                              t=w.tenant),
    }


def _kb_rows(engine, w: World) -> dict[str, int]:
    p = {"t": w.tenant, "kb": w.kb_id}
    return {
        "knowledge_bases": _count(engine, "SELECT count(*) FROM knowledge_bases WHERE tenant_id = :t AND id = :kb", **p),
        "kb_documents": _count(engine, "SELECT count(*) FROM kb_documents WHERE tenant_id = :t AND kb_id = :kb", **p),
        "kb_chunks": _count(engine, "SELECT count(*) FROM kb_chunks WHERE tenant_id = :t AND kb_id = :kb", **p),
    }


def _assert_zero(rows: dict[str, int], what: str) -> None:
    assert rows == dict.fromkeys(rows, 0), f"{what}: {rows}"


def test_delete_conversation_leaves_zero_rows_and_spares_the_other_tenant(engine, worlds):
    a, b = worlds
    before_b = snapshot(engine, b.tenant)
    assert MemoryStore(engine).delete_conversation(a.tenant, a.conv_id) is True
    _assert_zero(_conversation_rows(engine, a), "delete_conversation")
    # Resource-scoped memory is not the conversation's: it stays (forget_resource removes it).
    assert MemoryStore(engine).get_working_memory(a.tenant, AGENT, "resource", RESOURCE) is not None
    assert snapshot(engine, b.tenant) == before_b
    assert MemoryStore(engine).delete_conversation(a.tenant, a.conv_id) is False  # idempotent


@pytest.mark.parametrize("agent_scoped", [False, True])
def test_forget_resource_without_conversations_removes_cross_conversation_memory(engine, worlds, agent_scoped):
    a, b = worlds
    before_b = snapshot(engine, b.tenant)
    MemoryStore(engine).forget_resource(a.tenant, RESOURCE, agent_id=AGENT if agent_scoped else None)
    _assert_zero(_resource_rows(engine, a), "forget_resource")
    # Conversation data stays without include_conversations.
    assert _count(engine, "SELECT count(*) FROM conversations WHERE tenant_id = :t", t=a.tenant) == 1
    assert _count(engine, "SELECT count(*) FROM memory_observations WHERE tenant_id = :t", t=a.tenant) == 1
    assert snapshot(engine, b.tenant) == before_b


@pytest.mark.parametrize("agent_scoped", [False, True])
def test_forget_resource_with_conversations_leaves_zero_rows(engine, worlds, agent_scoped):
    a, b = worlds
    before_b = snapshot(engine, b.tenant)
    MemoryStore(engine).forget_resource(a.tenant, RESOURCE, agent_id=AGENT if agent_scoped else None,
                                        include_conversations=True)
    _assert_zero(_resource_rows(engine, a), "forget_resource(include_conversations)")
    _assert_zero(_conversation_rows(engine, a), "forget_resource(include_conversations)")
    for table in ("working_memories", "memory_observations", "message_embeddings", "conversations"):
        assert _count(engine, f"SELECT count(*) FROM {table} WHERE tenant_id = :t", t=a.tenant) == 0, table
    assert snapshot(engine, b.tenant) == before_b


def test_forget_resource_of_another_agent_touches_nothing(engine, worlds):
    a, b = worlds
    before = snapshot(engine, a.tenant, b.tenant)
    MemoryStore(engine).forget_resource(a.tenant, RESOURCE, agent_id=str(uuid.uuid4()), include_conversations=True)
    after = snapshot(engine, a.tenant, b.tenant)
    for table in ("working_memories", "memory_observations", "message_embeddings", "conversations", "runs"):
        assert after[a.tenant][table] == before[a.tenant][table], table
    assert after[b.tenant] == before[b.tenant]


def test_a_late_index_after_forget_is_skipped_and_never_recalled(engine, worlds):
    a, _ = worlds
    ms = MemoryStore(engine)
    ms.forget_resource(a.tenant, RESOURCE)
    result = ms.index_turn(a.tenant, AGENT, a.conv_id, a.run2, [TurnText("user", f"{a.marker} late", vec(5))],
                           embedding_model=MODEL)
    assert result.skipped
    assert _count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=a.tenant) == 0
    conv, _ = RunStore(engine).create_conversation(a.tenant, AGENT, title="n", resource_id=RESOURCE)
    assert ms.search_recall(a.tenant, AGENT, conv["id"], vec(5), embedding_model=MODEL) == []


def test_delete_document_and_knowledge_base_leave_zero_rows(engine, worlds):
    a, b = worlds
    ks = KnowledgeStore(engine)
    before_b = snapshot(engine, b.tenant)
    assert ks.delete_document(a.tenant, a.kb_id, "d1") is True
    assert _count(engine, "SELECT count(*) FROM kb_documents WHERE tenant_id = :t AND id = :d", t=a.tenant, d=a.doc_id) == 0
    assert _count(engine, "SELECT count(*) FROM kb_chunks WHERE tenant_id = :t AND document_id = :d",
                  t=a.tenant, d=a.doc_id) == 0
    assert ks.search(a.tenant, [a.kb_id], vec(1), embedding_model=MODEL, top_k=10) == []
    # A second document, then the knowledge base itself.
    ks.put_document(a.tenant, a.kb_id, document_key="d2", title="D2", content="more", mime_type="text/plain")
    assert ks.delete_knowledge_base(a.tenant, a.kb_id) is True
    _assert_zero(_kb_rows(engine, a), "delete_knowledge_base")
    assert snapshot(engine, b.tenant) == before_b
    assert ks.delete_knowledge_base(a.tenant, a.kb_id) is False


def test_delete_observations_and_working_memory_leave_zero_rows(engine, worlds):
    a, b = worlds
    ms = MemoryStore(engine)
    before_b = snapshot(engine, b.tenant)
    assert ms.delete_observations(a.tenant, a.conv_id) == 1
    assert _count(engine, "SELECT count(*) FROM memory_observations WHERE tenant_id = :t", t=a.tenant) == 0
    assert ms.delete_working_memory(a.tenant, AGENT, "conversation", a.conv_id) is True
    assert ms.delete_working_memory(a.tenant, AGENT, "resource", RESOURCE) is True
    assert _count(engine, "SELECT count(*) FROM working_memories WHERE tenant_id = :t", t=a.tenant) == 0
    assert snapshot(engine, b.tenant) == before_b


def test_retention_batches_are_per_tenant(engine, worlds):
    a, b = worlds
    ms = MemoryStore(engine)
    with engine.begin() as conn:
        for table in ("memory_observations", "message_embeddings"):
            conn.execute(text(f"UPDATE {table} SET created_at = now() - interval '9 days' WHERE tenant_id IN (:a, :b)"),
                         {"a": a.tenant, "b": b.tenant})
        conn.execute(text("UPDATE working_memories SET updated_at = now() - interval '9 days'"
                          " WHERE tenant_id IN (:a, :b)"), {"a": a.tenant, "b": b.tenant})
    before_b = snapshot(engine, b.tenant)
    cutoff = datetime.now(UTC) - timedelta(days=1)
    from eap_run_store import RETENTION_KINDS

    for kind in RETENTION_KINDS:
        after = None
        for _ in range(100):
            batch = ms.delete_retention_batch(kind, a.tenant, cutoff, after=after)
            if batch.done:
                break
            after = batch.next_after
    assert _count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=a.tenant) == 0
    # Old observations keep their coverage as a retention marker, without any text.
    assert _count(engine, "SELECT count(*) FROM memory_observations WHERE tenant_id = :t AND content LIKE :m",
                  t=a.tenant, m=f"%{a.marker}%") == 0
    assert _count(engine, "SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'conversation'",
                  t=a.tenant) == 0
    assert snapshot(engine, b.tenant) == before_b
