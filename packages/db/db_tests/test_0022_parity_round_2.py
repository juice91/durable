"""0022_parity_round_2: upgrade, constraint behaviour, downgrade and re-upgrade on a throwaway schema.

Adds runs.rerun_of / schedule_id / pause_after_each_step, workflow_schedules, spans, eval_suites
workflow targets and the widened ck_runs_kind_target (an eval run may target a workflow).
"""

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

SUITE_A = "11111111-1111-1111-1111-111111111111"
SUITE_W = "22222222-2222-2222-2222-222222222222"
RUN_SQL = (
    "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, workflow_id, workflow_version,"
    " eval_suite_id, status, input, created_at, updated_at)"
    " VALUES (:id, 't1', :kind, :agent, :agent_version, :wf, :wf_version, :suite, 'completed', '{}', now(), now())"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t AND table_schema = current_schema()"
    ), {"t": table}).scalars())


def _tables(conn) -> set[str]:
    return set(conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )).scalars())


def _constraints(conn, table: str, kind: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND contype = :k"
    ), {"t": table, "k": kind}).scalars())


def _indexes(conn, table: str) -> dict[str, str]:
    return dict(conn.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t AND schemaname = current_schema()"
    ), {"t": table}).all())


def _run(conn, run_id, kind, agent=None, agent_version=None, wf=None, wf_version=None, suite=None):
    conn.execute(text(RUN_SQL), {
        "id": run_id, "kind": kind, "agent": agent, "agent_version": agent_version,
        "wf": wf, "wf_version": wf_version, "suite": suite,
    })


def _suite(conn, suite_id, agent, wf, name):
    conn.execute(text(
        "INSERT INTO eval_suites (id, tenant_id, agent_id, workflow_id, name) VALUES (:i, 't1', :a, :w, :n)"
    ), {"i": suite_id, "a": agent, "w": wf, "n": name})


