"""0011_knowledge_bases: upgrade, downgrade and upgrade on a populated 0010 schema; existing runs
satisfy the widened kind check; kb_ingest runs and the HNSW indexes; the extension survives."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

_RUN = (
    "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, tool_id, status, input, created_at, updated_at)"
    " VALUES (:id, 't1', :kind, :agent, :version, :tool, 'queued', 'hi', now(), now())"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _tables(conn) -> set[str]:
    return set(conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )).scalars())


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t AND table_schema = current_schema()"
    ), {"t": table}).scalars())


def _indexes(conn, table: str) -> dict[str, str]:
    return dict(conn.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t AND schemaname = current_schema()"
    ), {"t": table}).all())


def test_knowledge_bases_revision_round_trip_on_a_populated_0010_schema():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0011_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0010_run_options")
            conn.execute(text(_RUN), {"id": "agent-run", "kind": "agent", "agent": "ag", "version": 1, "tool": None})
            conn.execute(text(_RUN), {"id": "tool-run", "kind": "tool", "agent": None, "version": None, "tool": "x"})
            conn.execute(text(
                "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, created_at)"
                " VALUES ('t1', 'agent-run', 'model:0', 'test:echo', 1, 1, 0, 0, true, now())"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0011_knowledge_bases")

        with engine.connect() as conn:
            # Existing rows satisfy the new check and keep their data; usage defaults to 'chat'.
            assert conn.execute(text("SELECT count(*) FROM runs WHERE knowledge_base_id IS NULL")).scalar() == 2
            assert conn.execute(text("SELECT operation FROM run_model_requests")).scalar() == "chat"
            assert {"knowledge_bases", "kb_documents", "kb_chunks"} <= _tables(conn)
            chunk_indexes = _indexes(conn, "kb_chunks")
            for dims in (768, 1024, 1536):
                ddl = chunk_indexes[f"ix_kb_chunks_embedding_{dims}"]
                assert "USING hnsw" in ddl
                assert f"(embedding)::public.vector({dims})" in ddl
                assert "vector_cosine_ops" in ddl
                assert "m='16'" in ddl and "ef_construction='64'" in ddl
                assert f"WHERE (dimensions = {dims})" in ddl
            assert "WHERE (embedding IS NULL)" in chunk_indexes["ix_kb_chunks_unembedded"]
            assert "last_generation" in _columns(conn, "knowledge_bases")
            assert "ix_runs_tenant_kb_created" in _indexes(conn, "runs")
            assert conn.execute(text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
                " WHERE attrelid = 'kb_chunks'::regclass AND attname = 'embedding'"
            )).scalar() == "public.vector"

        kb = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, embedding_model, dimensions, chunking)"
                " VALUES (:id, 't1', 'Docs', 'test:hash', 1536, '{}'::jsonb)"
            ), {"id": kb})
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, knowledge_base_id, status, input, created_at, updated_at)"
                " VALUES ('ingest-run', 't1', 'kb_ingest', :kb, 'queued', '{}', now(), now())"
            ), {"kb": kb})
            conn.execute(text("INSERT INTO execution_events (run_id, tenant_id, seq, type, data, ts, event_key)"
                              " VALUES ('ingest-run', 't1', 0, 'ingest_progress', '{}', now(), 'p0')"))
            conn.execute(text(
                "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, created_at, operation)"
                " VALUES ('t1', 'ingest-run', 'embed:batch:0', 'test:hash', 5, 0, 0, 0, true, now(), 'embedding')"
            ))
        bad_runs = [
            # a kb_ingest run must name its knowledge base, and nothing else
            "INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at)"
            " VALUES ('b1', 't1', 'kb_ingest', 'queued', now(), now())",
            "INSERT INTO runs (id, tenant_id, kind, knowledge_base_id, agent_id, status, created_at, updated_at)"
            f" VALUES ('b2', 't1', 'kb_ingest', '{kb}', 'ag', 'queued', now(), now())",
            # an agent run never names one
            "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, knowledge_base_id, status, created_at,"
            f" updated_at) VALUES ('b3', 't1', 'agent', 'ag', 1, '{kb}', 'queued', now(), now())",
        ]
        for sql in bad_runs:
            with pytest.raises(IntegrityError, match="ck_runs_kind_target"):
                with engine.begin() as conn:
                    conn.execute(text(sql))
        with pytest.raises(IntegrityError, match="ck_run_model_requests_operation"):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens,"
                    " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, created_at, operation)"
                    " VALUES ('t1', 'agent-run', 'model:1', 'm', 1, 1, 0, 0, true, now(), 'other')"
                ))

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0010_run_options")
        with engine.connect() as conn:
            assert {"knowledge_bases", "kb_documents", "kb_chunks"}.isdisjoint(_tables(conn))
            assert "knowledge_base_id" not in _columns(conn, "runs")
            assert "operation" not in _columns(conn, "run_model_requests")
            assert conn.execute(text("SELECT id FROM runs ORDER BY id")).scalars().all() == ["agent-run", "tool-run"]
            assert conn.execute(text("SELECT count(*) FROM execution_events")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM run_model_requests")).scalar() == 1
            # The extension is per database and shared: a downgrade never drops it.
            assert conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")).scalar() == 1

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


def test_cli_upgrade_commits(tmp_path):
    """`alembic upgrade` through EAP_DATABASE_URL (the CLI path of env.py) commits its work."""
    import subprocess
    from pathlib import Path

    from eap_db import head_revisions

    base = _base_url()
    schema = f"eap_test_cli_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    root = Path(__file__).resolve().parents[3]
    try:
        env = {**os.environ, "EAP_DATABASE_URL": schema_url(base, schema)}
        subprocess.run(
            ["uv", "run", "alembic", "-c", "packages/db/alembic.ini", "upgrade", "head"],
            cwd=root, env=env, check=True, capture_output=True, timeout=180,
        )
        engine = create_engine(schema_url(base, schema))
        try:
            with engine.connect() as conn:
                assert set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars()) == head_revisions()
        finally:
            engine.dispose()
    finally:
        drop_schema(base, schema)
