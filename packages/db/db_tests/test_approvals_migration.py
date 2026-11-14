"""0014_approvals: upgrade, downgrade and upgrade on a populated 0013 schema (a throwaway schema).
The backfill opens one `step` row per undecided workflow approval (a child run's names its workflow
run), decided waits get none, the checks hold, the tenant FKs cascade, and the result matches the
models."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

DEADLINE = "2030-01-02T03:04:05+00:00"


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _populate_0013(conn) -> None:
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, workflow_id, workflow_version, status, input, created_at, updated_at)"
        " VALUES ('wf1', 't1', 'workflow', 'wfdef', 1, 'running', '{}', now(), now()),"
        " ('wf2', 't1', 'workflow', 'wfdef', 1, 'running', '{}', now(), now()),"
        " ('wf9', 't2', 'workflow', 'wfdef9', 1, 'running', '{}', now(), now())"
    ))
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, parent_run_id,"
        " parent_step_id, created_at, updated_at)"
        " VALUES ('child1', 't1', 'agent', 'ag', 1, 'running', 'hi', 'wf1', 'agent_step', now(), now())"
    ))
    events = [
        # wf1: one open step, one decided step.
        ("t1", "wf1", 0, "step:open:waiting", "step_waiting",
         '{"step_id": "open", "prompt": "Ship?", "timeout_hours": 1, "workflow_id": "wf1", "deadline": "%s"}' % DEADLINE),
        ("t1", "wf1", 1, "step:done:waiting", "step_waiting",
         '{"step_id": "done", "prompt": "Old", "deadline": "%s"}' % DEADLINE),
        ("t1", "wf1", 2, "step:done:decision", "step_approved", '{"step_id": "done", "decided_by": "u"}'),
        # wf2: an open step without a recorded deadline (24 h after the event).
        ("t1", "wf2", 0, "step:nodl:waiting", "step_waiting", '{"step_id": "nodl"}'),
        # A deadline that is not ISO 8601 text: 24 h after the event, never a failed cast.
        ("t1", "wf2", 2, "step:bad:waiting", "step_waiting", '{"step_id": "bad", "deadline": "tomorrow"}'),
        # A step id containing colons.
        ("t1", "wf2", 1, "step:a:b:waiting", "step_waiting", '{"step_id": "a:b", "deadline": "%s"}' % DEADLINE),
        # A wait on a child agent run: root_run_id and workflow_id come from its parent workflow run.
        ("t1", "child1", 0, "step:c:waiting", "step_waiting", '{"step_id": "c", "deadline": "%s"}' % DEADLINE),
        # Another tenant's open step.
        ("t2", "wf9", 0, "step:x:waiting", "step_waiting", '{"step_id": "x", "deadline": "%s"}' % DEADLINE),
    ]
    for tenant, run, seq, key, typ, data in events:
        conn.execute(text(
            "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
            " VALUES (:t, :r, :s, :k, :ty, CAST(:d AS jsonb), '2029-12-31T00:00:00+00:00')"
        ), {"t": tenant, "r": run, "s": seq, "k": key, "ty": typ, "d": data})


def _rows(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute(text(
        "SELECT tenant_id, run_id, kind, approval_key, root_run_id, agent_id, workflow_id, prompt,"
        " to_char(deadline AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), decided_at IS NULL"
        " FROM pending_approvals ORDER BY tenant_id, run_id, approval_key"
    ))]


EXPECTED = [
    ("t1", "child1", "step", "c", "wf1", "ag", "wfdef", None, "2030-01-02 03:04:05", True),
    ("t1", "wf1", "step", "open", "wf1", None, "wfdef", "Ship?", "2030-01-02 03:04:05", True),
    ("t1", "wf2", "step", "a:b", "wf2", None, "wfdef", None, "2030-01-02 03:04:05", True),
    ("t1", "wf2", "step", "bad", "wf2", None, "wfdef", None, "2030-01-01 00:00:00", True),
    # No recorded deadline: 24 h after the event.
    ("t1", "wf2", "step", "nodl", "wf2", None, "wfdef", None, "2030-01-01 00:00:00", True),
    ("t2", "wf9", "step", "x", "wf9", None, "wfdef9", None, "2030-01-02 03:04:05", True),
]


def test_approvals_revision_round_trip_on_a_populated_0013_schema():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0014_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0013_semantic_recall")
            _populate_0013(conn)
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0014_approvals")
        with engine.connect() as conn:
            assert _rows(conn) == EXPECTED
            assert conn.execute(text("SELECT count(*) FROM runs WHERE output_format IS NOT NULL")).scalar_one() == 0
            # Added NOT VALID (no scan of runs under its lock); new writes are still checked below.
            assert conn.execute(text(
                "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_runs_output_format'"
                " AND conrelid = to_regclass('runs')"
            )).scalar_one() is False
            assert schema_diff(conn) == []

        # Checks.
        bad = [
            ("UPDATE runs SET output_format = 'xml' WHERE id = 'wf1'", "ck_runs_output_format"),
            ("INSERT INTO pending_approvals (tenant_id, run_id, kind, approval_key, root_run_id, deadline)"
             " VALUES ('t1', 'wf1', 'other', 'k', 'wf1', now())", "ck_pending_approvals_kind"),
            ("INSERT INTO pending_approvals (tenant_id, run_id, kind, approval_key, root_run_id, deadline)"
             " VALUES ('t1', 'child1', 'tool', 'k', 'wf1', now())", "ck_pending_approvals_tool_name"),
            ("UPDATE pending_approvals SET decision = 'approved' WHERE approval_key = 'open'",
             "ck_pending_approvals_decided"),
            ("UPDATE pending_approvals SET decision = 'maybe', decided_at = now() WHERE approval_key = 'open'",
             "ck_pending_approvals_decision"),
            # Another tenant's run cannot be named (composite FK).
            ("INSERT INTO pending_approvals (tenant_id, run_id, kind, approval_key, root_run_id, deadline)"
             " VALUES ('t2', 'wf1', 'step', 'k', 'wf1', now())", "fk_pending_approvals_tenant_run"),
        ]
        for sql, constraint in bad:
            with pytest.raises(IntegrityError, match=constraint):
                with engine.begin() as conn:
                    conn.execute(text(sql))
        with engine.begin() as conn:
            conn.execute(text("UPDATE runs SET output_format = 'json' WHERE id = 'child1'"))

        # Cascade: deleting a workflow run deletes its own and its children's rows.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM execution_events WHERE run_id IN ('wf1', 'child1')"))
            conn.execute(text("DELETE FROM runs WHERE id = 'child1'"))
            conn.execute(text("DELETE FROM runs WHERE id = 'wf1'"))
        with engine.connect() as conn:
            assert [r[1] for r in _rows(conn)] == ["wf2", "wf2", "wf2", "wf9"]

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0013_semantic_recall")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('pending_approvals')")).scalar_one() is None
            cols = set(conn.execute(text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'runs'"
            )).scalars())
            assert "output_format" not in cols
            assert conn.execute(text("SELECT count(*) FROM execution_events")).scalar_one() == 4  # wf2 and wf9 kept

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert [r[3] for r in _rows(conn)] == ["a:b", "bad", "nodl", "x"]
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)
