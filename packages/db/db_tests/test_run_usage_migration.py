"""0008_run_usage: existing runs unchanged (usage NULL, not overridden), tenant FK, clean round trip."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

requires_schema = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

_USAGE_INSERT = (
    "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
    " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at)"
    " VALUES (:tenant, 'a1', :key, 'test:ticket', 10, 5, 0, 0, true, 0.0000600000, 'override', now())"
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
def test_run_usage_revision_round_trip():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0008_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0007_workflow_runs")
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, created_at, updated_at)"
                " VALUES ('a1', 't1', 'agent', 'ag', 1, 'completed', 'hi', now(), now())"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0008_run_usage")
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT model_id, model_overridden, input_tokens, output_tokens, cost_usd, started_at, finished_at"
                " FROM runs WHERE id = 'a1'"
            )).one()
            assert tuple(row) == (None, False, None, None, None, None, None)
            # No schema_diff here: the models are at head (0009+); the round trip ends at head and checks it there.
            indexes = set(conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            )).scalars())
            assert {
                "ix_run_model_requests_tenant_created",
                "ix_run_model_requests_tenant_model_created",
                "ix_execution_events_tenant_type_ts",
                "run_model_requests_pkey",
            } <= indexes

        with engine.begin() as conn:
            conn.execute(text(_USAGE_INSERT), {"tenant": "t1", "key": "model:0"})
        with pytest.raises(IntegrityError, match="run_model_requests_pkey"):
            with engine.begin() as conn:
                conn.execute(text(_USAGE_INSERT), {"tenant": "t1", "key": "model:0"})
        with pytest.raises(IntegrityError, match="fk_run_model_requests_tenant_run"):
            with engine.begin() as conn:  # another tenant's run
                conn.execute(text(_USAGE_INSERT), {"tenant": "t2", "key": "model:1"})

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0007_workflow_runs")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('run_model_requests')")).scalar() is None
            assert not {"model_id", "input_tokens", "cost_usd", "started_at", "finished_at"} & _columns(conn, "runs")
            assert conn.execute(text("SELECT id FROM runs")).scalars().all() == ["a1"]

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)
