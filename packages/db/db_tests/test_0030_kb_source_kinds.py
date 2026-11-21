"""0030_kb_source_kinds: connection kind `api_token`, source kind `repository_docs` and the ledger status
`removed` (the GitHub repository documents adapter)."""

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
KB = "11111111-1111-1111-1111-111111111111"


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _connection(conn, cid: str, kind: str) -> None:
    conn.execute(
        text("INSERT INTO settings_connections (tenant_id, id, name, kind) VALUES ('t1', :c, :n, :k)"),
        {"c": cid, "n": cid, "k": kind},
    )


def _source(conn, sid: str, cid: str, kind: str) -> None:
    conn.execute(
        text(
            "INSERT INTO kb_sources (tenant_id, id, knowledge_base_id, connection_id, name, kind, provider)"
            " VALUES ('t1', :s, CAST(:kb AS uuid), :c, :s, :k, 'github')"
        ),
        {"s": sid, "kb": KB, "c": cid, "k": kind},
    )


def test_upgrade_widens_the_kinds_and_downgrade_removes_the_new_rows():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0030_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0029_kb_sources")
            conn.execute(
                text(
                    "INSERT INTO knowledge_bases (id, tenant_id, name, embedding_model, dimensions, chunking)"
                    " VALUES (:kb, 't1', 'kb', 'm', 768, '{}')"
                ),
                {"kb": KB},
            )
            _connection(conn, "old", "oauth2_client_credentials")
            _source(conn, "old-s", "old", "product_catalog")
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _connection(conn, "tok", "api_token")
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0030_kb_source_kinds")
        with engine.begin() as conn:
            _connection(conn, "tok", "api_token")
            _source(conn, "repo-s", "tok", "repository_docs")
            entity = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO kb_entities (tenant_id, id, source_id, entity_type, canonical_name)"
                    " VALUES ('t1', :e, 'repo-s', 'repository', 'o/r')"
                ),
                {"e": entity},
            )
            conn.execute(
                text(
                    "INSERT INTO kb_entity_documents (tenant_id, entity_id, document_key, relationship, source_url,"
                    " status) VALUES ('t1', :e, 'k', 'file', 'https://x', 'removed')"
                ),
                {"e": entity},
            )
        with pytest.raises(IntegrityError):  # the vocabularies stay closed
            with engine.begin() as conn:
                conn.execute(text("UPDATE settings_connections SET kind = 'bogus'"))
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text("UPDATE kb_sources SET kind = 'bogus'"))
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0029_kb_sources")
        with engine.begin() as conn:
            assert {r[0] for r in conn.execute(text("SELECT kind FROM settings_connections"))} == {"oauth2_client_credentials"}
            assert {r[0] for r in conn.execute(text("SELECT kind FROM kb_sources"))} == {"product_catalog"}
            assert conn.execute(text("SELECT count(*) FROM kb_entity_documents")).scalar() == 0
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
