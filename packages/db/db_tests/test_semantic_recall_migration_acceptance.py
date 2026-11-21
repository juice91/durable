"""RM.8 acceptance: 0013_semantic_recall on a populated 0012 schema (conversations with and without
resource_id, working memories, observations, runs, usage, events, knowledge bases): upgrade,
downgrade and upgrade again, nothing lost, the vector extension kept, `alembic check` clean.

Throwaway schemas only; the dev database's own tables are never touched."""

from __future__ import annotations

import os
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from eap_db.metadata import schema_diff
from eap_db.testing import create_schema, drop_schema, schema_url

HNSW = {f"ix_message_embeddings_embedding_{d}" for d in (768, 1024, 1536)}
NEW_INDEXES = HNSW | {
    "ix_message_embeddings_tenant_agent_resource", "ix_message_embeddings_tenant_conversation_turn",
    "ix_message_embeddings_tenant_created", "message_embeddings_pkey", "uq_message_embeddings_run_role",
    "memory_forget_watermarks_pkey",
}


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _populate_0012(conn) -> None:
    kb = str(uuid.uuid4())
    conn.execute(text(
        "INSERT INTO knowledge_bases (id, tenant_id, name, embedding_model, dimensions, chunking)"
        " VALUES (:id, 't1', 'Docs', 'test:hash', 1536, '{}'::jsonb)"), {"id": kb})
    for tenant, conv, res in (("t1", "c-a", "user:ana"), ("t1", "c-b", "user:ana"), ("t1", "c-n", None),
                              ("t2", "c-z", "user:ana")):
        conn.execute(text(
            "INSERT INTO conversations (id, tenant_id, agent_id, resource_id, title, created_at, updated_at)"
            " VALUES (:c, :t, 'ag', :res, :title, now(), now())"),
            {"c": conv, "t": tenant, "res": res, "title": f"{tenant}/{conv}"})
        for turn in (1, 2):
            run = f"{tenant}-{conv}-{turn}"
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, output, messages,"
                " conversation_id, turn, created_at, updated_at) VALUES (:r, :t, 'agent', 'ag', 1, 'completed',"
                " 'in', 'out', CAST('[{\"k\": 1}]' AS jsonb), :c, :n, now(), now())"),
                {"r": run, "t": tenant, "c": conv, "n": turn})
            conn.execute(text(
                "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, created_at)"
                " VALUES (:t, :r, 'model:0', 'test:echo', 3, 4, 0, 0, true, now())"), {"t": tenant, "r": run})
            conn.execute(text(
                "INSERT INTO execution_events (run_id, tenant_id, seq, type, data, ts, event_key)"
                " VALUES (:r, :t, 0, 'run_started', '{}', now(), 'e0')"), {"r": run, "t": tenant})
    conn.execute(text(
        "INSERT INTO working_memories (tenant_id, agent_id, scope, scope_key, conversation_id, format, content,"
        " version) VALUES ('t1', 'ag', 'conversation', 'c-a', 'c-a', 'markdown', '\"x\"', 1),"
        " ('t1', 'ag', 'resource', 'user:ana', NULL, 'json', '{\"a\": 1}', 2)"))
    conn.execute(text(
        "INSERT INTO memory_observations (tenant_id, agent_id, conversation_id, kind, covers_from_turn,"
        " covers_to_turn, content, approx_tokens, run_id) VALUES"
        " ('t1', 'ag', 'c-a', 'observation', 1, 2, 'obs', 1, 't1-c-a-2')"))


def _state(conn) -> dict:
    q = lambda sql: [tuple(r) for r in conn.execute(text(sql))]  # noqa: E731
    return {
        "conversations": q("SELECT id, tenant_id, agent_id, resource_id, title FROM conversations ORDER BY 2, 1"),
        "runs": q("SELECT id, tenant_id, kind, status, input, output, messages::text, conversation_id, turn"
                  " FROM runs ORDER BY id"),
        "usage": q("SELECT run_id, request_key, input_tokens, output_tokens FROM run_model_requests ORDER BY 1, 2"),
        "events": q("SELECT run_id, seq, type FROM execution_events ORDER BY 1, 2"),
        "kbs": q("SELECT id, tenant_id, name, embedding_model, dimensions FROM knowledge_bases ORDER BY id"),
        "notes": q("SELECT tenant_id, agent_id, scope, scope_key, conversation_id, format, content::text, version"
                   " FROM working_memories ORDER BY 1, 3, 4"),
        "observations": q("SELECT tenant_id, conversation_id, kind, covers_from_turn, covers_to_turn, content,"
                          " run_id FROM memory_observations ORDER BY 1, 2, 4"),
    }


