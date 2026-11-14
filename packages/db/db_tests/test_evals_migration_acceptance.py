"""AC.10 acceptance: 0015_evals on a populated 0014 schema (throwaway schemas only, never the
session schema). Up, down and up keep every non-eval row; the kind check accepts eval runs only with
a suite and exactly one of version/draft, and the other kinds' rules are unchanged; `alembic check`
is clean at head; one head."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from eap_db import alembic_config
from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

SUITE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
KB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


@contextmanager
def throwaway():
    base = _base_url()
    schema = f"eap_test_ac10_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        yield engine
    finally:
        engine.dispose()
        drop_schema(base, schema)


def _to(engine, rev, down=False):
    with engine.begin() as conn:
        (command.downgrade if down else command.upgrade)(alembic_config(conn), rev)


def _run(conn, rid, tenant, kind, **cols):
    vals = {"id": rid, "t": tenant, "kind": kind, "tool": None, "wf": None, "wfv": None, "ag": None, "agv": None,
            "ds": None, "kb": None, "suite": None}
    vals.update(cols)
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, tool_id, workflow_id, workflow_version, agent_id, agent_version,"
        " draft_spec, knowledge_base_id, status, input, created_at, updated_at)"
        " VALUES (:id, :t, :kind, :tool, :wf, :wfv, :ag, :agv, CAST(:ds AS jsonb), :kb, 'completed', '{}', now(), now())"
    ), vals)


def _populate(conn):
    for t in ("t1", "t2"):
        _run(conn, f"{t}-agent", t, "agent", ag="ag", agv=1)
        _run(conn, f"{t}-draft", t, "agent", ag="ag", ds="{}")
        _run(conn, f"{t}-tool", t, "tool", tool="calc")
        _run(conn, f"{t}-wf", t, "workflow", wf="w", wfv=1)
        _run(conn, f"{t}-kb", t, "kb_ingest", kb=KB)
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, parent_run_id,"
        " parent_step_id, created_at, updated_at)"
        " VALUES ('t1-child', 't1', 'agent', 'ag', 1, 'completed', 'x', 't1-wf', 's1', now(), now())"
    ))
    conn.execute(text(
        "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
        " VALUES ('t1', 't1-agent', 0, 'k', 'start', '{}'::jsonb, now())"
    ))


def _state(conn):
    return {
        "runs": [tuple(r) for r in conn.execute(text(
            "SELECT id, tenant_id, kind, agent_id, agent_version, tool_id, workflow_id, knowledge_base_id,"
            " parent_run_id, parent_step_id FROM runs ORDER BY id"))],
        "events": [tuple(r) for r in conn.execute(text("SELECT run_id, seq, event_key FROM execution_events"))],
    }


def test_up_down_up_on_a_populated_schema_keeps_every_existing_row():
    with throwaway() as engine:
        _to(engine, "0014_approvals")
        with engine.begin() as conn:
            _populate(conn)
        with engine.connect() as conn:
            before = _state(conn)
        assert len(before["runs"]) == 11
        _to(engine, "0015_evals")
        with engine.connect() as conn:
            assert _state(conn) == before
            assert conn.execute(text("SELECT count(*) FROM eval_suites")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM runs WHERE eval_suite_id IS NOT NULL")).scalar_one() == 0
        # An eval run (with a child and rows) is added, then the downgrade removes only the eval side.
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO eval_suites (id, tenant_id, agent_id, name) VALUES (:s, 't1', 'ag', 'n')"), {"s": SUITE})
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, eval_suite_id, status, input, created_at, updated_at)"
                " VALUES ('ev', 't1', 'eval', 'ag', 1, :s, 'running', '{}', now(), now())"), {"s": SUITE})
            conn.execute(text(
                "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                " VALUES ('t1', 'ev', 0, 'case:x', 'eval_case_result', '{}'::jsonb, now())"))
        _to(engine, "0014_approvals", down=True)
        with engine.connect() as conn:
            assert _state(conn) == before
            assert conn.execute(text("SELECT to_regclass('eval_suites')")).scalar_one() is None
        _to(engine, "0015_evals")
        with engine.connect() as conn:
            assert _state(conn) == before


def test_kind_check_accepts_eval_with_suite_and_rejects_without():
    cols = "id, tenant_id, kind, agent_id, agent_version, draft_spec, eval_suite_id, tool_id, workflow_id, workflow_version, knowledge_base_id"
    cases = [
        ("ok-pub", "'ag', 1, NULL, '@S@', NULL, NULL, NULL, NULL", True),
        ("ok-draft", "'ag', NULL, '{}'::jsonb, '@S@', NULL, NULL, NULL, NULL", True),
        ("no-suite", "'ag', 1, NULL, NULL, NULL, NULL, NULL, NULL", False),
        ("no-agent", "NULL, NULL, NULL, '@S@', NULL, NULL, NULL, NULL", False),
        ("both", "'ag', 1, '{}'::jsonb, '@S@', NULL, NULL, NULL, NULL", False),
        ("neither", "'ag', NULL, NULL, '@S@', NULL, NULL, NULL, NULL", False),
        ("with-tool", "'ag', 1, NULL, '@S@', 'calc', NULL, NULL, NULL", False),
        ("with-wf", "'ag', 1, NULL, '@S@', NULL, 'w', 1, NULL", False),
        ("with-kb", f"'ag', 1, NULL, '@S@', NULL, NULL, NULL, '{KB}'", False),
    ]
    with throwaway() as engine:
        _to(engine, "head")
        for name, vals, ok in cases:
            sql = f"INSERT INTO runs ({cols}, status, input, created_at, updated_at) VALUES ('{name}', 't1', 'eval', {vals.replace('@S@', SUITE)}, 'queued', '{{}}', now(), now())"
            if ok:
                with engine.begin() as conn:
                    conn.execute(text(sql))
            else:
                with pytest.raises(IntegrityError, match="ck_runs_kind_target"):
                    with engine.begin() as conn:
                        conn.execute(text(sql))


def test_other_kinds_are_unchanged_and_keep_their_rules():
    with throwaway() as engine:
        _to(engine, "head")
        with engine.begin() as conn:
            _populate(conn)  # all four old kinds still insert
        bad = [
            ("agent", "agent_id", "'ag'"),  # neither version nor draft
            ("tool", "tool_id, agent_id", "'calc', 'ag'"),
            ("workflow", "workflow_id", "'w'"),
            ("kb_ingest", "knowledge_base_id, agent_id", f"'{KB}', 'ag'"),
            ("bogus", "agent_id, agent_version", "'ag', 1"),
        ]
        for i, (kind, cols, vals) in enumerate(bad):
            with pytest.raises(IntegrityError, match="ck_runs_kind_target"):
                with engine.begin() as conn:
                    conn.execute(text(
                        f"INSERT INTO runs (id, tenant_id, kind, {cols}, status, input, created_at, updated_at)"
                        f" VALUES ('bad{i}', 't1', '{kind}', {vals}, 'queued', '{{}}', now(), now())"))


def test_alembic_check_is_clean_at_head_in_a_throwaway_schema():
    with throwaway() as engine:
        _to(engine, "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
        with engine.begin() as conn:
            command.check(alembic_config(conn))  # raises on pending autogenerate ops


def test_a_single_head_that_includes_0015():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    chain = [r.revision for r in script.walk_revisions()]
    assert "0015_evals" in chain
    assert script.get_revision("0015_evals").down_revision == "0014_approvals"
    assert chain.count("0015_evals") == 1
