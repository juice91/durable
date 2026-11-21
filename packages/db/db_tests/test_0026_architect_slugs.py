"""0026_architect_slugs: a per-tenant unique slug on agents and workflows."""

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


def _has_slug(conn, table: str) -> bool:
    return bool(conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = 'slug' "
        "AND table_schema = current_schema()"
    ), {"t": table}).first())


def test_upgrade_adds_unique_slugs_and_downgrade_removes_them():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0026_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0026_architect_slugs")
        insert = (
            "INSERT INTO agents (id, tenant_id, name, archived, created_at, updated_at, slug) "
            "VALUES (:id, :t, 'n', false, now(), now(), :s)"
        )
        with engine.begin() as conn:
            assert _has_slug(conn, "agents") and _has_slug(conn, "workflows")
            conn.execute(text(insert), {"id": "a1", "t": "t1", "s": "helper"})
            conn.execute(text(insert), {"id": "a2", "t": "t2", "s": "helper"})  # another tenant: fine
            conn.execute(text(insert), {"id": "a3", "t": "t1", "s": None})
            conn.execute(text(insert), {"id": "a4", "t": "t1", "s": None})  # NULLs never collide
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(insert), {"id": "a5", "t": "t1", "s": "helper"})
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0025_architect")
        with engine.connect() as conn:
            assert not _has_slug(conn, "agents") and not _has_slug(conn, "workflows")
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
