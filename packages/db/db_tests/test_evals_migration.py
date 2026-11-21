"""0015_evals: upgrade, downgrade and upgrade on a populated 0014 schema (a throwaway schema).
Existing runs of every kind satisfy the new kind check, the eval tables' checks and tenant FKs hold,
suites cascade to cases and runs to results, the schema matches the models, and the downgrade
removes eval runs (detaching their child runs) and restores the 0014 check."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

SUITE = "11111111-1111-1111-1111-111111111111"
CASE = "22222222-2222-2222-2222-222222222222"
KB = "33333333-3333-3333-3333-333333333333"


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _populate_0014(conn) -> None:
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, created_at, updated_at)"
        " VALUES ('a1', 't1', 'agent', 'ag', 1, 'completed', 'hi', now(), now())"
    ))
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, agent_id, draft_spec, status, input, created_at, updated_at)"
        " VALUES ('a2', 't1', 'agent', 'ag', '{}'::jsonb, 'completed', 'hi', now(), now())"
    ))
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, tool_id, status, input, created_at, updated_at)"
        " VALUES ('tl', 't1', 'tool', 'calc', 'completed', '{}', now(), now())"
    ))
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, workflow_id, workflow_version, status, input, created_at, updated_at)"
        " VALUES ('wf', 't1', 'workflow', 'wfd', 1, 'completed', '{}', now(), now())"
    ))
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, knowledge_base_id, status, input, created_at, updated_at)"
        " VALUES ('kb', 't1', 'kb_ingest', :kb, 'completed', '{}', now(), now())"
    ), {"kb": KB})
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, parent_run_id,"
        " parent_step_id, created_at, updated_at)"
        " VALUES ('wchild', 't1', 'agent', 'ag', 1, 'completed', 'hi', 'wf', 's', now(), now())"
    ))


def _insert_eval_run(conn, run_id: str = "ev1", tenant: str = "t1") -> None:
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, eval_suite_id, status, input,"
        " created_at, updated_at)"
        " VALUES (:r, :t, 'eval', 'ag', 1, :s, 'running', '{}', now(), now())"
    ), {"r": run_id, "t": tenant, "s": SUITE})


def test_evals_revision_round_trip_on_a_populated_0014_schema():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0015_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0014_approvals")
            _populate_0014(conn)
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0015_evals")
        with engine.connect() as conn:
            # The models are compared at head only (at the end): a later revision changes them.
            assert conn.execute(text("SELECT count(*) FROM runs WHERE eval_suite_id IS NOT NULL")).scalar_one() == 0
            # Added NOT VALID (no scan under the runs lock)...
            assert conn.execute(text(
                "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_runs_kind_target'"
                " AND conrelid = to_regclass('runs')"
            )).scalar_one() is False
        # ... and every existing run of every kind satisfies it.
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE runs VALIDATE CONSTRAINT ck_runs_kind_target"))

        # A suite, a case, an eval run (published and draft), a child, a result, an event, a usage row.
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO eval_suites (id, tenant_id, agent_id, name) VALUES (:s, 't1', 'ag', 'smoke')"
            ), {"s": SUITE})
            conn.execute(text(
                "INSERT INTO eval_cases (id, tenant_id, suite_id, name, input, position)"
                " VALUES (:c, 't1', :s, 'c1', 'hi', 0)"
            ), {"c": CASE, "s": SUITE})
            _insert_eval_run(conn)
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, draft_spec, eval_suite_id, status, input,"
                " created_at, updated_at)"
                " VALUES ('ev2', 't1', 'eval', 'ag', '{}'::jsonb, :s, 'queued', '{}', now(), now())"
            ), {"s": SUITE})
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, parent_run_id,"
                " parent_step_id, created_at, updated_at)"
                " VALUES ('echild', 't1', 'agent', 'ag', 1, 'completed', 'hi', 'ev1', 'case', now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO eval_case_results (tenant_id, run_id, case_id, case_name, position, status)"
                " VALUES ('t1', 'ev1', :c, 'c1', 0, 'passed')"
            ), {"c": CASE})
            conn.execute(text(
                "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                " VALUES ('t1', 'ev1', 0, 'case:x', 'eval_case_result', '{}'::jsonb, now()),"
                " ('t1', 'echild', 0, 'start', 'start', '{}'::jsonb, now())"
            ))
            conn.execute(text(
                "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, created_at)"
                " VALUES ('t1', 'ev1', 'judge:x:0', 'test:judge', 1, 1, 0, 0, true, now()),"
                " ('t1', 'echild', 'model:0', 'test:echo', 1, 1, 0, 0, true, now())"
            ))
            conn.execute(text("INSERT INTO side_effects (tenant_id, key, state, created_at) VALUES ('t1', 'ev1:k', 'completed', now())"))

        bad = [
            # Kind check: an eval run needs its suite, exactly one of version and draft, no tool.
            ("INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, created_at, updated_at)"
             " VALUES ('b1', 't1', 'eval', 'ag', 1, 'queued', '{}', now(), now())", "ck_runs_kind_target"),
            ("INSERT INTO runs (id, tenant_id, kind, agent_id, eval_suite_id, status, input, created_at, updated_at)"
             f" VALUES ('b2', 't1', 'eval', 'ag', '{SUITE}', 'queued', '{{}}', now(), now())", "ck_runs_kind_target"),
            ("INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, eval_suite_id, tool_id, status, input,"
             " created_at, updated_at)"
             f" VALUES ('b3', 't1', 'eval', 'ag', 1, '{SUITE}', 'calc', 'queued', '{{}}', now(), now())", "ck_runs_kind_target"),
            ("INSERT INTO eval_suites (id, tenant_id, agent_id, name)"
             f" VALUES (gen_random_uuid(), 't1', 'ag', 'smoke')", "uq_eval_suites_tenant_agent_name"),
            ("INSERT INTO eval_suites (id, tenant_id, agent_id, name, evaluators)"
             " VALUES (gen_random_uuid(), 't1', 'ag', 'x', '{}'::jsonb)", "ck_eval_suites_evaluators"),
            ("INSERT INTO eval_cases (id, tenant_id, suite_id, name, input, position)"
             f" VALUES (gen_random_uuid(), 't1', '{SUITE}', 'c1', 'x', 1)", "uq_eval_cases_suite_name"),
            ("INSERT INTO eval_cases (id, tenant_id, suite_id, name, input, position)"
             f" VALUES (gen_random_uuid(), 't1', '{SUITE}', 'c2', repeat('x', 32769), 1)", "ck_eval_cases_input_size"),
            ("INSERT INTO eval_cases (id, tenant_id, suite_id, name, input, position)"
             f" VALUES (gen_random_uuid(), 't1', '{SUITE}', 'c3', 'x', -1)", "ck_eval_cases_position"),
            ("INSERT INTO eval_cases (id, tenant_id, suite_id, name, input, position, metadata)"
             f" VALUES (gen_random_uuid(), 't1', '{SUITE}', 'c4', 'x', 1, '[]'::jsonb)", "ck_eval_cases_metadata"),
            # Another tenant's suite cannot be named (composite FK).
            ("INSERT INTO eval_cases (id, tenant_id, suite_id, name, input, position)"
             f" VALUES (gen_random_uuid(), 't2', '{SUITE}', 'c5', 'x', 1)", "fk_eval_cases_tenant_suite"),
            ("INSERT INTO eval_case_results (tenant_id, run_id, case_id, case_name, position, status)"
             " VALUES ('t1', 'ev1', gen_random_uuid(), 'c', 0, 'skipped')", "ck_eval_case_results_status"),
            ("INSERT INTO eval_case_results (tenant_id, run_id, case_id, case_name, position, status)"
             " VALUES ('t1', 'ev1', gen_random_uuid(), 'c', 0, 'error')", "ck_eval_case_results_error"),
            ("INSERT INTO eval_case_results (tenant_id, run_id, case_id, case_name, position, status, error_code)"
             " VALUES ('t1', 'ev1', gen_random_uuid(), 'c', 0, 'passed', 'x')", "ck_eval_case_results_error"),
            ("INSERT INTO eval_case_results (tenant_id, run_id, case_id, case_name, position, status, scores)"
             " VALUES ('t1', 'ev1', gen_random_uuid(), 'c', 0, 'passed', '[]'::jsonb)", "ck_eval_case_results_json"),
            ("INSERT INTO eval_case_results (tenant_id, run_id, case_id, case_name, position, status)"
             " VALUES ('t2', 'ev1', gen_random_uuid(), 'c', 0, 'passed')", "fk_eval_case_results_tenant_run"),
        ]
        for sql, constraint in bad:
            with pytest.raises(IntegrityError, match=constraint):
                with engine.begin() as conn:
                    conn.execute(text(sql))

        # Cascades: suite -> cases; run -> results.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM eval_suites WHERE id = :s"), {"s": SUITE})
            assert conn.execute(text("SELECT count(*) FROM eval_cases")).scalar_one() == 0
            _insert_eval_run(conn, "ev3")
            conn.execute(text(
                "INSERT INTO eval_case_results (tenant_id, run_id, case_id, case_name, position, status)"
                " VALUES ('t1', 'ev3', :c, 'c1', 0, 'failed')"
            ), {"c": CASE})
            conn.execute(text("DELETE FROM runs WHERE id = 'ev3'"))
            assert conn.execute(text("SELECT count(*) FROM eval_case_results WHERE run_id = 'ev3'")).scalar_one() == 0

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0014_approvals")
        with engine.connect() as conn:
            for table in ("eval_suites", "eval_cases", "eval_case_results"):
                assert conn.execute(text(f"SELECT to_regclass('{table}')")).scalar_one() is None
            cols = set(conn.execute(text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'runs'"
            )).scalars())
            assert "eval_suite_id" not in cols
            runs = dict(conn.execute(text("SELECT id, parent_run_id FROM runs")).all())
            # Eval runs are gone; their child is a standalone agent run with its own rows.
            assert runs == {"a1": None, "a2": None, "tl": None, "wf": None, "kb": None, "wchild": "wf", "echild": None}
            assert conn.execute(text("SELECT parent_step_id FROM runs WHERE id = 'echild'")).scalar_one() is None
            assert conn.execute(text("SELECT run_id FROM execution_events")).scalars().all() == ["echild"]
            assert conn.execute(text("SELECT run_id FROM run_model_requests")).scalars().all() == ["echild"]
            assert conn.execute(text("SELECT count(*) FROM side_effects")).scalar_one() == 0
            assert "'eval'" not in conn.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_runs_kind_target'"
                " AND conrelid = to_regclass('runs')"
            )).scalar_one()

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
            assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 7
    finally:
        engine.dispose()
        drop_schema(base, schema)
