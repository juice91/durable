"""0005_workflows: tenant-owned drafts and immutable versions, then a clean downgrade.

Private-schema setup matches packages/db/db_tests/test_migrations.py: a schema of our own,
alembic against that connection, and drop_schema in finally. Integrity errors each get their
own transaction so a failed insert does not abort the checks that follow.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

requires_schema = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

_WORKFLOW = (
    "INSERT INTO workflows (id, tenant_id, name, draft_spec, archived, created_at, updated_at) "
    "VALUES (:id, :tenant_id, :name, CAST(:spec AS jsonb), false, now(), now())"
)
_VERSION = (
    "INSERT INTO workflow_versions (id, workflow_id, tenant_id, version, spec, spec_hash, created_at, created_by) "
    "VALUES (:id, :workflow_id, :tenant_id, :version, CAST(:spec AS jsonb), :spec_hash, now(), 'u1')"
)


def _base_url() -> str:
    # The session URL without its search_path option: the database the session schema lives in.
    from sqlalchemy.engine import make_url

    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _tables(conn, schema: str) -> set[str]:
    return set(
        conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
            {"s": schema},
        ).scalars()
    )


def _spec() -> str:
    return json.dumps(
        {
            "name": "Test",
            "input_schema": {"type": "object"},
            "start": "step1",
            "steps": [
                {"id": "step1", "type": "agent", "agent_id": str(uuid.uuid4()), "version": 1, "input": {}, "next": "step2"},
                {"id": "step2", "type": "tool", "tool_id": "clock.now", "input": {}},
            ],
        }
    )


@requires_schema
def test_workflows_revision_creates_tables_and_round_trips():
    """Upgrade to 0005 creates both tables. Downgrade to 0004 drops them. Upgrade again brings them back."""
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0005_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0005_workflows")
        with engine.connect() as conn:
            assert {"workflows", "workflow_versions"} <= _tables(conn, schema)
            cols = set(
                conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :s AND table_name = 'workflows'"
                    ),
                    {"s": schema},
                ).scalars()
            )
            assert {"id", "tenant_id", "name", "draft_spec", "archived", "created_at", "updated_at"} <= cols
            draft_type = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'workflows' AND column_name = 'draft_spec'"
                ),
                {"s": schema},
            ).scalar_one()
            assert draft_type == "jsonb"
            unique_cols = conn.execute(
                text(
                    "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema "
                    "WHERE tc.table_schema = :s AND tc.constraint_name = 'uq_workflow_version' "
                    "ORDER BY kcu.ordinal_position"
                ),
                {"s": schema},
            ).scalars().all()
            assert unique_cols == ["workflow_id", "version"]
            fk_cols = conn.execute(
                text(
                    "SELECT kcu.column_name FROM information_schema.key_column_usage kcu "
                    "WHERE kcu.table_schema = :s AND kcu.constraint_name = 'fk_workflow_versions_tenant_workflow' "
                    "ORDER BY kcu.ordinal_position"
                ),
                {"s": schema},
            ).scalars().all()
            assert fk_cols == ["tenant_id", "workflow_id"]

        spec = _spec()
        with engine.begin() as conn:
            conn.execute(text(_WORKFLOW), {"id": "w1", "tenant_id": "t1", "name": "Test", "spec": spec})
            conn.execute(
                text(_VERSION),
                {"id": "v1", "workflow_id": "w1", "tenant_id": "t1", "version": 1, "spec": spec, "spec_hash": "h1"},
            )
        with engine.connect() as conn:
            stored = conn.execute(text("SELECT draft_spec FROM workflows WHERE id = 'w1'")).scalar_one()
            assert stored["steps"][0]["type"] == "agent"
            assert stored["steps"][1]["tool_id"] == "clock.now"

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0004_mcp_servers")
        with engine.connect() as conn:
            tables = _tables(conn, schema)
            assert "workflows" not in tables
            assert "workflow_versions" not in tables

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0005_workflows")
        with engine.begin() as conn:
            assert {"workflows", "workflow_versions"} <= _tables(conn, schema)
            conn.execute(text(_WORKFLOW), {"id": "w1", "tenant_id": "t1", "name": "Test", "spec": spec})
            assert conn.execute(text("SELECT count(*) FROM workflows")).scalar_one() == 1
    finally:
        engine.dispose()
        drop_schema(base, schema)


@requires_schema
def test_workflows_revision_unique_version_and_tenant_fk():
    """UNIQUE(workflow_id, version) and the composite tenant foreign key both reject a bad insert."""
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0005fk_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    spec = "{}"
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0005_workflows")
        with engine.begin() as conn:
            conn.execute(text(_WORKFLOW), {"id": "w1", "tenant_id": "t1", "name": "A", "spec": spec})
            conn.execute(text(_WORKFLOW), {"id": "w2", "tenant_id": "t1", "name": "B", "spec": spec})
            conn.execute(
                text(_VERSION),
                {"id": "v1", "workflow_id": "w1", "tenant_id": "t1", "version": 1, "spec": spec, "spec_hash": "h1"},
            )
            conn.execute(
                text(_VERSION),
                {"id": "v2", "workflow_id": "w1", "tenant_id": "t1", "version": 2, "spec": spec, "spec_hash": "h2"},
            )

        with pytest.raises(IntegrityError, match="uq_workflow_version"):
            with engine.begin() as conn:
                conn.execute(
                    text(_VERSION),
                    {"id": "v3", "workflow_id": "w1", "tenant_id": "t1", "version": 1, "spec": spec, "spec_hash": "h3"},
                )
        with pytest.raises(IntegrityError, match="fk_workflow_versions_tenant_workflow"):
            with engine.begin() as conn:
                conn.execute(
                    text(_VERSION),
                    # version 3 is free, so this fails the tenant composite FK and not the unique pair
                    {"id": "v4", "workflow_id": "w1", "tenant_id": "t2", "version": 3, "spec": spec, "spec_hash": "h4"},
                )
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM workflow_versions")).scalar_one() == 2
    finally:
        engine.dispose()
        drop_schema(base, schema)
