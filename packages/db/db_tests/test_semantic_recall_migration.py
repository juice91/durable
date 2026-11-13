"""0013_semantic_recall: upgrade, downgrade and upgrade on a populated 0012 schema (a throwaway
schema). The embedding check has no NULL hole, the tenant-composite FKs cascade, the HNSW indexes
exist per dimension, the downgrade keeps the extension, and the result matches the models."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _tables(conn) -> set[str]:
    return set(conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )).scalars())


def _vec(dims: int, hot: int = 0) -> str:
    v = ["0"] * dims
    v[hot] = "1"
    return "[" + ",".join(v) + "]"


_ME = (
    "INSERT INTO message_embeddings (tenant_id, agent_id, resource_id, conversation_id, turn, run_id, role, text,"
    " embedding_model, dimensions, embedding)"
    " VALUES (:t, 'ag', 'u1', :conv, :turn, :run, :role, 'hi', 'test:hash', :dims, CAST(:e AS public.vector))"
)


def _fails(engine, sql: str, constraint: str, **params) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        with engine.begin() as conn:
            conn.execute(text(sql), params)


def test_semantic_recall_revision_round_trip_on_a_populated_0012_schema():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0013_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0012_memory")
            conn.execute(text(
                "INSERT INTO conversations (id, tenant_id, agent_id, resource_id, created_at, updated_at)"
                " VALUES ('c1', 't1', 'ag', 'u1', now(), now()), ('c2', 't2', 'ag', 'u1', now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, conversation_id, turn,"
                " created_at, updated_at) VALUES"
                " ('r1', 't1', 'agent', 'ag', 1, 'completed', 'hi', 'c1', 1, now(), now()),"
                " ('r2', 't1', 'agent', 'ag', 1, 'completed', 'hi', 'c1', 2, now(), now()),"
                " ('r9', 't2', 'agent', 'ag', 1, 'completed', 'hi', 'c2', 1, now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO working_memories (tenant_id, agent_id, scope, scope_key, format, content, version)"
                " VALUES ('t1', 'ag', 'resource', 'u1', 'markdown', '\"# note\"'::jsonb, 1)"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0013_semantic_recall")

        with engine.connect() as conn:
            assert "message_embeddings" in _tables(conn)
            indexes = dict(conn.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()"
                " AND tablename = 'message_embeddings'"
            )).all())
        for dims in (768, 1024, 1536):
            definition = indexes[f"ix_message_embeddings_embedding_{dims}"]
            assert "USING hnsw" in definition and f"vector({dims})" in definition
            assert "vector_cosine_ops" in definition and f"(dimensions = {dims})" in definition
            assert "m='16'" in definition and "ef_construction='64'" in definition
        assert {"ix_message_embeddings_tenant_agent_resource", "ix_message_embeddings_tenant_conversation_turn",
                "ix_message_embeddings_tenant_created", "uq_message_embeddings_run_role"} <= set(indexes)

        # forget watermarks: one per conversation, tenant-composite FK, non-negative turn, cascade
        wm = ("INSERT INTO memory_forget_watermarks (tenant_id, conversation_id, forgotten_through_turn)"
              " VALUES (:t, :conv, :turn)")
        with engine.begin() as conn:
            conn.execute(text(wm), {"t": "t1", "conv": "c1", "turn": 2})
        _fails(engine, wm, "memory_forget_watermarks_pkey", t="t1", conv="c1", turn=3)
        _fails(engine, wm, "fk_memory_forget_watermarks_tenant_conversation", t="t1", conv="c2", turn=1)
        _fails(engine, wm, "ck_memory_forget_watermarks_turn", t="t2", conv="c2", turn=-1)

        with engine.begin() as conn:
            for dims, turn, run, role in ((768, 1, "r1", "user"), (1024, 1, "r1", "assistant"), (1536, 2, "r2", "user")):
                conn.execute(text(_ME), {"t": "t1", "conv": "c1", "turn": turn, "run": run, "role": role,
                                         "dims": dims, "e": _vec(dims)})

        ok = {"t": "t1", "conv": "c1", "turn": 2, "run": "r2", "role": "assistant", "dims": 768, "e": _vec(768)}
        _fails(engine, _ME, "uq_message_embeddings_run_role", **{**ok, "role": "user", "dims": 1536, "e": _vec(1536)})
        _fails(engine, _ME, "ck_message_embeddings_role", **{**ok, "role": "system"})
        _fails(engine, _ME, "ck_message_embeddings_turn", **{**ok, "turn": 0})
        # dimensions must match the vector and be one of the indexed sizes; NULL never passes
        _fails(engine, _ME, "ck_message_embeddings_embedding", **{**ok, "dims": 1536})
        _fails(engine, _ME, "ck_message_embeddings_embedding", **{**ok, "dims": 512, "e": _vec(512)})
        with pytest.raises(IntegrityError, match="null value|ck_message_embeddings_embedding"):
            with engine.begin() as conn:
                conn.execute(text(_ME.replace(":dims", "NULL")), ok)
        with pytest.raises(IntegrityError, match="null value|ck_message_embeddings_embedding"):
            with engine.begin() as conn:
                conn.execute(text(_ME.replace("CAST(:e AS public.vector)", "NULL")), ok)
        # tenant-composite FKs: another tenant's conversation or run
        _fails(engine, _ME, "fk_message_embeddings_tenant_conversation", **{**ok, "conv": "c2"})
        _fails(engine, _ME, "fk_message_embeddings_tenant_run", **{**ok, "run": "r9"})

        # The conversation FK cascades (runs detached first, as delete_conversation does); so does the run FK.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM runs WHERE id = 'r2'"))
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM message_embeddings")).scalar() == 2
        with engine.begin() as conn:
            conn.execute(text("UPDATE runs SET conversation_id = NULL, turn = NULL WHERE conversation_id = 'c1'"))
            conn.execute(text("DELETE FROM conversations WHERE id = 'c1'"))
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM message_embeddings")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM memory_forget_watermarks")).scalar() == 0
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO conversations (id, tenant_id, agent_id, resource_id, created_at, updated_at)"
                " VALUES ('c3', 't1', 'ag', 'u1', now(), now())"
            ))
            conn.execute(text("UPDATE runs SET conversation_id = 'c3', turn = 1 WHERE id = 'r1'"))
            conn.execute(text(_ME), {**ok, "conv": "c3", "turn": 1, "run": "r1"})

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0012_memory")
        with engine.connect() as conn:
            assert {"message_embeddings", "memory_forget_watermarks"}.isdisjoint(_tables(conn))
            assert conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")).scalar() == 1
            assert set(conn.execute(text("SELECT id FROM conversations")).scalars()) == {"c2", "c3"}
            assert conn.execute(text("SELECT count(*) FROM runs")).scalar() == 2
            assert conn.execute(text("SELECT count(*) FROM working_memories")).scalar() == 1

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
