"""RM.5 acceptance: 0012_memory on a populated 0011 schema (conversations with turns, runs, usage,
events, knowledge bases): upgrade, downgrade and upgrade again, nothing lost, `alembic check` clean.

Throwaway schemas only; the dev database's own tables are never touched."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

NEW_TABLES = {"working_memories", "memory_observations"}
NEW_INDEXES = {
    "ix_conversations_tenant_resource", "ix_working_memories_tenant_scope_key",
    "ix_working_memories_tenant_conversation", "ix_working_memories_conversation_updated",
    "ix_memory_observations_tenant_superseded_by", "ix_memory_observations_created",
}


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _populate_0011(conn) -> None:
    kb = str(uuid.uuid4())
    conn.execute(text(
        "INSERT INTO knowledge_bases (id, tenant_id, name, embedding_model, dimensions, chunking)"
        " VALUES (:id, 't1', 'Docs', 'test:hash', 1536, '{}'::jsonb)"), {"id": kb})
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, knowledge_base_id, status, input, created_at, updated_at)"
        " VALUES ('ingest-run', 't1', 'kb_ingest', :kb, 'completed', '{}', now(), now())"), {"kb": kb})
    for tenant, conv in (("t1", "c-a"), ("t1", "c-b"), ("t2", "c-z")):
        conn.execute(text(
            "INSERT INTO conversations (id, tenant_id, agent_id, title, created_at, updated_at)"
            " VALUES (:c, :t, 'ag', :title, now(), now())"), {"c": conv, "t": tenant, "title": f"{tenant}/{conv}"})
        for turn in (1, 2, 3):
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
                " VALUES (:r, :t, 0, 'run_started', '{}', now(), 'e0')"), {"t": tenant, "r": run})


def _state(conn) -> dict:
    q = lambda sql: [tuple(r) for r in conn.execute(text(sql))]  # noqa: E731
    return {
        "conversations": q("SELECT id, tenant_id, agent_id, title FROM conversations ORDER BY tenant_id, id"),
        "runs": q("SELECT id, tenant_id, kind, status, input, output, messages::text, conversation_id, turn"
                  " FROM runs ORDER BY id"),
        "usage": q("SELECT run_id, request_key, input_tokens, output_tokens FROM run_model_requests ORDER BY 1, 2"),
        "events": q("SELECT run_id, seq, type FROM execution_events ORDER BY 1, 2"),
        "kbs": q("SELECT id, tenant_id, name, embedding_model, dimensions FROM knowledge_bases ORDER BY id"),
    }


def _schema_objects(conn) -> tuple[set[str], set[str], set[str], set[str]]:
    tables = set(conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")).scalars())
    indexes = set(conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")).scalars())
    constraints = set(conn.execute(text(
        "SELECT c.conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
        " WHERE n.nspname = current_schema()")).scalars())
    columns = set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'conversations'"
        " AND table_schema = current_schema()")).scalars())
    return tables, indexes, constraints, columns


def test_populated_0011_upgrade_downgrade_upgrade_and_alembic_check():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0012a_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0011_knowledge_bases")
            _populate_0011(conn)
            before = _state(conn)
            tables_11, indexes_11, constraints_11, columns_11 = _schema_objects(conn)
        assert len(before["conversations"]) == 3 and len(before["runs"]) == 10 and len(before["kbs"]) == 1
        assert not NEW_TABLES & tables_11 and "resource_id" not in columns_11

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0012_memory")
        with engine.connect() as conn:
            assert _state(conn) == before  # nothing lost or changed
            assert conn.execute(text("SELECT count(*) FROM conversations WHERE resource_id IS NULL")).scalar() == 3
            assert conn.execute(text("SELECT count(*) FROM working_memories")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM memory_observations")).scalar() == 0
            tables, indexes, _, columns = _schema_objects(conn)
            assert NEW_TABLES <= tables and NEW_INDEXES <= indexes and "resource_id" in columns
        # An old API that never heard of resource_id still creates conversations.
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO conversations (id, tenant_id, agent_id, created_at, updated_at)"
                " VALUES ('c-new', 't1', 'ag', now(), now())"))
            assert conn.execute(text("SELECT resource_id FROM conversations WHERE id = 'c-new'")).scalar() is None
            conn.execute(text("DELETE FROM conversations WHERE id = 'c-new'"))

        # Give the new tables rows (one conversation each way), then downgrade: they go, the rest stays.
        with engine.begin() as conn:
            conn.execute(text("UPDATE conversations SET resource_id = 'user:ana' WHERE tenant_id = 't1' AND id = 'c-a'"))
            conn.execute(text(
                "INSERT INTO working_memories (tenant_id, agent_id, scope, scope_key, conversation_id, format,"
                " content, version) VALUES ('t1', 'ag', 'conversation', 'c-a', 'c-a', 'markdown', '\"x\"', 1),"
                " ('t1', 'ag', 'resource', 'user:ana', NULL, 'json', '{\"a\": 1}', 2)"))
            conn.execute(text(
                "INSERT INTO memory_observations (tenant_id, agent_id, conversation_id, kind, covers_from_turn,"
                " covers_to_turn, content, approx_tokens, run_id) VALUES"
                " ('t1', 'ag', 'c-a', 'observation', 1, 2, 'obs', 1, 't1-c-a-2')"))
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0011_knowledge_bases")
        with engine.connect() as conn:
            assert _state(conn) == before  # every conversation, run, usage row, event and KB intact
            tables, indexes, constraints, columns = _schema_objects(conn)
            assert (tables, indexes, constraints, columns) == (tables_11, indexes_11, constraints_11, columns_11)
            assert not NEW_TABLES & tables and not NEW_INDEXES & indexes
            assert "ck_conversations_resource_id" not in constraints and "resource_id" not in columns

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0012_memory")
        with engine.connect() as conn:
            assert _state(conn) == before
            # resource ids were dropped with the column: every conversation is NULL again
            assert conn.execute(text("SELECT count(*) FROM conversations WHERE resource_id IS NOT NULL")).scalar() == 0

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


def test_fresh_head_schema_is_in_sync_with_the_models():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0012b_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        check = create_engine(schema_url(base, schema, public=True))
        try:
            with check.connect() as conn:
                assert schema_diff(conn) == []
        finally:
            check.dispose()
    finally:
        engine.dispose()
        drop_schema(base, schema)
