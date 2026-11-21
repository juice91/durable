"""0029_kb_sources: connections, sources, records and documents of the knowledge-base source sync, and
the `kb_source_sync` run kind."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

TABLES = {
    "settings_connections", "kb_sources", "kb_source_syncs", "kb_source_records", "kb_entities",
    "kb_entity_identifiers", "kb_entity_parameters", "kb_entity_documents",
}
KB = "11111111-1111-1111-1111-111111111111"


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _tables(conn) -> set[str]:
    rows = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"))
    return {r[0] for r in rows} & TABLES


def _run(conn, rid: str, kind: str, kb: str | None) -> None:
    conn.execute(
        text(
            "INSERT INTO runs (id, tenant_id, kind, knowledge_base_id, status, input, created_at, updated_at)"
            " VALUES (:id, 't1', :k, :kb, 'queued', '{}', now(), now())"
        ),
        {"id": rid, "k": kind, "kb": kb},
    )


def test_upgrade_creates_tables_allows_sync_runs_and_downgrade_removes_them():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0029_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0028_mcp_catalog_source")
            assert not _tables(conn)
            conn.execute(
                text(
                    "INSERT INTO knowledge_bases (id, tenant_id, name, embedding_model, dimensions, chunking)"
                    " VALUES (:kb, 't1', 'kb', 'm', 768, '{}')"
                ),
                {"kb": KB},
            )
        with pytest.raises(IntegrityError):  # not yet a run kind
            with engine.begin() as conn:
                _run(conn, str(uuid.uuid4()), "kb_source_sync", KB)
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0029_kb_sources")
        with engine.begin() as conn:
            assert _tables(conn) == TABLES
            sync_run = str(uuid.uuid4())
            _run(conn, sync_run, "kb_source_sync", KB)
            _run(conn, str(uuid.uuid4()), "kb_ingest", KB)
        with pytest.raises(IntegrityError):  # a sync run names its knowledge base
            with engine.begin() as conn:
                _run(conn, str(uuid.uuid4()), "kb_source_sync", None)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO settings_connections (tenant_id, id, name, kind)"
                    " VALUES ('t1', :c, 'c', 'oauth2_client_credentials')"
                ),
                {"c": "c1"},
            )
        with pytest.raises(IntegrityError):  # kind vocabulary
            with engine.begin() as conn:
                conn.execute(text("UPDATE settings_connections SET kind = 'bogus'"))
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0028_mcp_catalog_source")
        with engine.begin() as conn:
            assert not _tables(conn)
            kinds = {r[0] for r in conn.execute(text("SELECT kind FROM runs"))}
            assert kinds == {"kb_ingest"}  # the sync run is deleted, the ingest run stays
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
