"""0009_version_restored_from: existing versions get NULL, the FK names a version of the same agent, clean round trip."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, drop_schema, schema_url

requires_schema = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

_VERSION_INSERT = (
    "INSERT INTO agent_versions (id, agent_id, tenant_id, version, spec, spec_hash, created_at, created_by, restored_from)"
    " VALUES (:id, :agent, 't1', :version, '{}', 'h', now(), 'u', :restored_from)"
)


def _base_url() -> str:
    from sqlalchemy.engine import make_url

    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t AND table_schema = current_schema()"
    ), {"t": table}).scalars())


@requires_schema
def test_version_restored_from_revision_round_trip():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0009_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0008_run_usage")
            for agent in ("ag", "other"):
                conn.execute(text(
                    "INSERT INTO agents (id, tenant_id, name, archived, created_at, updated_at)"
                    " VALUES (:a, 't1', 'A', false, now(), now())"
                ), {"a": agent})
            conn.execute(text(
                "INSERT INTO agent_versions (id, agent_id, tenant_id, version, spec, spec_hash, created_at, created_by)"
                " VALUES ('v1', 'ag', 't1', 1, '{}', 'h', now(), 'u')"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0009_version_restored_from")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT restored_from FROM agent_versions WHERE id = 'v1'")).scalar() is None
            # No schema_diff here: the models may be past 0009; the round trip ends at head and checks it there.

        with engine.begin() as conn:
            conn.execute(text(_VERSION_INSERT), {"id": "v2", "agent": "ag", "version": 2, "restored_from": 1})
        with pytest.raises(IntegrityError, match="fk_agent_versions_restored_from"):
            with engine.begin() as conn:  # no version 7 of this agent
                conn.execute(text(_VERSION_INSERT), {"id": "v3", "agent": "ag", "version": 3, "restored_from": 7})
        with pytest.raises(IntegrityError, match="fk_agent_versions_restored_from"):
            with engine.begin() as conn:  # version 1 exists, but of another agent
                conn.execute(text(_VERSION_INSERT), {"id": "o2", "agent": "other", "version": 2, "restored_from": 1})

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0008_run_usage")
        with engine.connect() as conn:
            assert "restored_from" not in _columns(conn, "agent_versions")
            assert conn.execute(text("SELECT id FROM agent_versions ORDER BY id")).scalars().all() == ["v1", "v2"]

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)
