"""0016_ag_ui: upgrade, downgrade and upgrade on a populated 0015 schema (a throwaway schema).
Existing rows read the new columns as NULL, the checks are added NOT VALID, the new table's checks
and tenant FK hold and cascade, the external thread index is unique per tenant and agent, the
downgrade keeps every run and conversation, and the result matches the models."""

from __future__ import annotations

import hashlib
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

FP = hashlib.sha256(b"fp").hexdigest()


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _populate_0015(conn) -> None:
    conn.execute(text(
        "INSERT INTO conversations (id, tenant_id, agent_id, created_at, updated_at)"
        " VALUES ('c1', 't1', 'ag', now(), now()), ('c2', 't2', 'ag', now(), now())"
    ))
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, conversation_id, turn,"
        " created_at, updated_at)"
        " VALUES ('r1', 't1', 'agent', 'ag', 1, 'completed', 'hi', 'c1', 1, now(), now()),"
        " ('r2', 't2', 'agent', 'ag', 1, 'completed', 'hi', 'c2', 1, now(), now())"
    ))
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, workflow_id, workflow_version, status, input, created_at, updated_at)"
        " VALUES ('wf1', 't1', 'workflow', 'wf', 1, 'running', '{}', now(), now())"
    ))


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = :t"
    ), {"t": table}).scalars())


def _insert_ag_ui(conn, tenant, run, external_run="x", scope_kind="agent", from_seq=-1, fp=FP, ids="[]"):
    conn.execute(text(
        "INSERT INTO ag_ui_runs (tenant_id, scope_kind, scope_id, external_thread_id, external_run_id, run_id,"
        " from_seq, fingerprint, input_message_ids)"
        " VALUES (:t, :k, 'ag', 'th', :er, :r, :s, :f, CAST(:m AS jsonb))"
    ), {"t": tenant, "k": scope_kind, "er": external_run, "r": run, "s": from_seq, "f": fp, "m": ids})


def test_ag_ui_revision_round_trip_on_a_populated_0015_schema():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0016_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0015_evals")
            _populate_0015(conn)
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0016_ag_ui")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0016_ag_ui"
            # Old rows read the new columns as NULL.
            assert conn.execute(text(
                "SELECT count(*) FROM runs WHERE client_tools IS NOT NULL OR client_tool_results IS NOT NULL"
            )).scalar_one() == 0
            assert conn.execute(text(
                "SELECT count(*) FROM conversations WHERE external_thread_id IS NOT NULL"
            )).scalar_one() == 0
            # The commit order: old events have no root_order (NULL), every run's counter starts at 0.
            assert conn.execute(text("SELECT count(*) FROM execution_events WHERE root_order IS NOT NULL")).scalar_one() == 0
            assert conn.execute(text("SELECT DISTINCT event_order_next FROM runs")).scalars().all() == [0]
            assert {"from_order", "end_order"} <= _columns(conn, "ag_ui_runs")
            assert conn.execute(text("SELECT to_regclass('ix_execution_events_run_root_order')")).scalar_one() is not None
            assert conn.execute(text(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_execution_events_root_order'"
                " AND tgrelid = to_regclass('execution_events')"
            )).scalar_one() == 1
            # The runs checks are added NOT VALID (no scan of runs under its lock).
            validated = dict(conn.execute(text(
                "SELECT conname, convalidated FROM pg_constraint"
                " WHERE conrelid = to_regclass('runs') AND conname IN ('ck_runs_client_tools', 'ck_runs_client_tool_results')"
            )).all())
            assert validated == {"ck_runs_client_tools": False, "ck_runs_client_tool_results": False}
            assert conn.execute(text("SELECT count(*) FROM ag_ui_runs")).scalar_one() == 0

        # The new table: rows, the same external ids per tenant, checks and the tenant FK.
        with engine.begin() as conn:
            _insert_ag_ui(conn, "t1", "r1", ids='["u1"]')
            _insert_ag_ui(conn, "t2", "r2")
            _insert_ag_ui(conn, "t1", "wf1", scope_kind="workflow", from_seq=4)
            conn.execute(text(
                "UPDATE runs SET client_tools = '[{\"name\": \"pick\"}]'::jsonb,"
                " client_tool_results = '[]'::jsonb WHERE id = 'r1'"
            ))
            conn.execute(text("UPDATE conversations SET external_thread_id = 'th' WHERE id = 'c1'"))
            conn.execute(text(
                "INSERT INTO conversations (id, tenant_id, agent_id, external_thread_id, created_at, updated_at)"
                " VALUES ('c3', 't1', 'other', 'th', now(), now()), ('c4', 't2', 'ag', 'th', now(), now())"
            ))
        bad = [
            (lambda c: _insert_ag_ui(c, "t1", "r1"), "ag_ui_runs_pkey"),
            (lambda c: _insert_ag_ui(c, "t1", "r1", "y", scope_kind="tool"), "ck_ag_ui_runs_scope_kind"),
            (lambda c: _insert_ag_ui(c, "t1", "r1", "y", from_seq=-2), "ck_ag_ui_runs_from_seq"),
            (lambda c: _insert_ag_ui(c, "t1", "r1", "y", fp="nope"), "ck_ag_ui_runs_fingerprint"),
            (lambda c: _insert_ag_ui(c, "t1", "r1", "y", ids="{}"), "ck_ag_ui_runs_input_message_ids"),
            # Another tenant's run cannot be named (composite FK).
            (lambda c: _insert_ag_ui(c, "t1", "r2", "y"), "fk_ag_ui_runs_tenant_run"),
            (lambda c: c.execute(text("UPDATE runs SET client_tools = '{}'::jsonb WHERE id = 'r2'")),
             "ck_runs_client_tools"),
            (lambda c: c.execute(text("UPDATE runs SET client_tool_results = '1'::jsonb WHERE id = 'r2'")),
             "ck_runs_client_tool_results"),
            (lambda c: c.execute(text(
                "INSERT INTO conversations (id, tenant_id, agent_id, external_thread_id, created_at, updated_at)"
                " VALUES ('c5', 't1', 'ag', 'th', now(), now())"
            )), "uq_conversations_tenant_agent_external_thread"),
        ]
        for write, constraint in bad:
            with pytest.raises(IntegrityError, match=constraint):
                with engine.begin() as conn:
                    write(conn)

        # Cascade: deleting a run deletes its AG-UI rows.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM runs WHERE id = 'wf1'"))
        with engine.connect() as conn:
            assert sorted(conn.execute(text("SELECT tenant_id, run_id FROM ag_ui_runs")).all()) == [
                ("t1", "r1"),
                ("t2", "r2"),
            ]

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0015_evals")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('ag_ui_runs')")).scalar_one() is None
            assert not {"client_tools", "client_tool_results", "event_order_next"} & _columns(conn, "runs")
            assert "root_order" not in _columns(conn, "execution_events")
            assert conn.execute(text(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_execution_events_root_order'"
                " AND tgrelid = to_regclass('execution_events')"
            )).scalar_one() == 0
            assert conn.execute(text(
                "SELECT count(*) FROM pg_proc WHERE proname = 'execution_events_root_order'"
                " AND pronamespace = current_schema()::regnamespace"
            )).scalar_one() == 0
            assert "external_thread_id" not in _columns(conn, "conversations")
            assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 2
            assert conn.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 4

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM ag_ui_runs")).scalar_one() == 0
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)