def _objects(conn) -> tuple[set[str], set[str], set[str]]:
    tables = set(conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")).scalars())
    indexes = set(conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")).scalars())
    constraints = set(conn.execute(text(
        "SELECT c.conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
        " WHERE n.nspname = current_schema()")).scalars())
    return tables, indexes, constraints


def _extension(conn) -> tuple[str, str]:
    return tuple(conn.execute(text(
        "SELECT e.extname, n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace"
        " WHERE e.extname = 'vector'")).one())


def _embed(conn, run: str, conv: str, turn: int, role: str, dims: int) -> None:
    vec = "[" + ",".join(["1"] + ["0"] * (dims - 1)) + "]"
    conn.execute(text(
        "INSERT INTO message_embeddings (tenant_id, agent_id, resource_id, conversation_id, turn, run_id, role,"
        " text, embedding_model, dimensions, embedding) VALUES ('t1', 'ag', 'user:ana', :c, :n, :r, :role, 'hi',"
        " 'test:hash', :d, CAST(:e AS public.vector))"), {"c": conv, "n": turn, "r": run, "role": role, "d": dims, "e": vec})


def test_populated_0012_upgrade_downgrade_upgrade_and_alembic_check():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0013a_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0012_memory")
            _populate_0012(conn)
            before = _state(conn)
            tables_12, indexes_12, constraints_12 = _objects(conn)
            extension = _extension(conn)
        assert len(before["conversations"]) == 4 and len(before["runs"]) == 8 and len(before["notes"]) == 2
        assert len(before["observations"]) == 1 and len(before["kbs"]) == 1
        assert {"message_embeddings", "memory_forget_watermarks"}.isdisjoint(tables_12)

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0013_semantic_recall")
        with engine.connect() as conn:
            assert _state(conn) == before  # nothing lost or changed by the upgrade
            assert conn.execute(text("SELECT count(*) FROM message_embeddings")).scalar() == 0
            tables, indexes, _ = _objects(conn)
            assert {"message_embeddings", "memory_forget_watermarks"} <= tables and NEW_INDEXES <= indexes

        # Index rows of every dimension, one conversation without a resource id, then downgrade.
        with engine.begin() as conn:
            _embed(conn, "t1-c-a-1", "c-a", 1, "user", 768)
            _embed(conn, "t1-c-a-1", "c-a", 1, "assistant", 1024)
            _embed(conn, "t1-c-b-2", "c-b", 2, "user", 1536)
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0012_memory")
        with engine.connect() as conn:
            assert _state(conn) == before  # the recall table is derived data; everything else stays
            assert _objects(conn) == (tables_12, indexes_12, constraints_12)
            assert not NEW_INDEXES & _objects(conn)[1]
            assert _extension(conn) == extension  # kept, same schema
            assert conn.execute(text("SELECT '[1,0]'::public.vector(2) OPERATOR(public.<=>) '[1,0]'::public.vector(2)"
                                     )).scalar() == 0  # the type and operator still resolve

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0013_semantic_recall")
        with engine.connect() as conn:
            assert _state(conn) == before
            assert conn.execute(text("SELECT count(*) FROM message_embeddings")).scalar() == 0
            assert NEW_INDEXES <= _objects(conn)[1]
        with engine.begin() as conn:
            _embed(conn, "t1-c-a-2", "c-a", 2, "user", 1536)  # the re-created table works
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        check = create_engine(schema_url(base, schema, public=True))
        try:
            with check.connect() as conn:
                assert schema_diff(conn) == []  # what `alembic check` reports
        finally:
            check.dispose()
    finally:
        engine.dispose()
        drop_schema(base, schema)
