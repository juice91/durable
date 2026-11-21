"""0031_workflow_archived_at: nullable archived_at, backfilled from updated_at for archived workflows."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _insert(conn, wid: str, archived: bool) -> None:
    conn.execute(
        text(
            "INSERT INTO workflows (id, tenant_id, name, archived, created_at, updated_at)"
            " VALUES (:i, 't1', :i, :a, '2026-01-01', '2026-02-02')"
        ),
        {"i": wid, "a": archived},
    )


def test_upgrade_backfills_archived_rows_and_downgrade_drops_the_column():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0031_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0030_kb_source_kinds")
            _insert(conn, "live", False)
            _insert(conn, "gone", True)
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0031_workflow_archived_at")
        with engine.begin() as conn:
            rows = {r[0]: r[1] for r in conn.execute(text("SELECT id, archived_at FROM workflows"))}
            assert rows["live"] is None
            assert rows["gone"] is not None and rows["gone"].year == 2026 and rows["gone"].month == 2
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0030_kb_source_kinds")
        with engine.begin() as conn:
            cols = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'workflows' AND table_schema = current_schema()"))}
            assert "archived_at" not in cols
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
