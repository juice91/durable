"""0028_mcp_catalog_source: `source` and `meta` on the MCP catalog mirror."""

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


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _columns(conn) -> set[str]:
    rows = conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'mcp_catalog_entries' AND table_schema = current_schema()"
    ))
    return {r[0] for r in rows}


def test_upgrade_defaults_existing_rows_to_official_and_downgrade_drops_toolhive_rows():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0028_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    engine = create_engine(schema_url(base, schema))
    insert = (
        "INSERT INTO mcp_catalog_entries (name, version, fetched_at{extra}) "
        "VALUES (:n, '1', now(){vals})"
    )
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0027_tenant_secrets")
            conn.execute(text(insert.format(extra="", vals="")), {"n": "io.example/old"})
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0028_mcp_catalog_source")
        with engine.begin() as conn:
            assert {"source", "meta"} <= _columns(conn)
            row = conn.execute(text("SELECT source, meta FROM mcp_catalog_entries")).one()
            assert row[0] == "official" and row[1] is None
            conn.execute(
                text(insert.format(extra=", source, meta", vals=", 'toolhive', CAST(:m AS jsonb)")),
                {"n": "io.github.stacklok/fetch", "m": '{"tier": "Community"}'},
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(insert.format(extra=", source", vals=", 'other'")), {"n": "io.example/bad"})
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0027_tenant_secrets")
        with engine.begin() as conn:
            assert not ({"source", "meta"} & _columns(conn))
            assert [r[0] for r in conn.execute(text("SELECT name FROM mcp_catalog_entries"))] == ["io.example/old"]
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
