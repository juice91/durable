"""0006_tool_runs: existing runs become agent runs unchanged; the kind/target check holds; round trip."""

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

_INSERT_RUN = (
    "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, tool_id, status, input, conversation_id, turn,"
    " created_at, updated_at) VALUES (:id, 't1', :kind, :agent_id, :agent_version, :tool_id, 'queued', '{}',"
    " :conversation_id, :turn, now(), now())"
)


def _base_url() -> str:
    from sqlalchemy.engine import make_url

    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


@requires_schema
def test_tool_runs_revision_keeps_agent_runs_and_enforces_kind_targets():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0006_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0005_workflows")
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, agent_id, agent_version, status, input, created_at, updated_at) "
                "VALUES ('r1', 't1', 'a1', 1, 'completed', 'hi', now(), now())"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0006_tool_runs")
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, kind, agent_id, agent_version, tool_id, input FROM runs")).all()
            assert [tuple(r) for r in rows] == [("r1", "agent", "a1", 1, None, "hi")]
            # The models are at head (0007_workflow_runs adds columns), so the schema/model diff is
            # checked after the final upgrade to head below.

        ok_tool = {"id": "r2", "kind": "tool", "agent_id": None, "agent_version": None, "tool_id": "clock.now",
                   "conversation_id": None, "turn": None}
        with engine.begin() as conn:
            conn.execute(text(_INSERT_RUN), ok_tool)
        bad = [
            {**ok_tool, "id": "b1", "tool_id": None},  # tool run without a tool
            {**ok_tool, "id": "b2", "agent_id": "a1"},  # tool run with an agent
            {**ok_tool, "id": "b3", "kind": "agent"},  # agent run without an agent version
            {**ok_tool, "id": "b4", "kind": "workflow"},  # unknown kind
        ]
        for values in bad:
            with pytest.raises(IntegrityError, match="ck_runs_kind_target"):
                with engine.begin() as conn:
                    conn.execute(text(_INSERT_RUN), values)

        # Side-effect reservations: the tool run's two keys, the same keys under another tenant,
        # and an agent run's own key.
        with engine.begin() as conn:
            for tenant, key in [("t1", "r2:tool"), ("t1", "r2:tool_run"), ("t2", "r2:tool"), ("t1", "r1:call-1")]:
                conn.execute(
                    text("INSERT INTO side_effects (tenant_id, key, state, created_at) VALUES (:t, :k, 'completed', now())"),
                    {"t": tenant, "k": key},
                )
            conn.execute(text(
                "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts) "
                "VALUES ('t1', 'r2', 0, 'start', 'run_started', '{}', now())"
            ))

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0005_workflows")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT id FROM runs")).scalars().all() == ["r1"]
            assert conn.execute(text("SELECT count(*) FROM execution_events")).scalar_one() == 0
            remaining = conn.execute(text("SELECT tenant_id, key FROM side_effects ORDER BY tenant_id, key")).all()
            assert [tuple(r) for r in remaining] == [("t1", "r1:call-1"), ("t2", "r2:tool")]
            indexes = conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'runs' AND schemaname = current_schema()")).scalars().all()
            assert "ix_runs_tenant_kind_created" not in indexes

        # Re-upgrade: models and schema agree again, and the old client run_id starts clean.
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0006_tool_runs")
        with engine.connect() as conn:
            indexes = set(conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'runs' AND schemaname = current_schema()")).scalars())
            assert {"ix_runs_tenant_kind_created", "ix_runs_tenant_tool_created"} <= indexes
        with engine.begin() as conn:
            conn.execute(text(_INSERT_RUN), ok_tool)
            assert conn.execute(
                text("SELECT count(*) FROM side_effects WHERE tenant_id = 't1' AND key LIKE 'r2:%'")
            ).scalar_one() == 0
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)
