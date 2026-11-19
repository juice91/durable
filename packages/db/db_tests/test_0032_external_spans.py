"""0032_external_spans: source, trace_id and the attribute columns on spans, the partial indexes, and the downgrade."""

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


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _columns(conn) -> set[str]:
    return {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'spans' AND table_schema = current_schema()"))}


def _insert(conn, span_id: str, **extra) -> None:
    cols = {"tenant_id": "t1", "run_id": str(uuid.uuid4()), "span_id": span_id, "kind": "run", "name": "n",
            "start": "2026-01-01T00:00:00Z", "status": "ok", **extra}
    conn.execute(text(f"INSERT INTO spans ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"), cols)


def test_upgrade_keeps_platform_rows_and_downgrade_drops_external_ones():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0032_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0031_workflow_archived_at")
            _insert(conn, "old")
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0032_external_spans")
        with engine.begin() as conn:
            assert {"source", "trace_id", "service_name", "api_key_id", "attributes", "resource_attributes"} <= _columns(conn)
            assert conn.execute(text("SELECT source FROM spans WHERE span_id = 'old'")).scalar_one() == "platform"
            _insert(conn, "ext", source="external", trace_id="1" * 32)
            with pytest.raises(Exception):
                with conn.begin_nested():
                    _insert(conn, "bad", source="other")
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0031_workflow_archived_at")
        with engine.begin() as conn:
            assert "source" not in _columns(conn)
            assert [r[0] for r in conn.execute(text("SELECT span_id FROM spans"))] == ["old"]
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