def test_upgrade_adds_columns_tables_constraints_and_round_trips():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0022_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0021_chat_attachments")
            _run(conn, "old", "agent", agent="ag", agent_version=1)
            conn.execute(text(
                "INSERT INTO eval_suites (id, tenant_id, agent_id, name) VALUES (:i, 't1', 'ag', 'smoke')"
            ), {"i": SUITE_A})
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0022_parity_round_2")
        with engine.connect() as conn:
            assert {"rerun_of", "schedule_id", "pause_after_each_step"} <= _columns(conn, "runs")
            assert conn.execute(text(
                "SELECT pause_after_each_step, rerun_of IS NULL, schedule_id IS NULL FROM runs WHERE id = 'old'"
            )).one() == (False, True, True)
            assert {"workflow_schedules", "spans"} <= _tables(conn)
            assert "workflow_id" in _columns(conn, "eval_suites")
            assert {"ck_eval_suites_one_target", "ck_runs_kind_target"} <= _constraints(conn, "eval_suites", "c") | _constraints(conn, "runs", "c")
            assert {"uq_eval_suites_tenant_agent_name", "uq_eval_suites_tenant_workflow_name"} <= _constraints(conn, "eval_suites", "u")
            assert "uq_workflow_schedules_tenant_workflow_name" in _constraints(conn, "workflow_schedules", "u")
            assert {"ix_runs_rerun_of", "ix_runs_schedule_id"} <= set(_indexes(conn, "runs"))
            assert "ix_workflow_schedules_active_next_fire" in _indexes(conn, "workflow_schedules")
            assert "(tenant_id, start DESC)" in _indexes(conn, "spans")["ix_spans_tenant_start"]
            assert _constraints(conn, "spans", "p") == {"spans_pkey"}
            assert schema_diff(conn) == []

        # An eval run may target a workflow; every other shape stays rejected.
        with engine.begin() as conn:
            _suite(conn, SUITE_W, None, "wf", "wfsuite")
            _run(conn, "evw", "eval", wf="wf", wf_version=1, suite=SUITE_W)
            _run(conn, "eva", "eval", agent="ag", agent_version=1, suite=SUITE_A)
        for bad in (
            dict(kind="eval", agent="ag", agent_version=1, wf="wf", wf_version=1, suite=SUITE_A),  # both targets
            dict(kind="eval", wf="wf", suite=SUITE_W),  # workflow without version
            dict(kind="eval", wf="wf", wf_version=1),  # no suite
            dict(kind="agent", wf="wf", wf_version=1),  # other kinds unchanged
        ):
            with pytest.raises(IntegrityError, match="ck_runs_kind_target"):
                with engine.begin() as conn:
                    _run(conn, f"bad{uuid.uuid4().hex[:4]}", **bad)

        # A suite targets exactly one of agent and workflow; names are unique per target.
        for agent, wf in ((None, None), ("ag", "wf")):
            with pytest.raises(IntegrityError, match="ck_eval_suites_one_target"):
                with engine.begin() as conn:
                    _suite(conn, str(uuid.uuid4()), agent, wf, "x")
        with pytest.raises(IntegrityError, match="uq_eval_suites_tenant_workflow_name"):
            with engine.begin() as conn:
                _suite(conn, str(uuid.uuid4()), None, "wf", "wfsuite")

        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO workflow_schedules (id, tenant_id, workflow_id, name, cron, next_fire_at)"
                " VALUES (:i, 't1', 'wf', 'nightly', '0 2 * * *', now())"
            ), {"i": str(uuid.uuid4())})
        with pytest.raises(IntegrityError, match="uq_workflow_schedules_tenant_workflow_name"):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO workflow_schedules (id, tenant_id, workflow_id, name, cron, next_fire_at)"
                    " VALUES (:i, 't1', 'wf', 'nightly', '0 3 * * *', now())"
                ), {"i": str(uuid.uuid4())})
        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT timezone, active, input, last_error FROM workflow_schedules"
            )).one() == ("UTC", True, {}, None)

        # The workflow eval run `evw` has a child run, events and model requests: the downgrade drops the
        # eval run with its events and requests, and keeps the child as a standalone run.
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, workflow_id, workflow_version, status, input, parent_run_id,"
                " parent_step_id, created_at, updated_at)"
                " VALUES ('evwc', 't1', 'workflow', 'wf', 1, 'completed', '{}', 'evw', 'case:c1', now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                " VALUES ('t1', 'evw', 1, 'start', 'run_started', '{}', now())"
            ))
            conn.execute(text(
                "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, created_at, operation)"
                " VALUES ('t1', 'evw', 'judge:c1:1', 'test:judge', 10, 5, 0, 0, true, now(), 'chat')"
            ))
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0021_chat_attachments")
        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT parent_run_id, parent_step_id FROM runs WHERE id = 'evwc'"
            )).one() == (None, None)
            # ck_runs_parent_link is 0007's again: a parented workflow run is refused.
            assert "'workflow'" not in conn.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'ck_runs_parent_link' AND conrelid = CAST('runs' AS regclass)"
            )).scalar_one()
            assert conn.execute(text("SELECT count(*) FROM execution_events WHERE run_id = 'evw'")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM run_model_requests WHERE run_id = 'evw'")).scalar() == 0
            assert {"workflow_schedules", "spans"}.isdisjoint(_tables(conn))
            assert {"rerun_of", "schedule_id", "pause_after_each_step"}.isdisjoint(_columns(conn, "runs"))
            assert "workflow_id" not in _columns(conn, "eval_suites")
            assert conn.execute(text("SELECT id FROM runs ORDER BY id")).scalars().all() == ["eva", "evwc", "old"]
            assert conn.execute(text("SELECT id::text FROM eval_suites")).scalars().all() == [SUITE_A]

        with pytest.raises(IntegrityError, match="ck_runs_parent_link"):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO runs (id, tenant_id, kind, workflow_id, workflow_version, status, input,"
                    " parent_run_id, parent_step_id, created_at, updated_at)"
                    " VALUES ('wchild', 't1', 'workflow', 'wf', 1, 'completed', '{}', 'old', 'x', now(), now())"
                ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)
