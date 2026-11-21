"""0007_workflow_runs: existing runs unchanged, kind/target and parent-link checks, clean round trip."""

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

_INSERT = (
    "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, tool_id, workflow_id, workflow_version,"
    " parent_run_id, parent_step_id, conversation_id, turn, status, input, created_at, updated_at)"
    " VALUES (:id, :tenant, :kind, :agent_id, :agent_version, :tool_id, :workflow_id, :workflow_version,"
    " :parent_run_id, :parent_step_id, NULL, NULL, 'queued', '{}', now(), now())"
)
_BLANK = {"tenant": "t1", "agent_id": None, "agent_version": None, "tool_id": None, "workflow_id": None,
          "workflow_version": None, "parent_run_id": None, "parent_step_id": None}


def _base_url() -> str:
    from sqlalchemy.engine import make_url

    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


@requires_schema
def test_workflow_runs_revision_round_trip():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0007_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0006_tool_runs")
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, tool_id, status, input, created_at,"
                " updated_at) VALUES ('a1', 't1', 'agent', 'ag', 1, NULL, 'completed', 'hi', now(), now()),"
                " ('tl', 't1', 'tool', NULL, NULL, 'clock.now', 'completed', '{}', now(), now())"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0007_workflow_runs")
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, kind, workflow_id, parent_run_id FROM runs ORDER BY id")).all()
            assert [tuple(r) for r in rows] == [("a1", "agent", None, None), ("tl", "tool", None, None)]
            # No schema_diff here: the models are at head (0008+); the round trip ends at head and checks it there.

        wf = {**_BLANK, "id": "w1", "kind": "workflow", "workflow_id": "wf", "workflow_version": 1}
        child = {**_BLANK, "id": "c1", "kind": "agent", "agent_id": "ag", "agent_version": 1,
                 "parent_run_id": "w1", "parent_step_id": "ask"}
        with engine.begin() as conn:
            conn.execute(text(_INSERT), wf)
            conn.execute(text(_INSERT), child)
        bad = [
            ({**wf, "id": "b1", "workflow_version": None}, "ck_runs_kind_target"),
            ({**wf, "id": "b2", "agent_id": "ag"}, "ck_runs_kind_target"),
            ({**_BLANK, "id": "b3", "kind": "agent", "agent_id": "ag", "agent_version": 1, "workflow_id": "wf"},
             "ck_runs_kind_target"),
            ({**child, "id": "b4", "parent_step_id": None}, "ck_runs_parent_link"),
            ({**wf, "id": "b5", "parent_run_id": "w1", "parent_step_id": "x"}, "ck_runs_parent_link"),
            ({**child, "id": "b6", "tenant": "t2"}, "fk_runs_tenant_parent_run"),  # another tenant's parent
        ]
        for values, name in bad:
            with pytest.raises(IntegrityError, match=name):
                with engine.begin() as conn:
                    conn.execute(text(_INSERT), values)

        with engine.begin() as conn:
            for tenant, key in [("t1", "w1:tool_step"), ("t1", "c1:call-1"), ("t2", "w1:tool_step"), ("t1", "a1:call-1")]:
                conn.execute(
                    text("INSERT INTO side_effects (tenant_id, key, state, created_at) VALUES (:t, :k, 'completed', now())"),
                    {"t": tenant, "k": key},
                )
            for run_id in ("w1", "c1", "a1"):
                conn.execute(text(
                    "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                    " VALUES ('t1', :r, 0, 'start', 'run_started', '{}', now())"
                ), {"r": run_id})

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0006_tool_runs")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT id FROM runs ORDER BY id")).scalars().all() == ["a1", "tl"]
            assert conn.execute(text("SELECT run_id FROM execution_events")).scalars().all() == ["a1"]
            remaining = conn.execute(text("SELECT tenant_id, key FROM side_effects ORDER BY tenant_id, key")).all()
            assert [tuple(r) for r in remaining] == [("t1", "a1:call-1"), ("t2", "w1:tool_step")]
            columns = conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'runs'"
                " AND table_schema = current_schema()"
            )).scalars().all()
            assert "workflow_id" not in columns and "parent_run_id" not in columns

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)
