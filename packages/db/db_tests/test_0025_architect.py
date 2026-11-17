"""0025_architect: builtin flag on agents, created_via marker on agent and workflow versions."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from eap_db import head_revisions
from eap_db.testing import SCHEMA_ENV, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t AND table_schema = current_schema()"
    ), {"t": table}).scalars())


def test_head_is_the_slug_revision_after_0025():
    assert head_revisions() == {"0026_architect_slugs"}


def test_upgrade_adds_the_columns_and_downgrade_removes_them():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0025_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0023_attachments_r2")
            conn.execute(text("INSERT INTO agents (id, tenant_id, name, archived, created_at, updated_at) VALUES ('a1', 't1', 'old', false, now(), now())"))
        with engine.connect() as conn:
            assert "builtin" not in _columns(conn, "agents")
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0025_architect")
        with engine.connect() as conn:
            assert "builtin" in _columns(conn, "agents")
            assert "created_via" in _columns(conn, "agent_versions")
            assert "created_via" in _columns(conn, "workflow_versions")
            # Existing rows are not builtin; the models match the migrated schema.
            assert conn.execute(text("SELECT builtin FROM agents WHERE id = 'a1'")).scalar_one() is False
            # (the models also carry the 0026 slugs now; head-vs-models is checked in test_migrations.py)
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0023_attachments_r2")
        with engine.connect() as conn:
            assert "builtin" not in _columns(conn, "agents")
            assert "created_via" not in _columns(conn, "agent_versions")
            assert "created_via" not in _columns(conn, "workflow_versions")
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
