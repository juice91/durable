"""0027_tenant_secrets: the per-tenant encrypted secret table."""

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


def _exists(conn) -> bool:
    return bool(conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'tenant_secrets' AND table_schema = current_schema()"
    )).first())


def test_upgrade_creates_the_table_keyed_by_tenant_and_name_and_downgrade_drops_it():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0027_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0027_tenant_secrets")
        insert = (
            "INSERT INTO tenant_secrets (tenant_id, name, ciphertext, key_id, created_by) "
            "VALUES (:t, :n, :c, 'k', 'alice')"
        )
        with engine.begin() as conn:
            assert _exists(conn)
            conn.execute(text(insert), {"t": "t1", "n": "slack-token", "c": b"\x00" * 20})
            conn.execute(text(insert), {"t": "t2", "n": "slack-token", "c": b"\x00" * 20})  # another tenant: fine
            row = conn.execute(text("SELECT created_at, updated_at FROM tenant_secrets WHERE tenant_id = 't1'")).one()
            assert row[0] is not None and row[1] is not None
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(insert), {"t": "t1", "n": "slack-token", "c": b"\x00" * 20})
        for bad in ["Upper", "1abc", "a b", "", "x" * 65]:
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(text(insert), {"t": "t3", "n": bad, "c": b"\x00" * 20})
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0026_architect_slugs")
        with engine.begin() as conn:
            assert not _exists(conn)
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
