"""0033_revisions: `revision` NOT NULL DEFAULT 1 on the nine definition-class tables; downgrade drops them."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from eap_db.testing import SCHEMA_ENV, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

TABLES = ("agents", "workflows", "workflow_schedules", "eval_suites", "eval_cases", "settings_connections",
          "kb_sources", "scorers", "mcp_servers")


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _columns(conn, table: str) -> dict[str, tuple]:
    return {
        r[0]: (r[1], r[2]) for r in conn.execute(
            text("SELECT column_name, is_nullable, column_default FROM information_schema.columns"
                 " WHERE table_name = :t AND table_schema = current_schema()"), {"t": table})
    }


def test_upgrade_gives_existing_rows_revision_1_and_downgrade_drops_the_column():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0033_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0032_external_spans")
            conn.execute(text("INSERT INTO workflows (id, tenant_id, name, created_at, updated_at)"
                              " VALUES ('w1', 't1', 'w', now(), now())"))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0033_revisions")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT revision FROM workflows WHERE id = 'w1'")).scalar_one() == 1
            for table in TABLES:
                nullable, default = _columns(conn, table)["revision"]
                assert nullable == "NO" and default == "1", table
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0032_external_spans")
        with engine.begin() as conn:
            for table in TABLES:
                assert "revision" not in _columns(conn, table), table
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
