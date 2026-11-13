"""RM.4 acceptance: 0011 on a populated 0010 schema (every run kind, usage rows), down and up again.

Throwaway schemas only; the dev database's own tables are never touched."""

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

_RUN_COLS = "id, tenant_id, kind, agent_id, agent_version, tool_id, workflow_id, workflow_version, status, input"
_USAGE = (
    "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
    " cache_read_tokens, cache_write_tokens, usage_measured, created_at{extra_cols})"
    " VALUES ('t1', :run, :key, 'test:echo', 3, 4, 0, 0, true, now(){extra_vals})"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _kind_check(conn) -> str:
    return conn.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_runs_kind_target'"
        " AND conrelid = 'runs'::regclass"
    )).scalar()


def _populate_0010(conn) -> None:
    conn.execute(text(
        "INSERT INTO agents (id, tenant_id, name, archived, created_at, updated_at, draft_spec)"
        " VALUES ('ag', 't1', 'Agent', false, now(), now(), '{}'::jsonb)"
    ))
    conn.execute(text(
        "INSERT INTO agent_versions (id, agent_id, tenant_id, version, spec, spec_hash, created_at, created_by)"
        " VALUES ('agv', 'ag', 't1', 1, '{}'::jsonb, 'h', now(), 'u')"
    ))
    ins = (
        "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, tool_id, workflow_id, workflow_version,"
        " draft_spec, status, input, created_at, updated_at) VALUES (:id, 't1', :kind, :agent, :version, :tool,"
        " :wf, :wfv, CAST(:draft AS jsonb), 'succeeded', 'hi', now(), now())"
    )
    base = dict(agent=None, version=None, tool=None, wf=None, wfv=None, draft=None)
    for row in (
        {"id": "r-agent", "kind": "agent", "agent": "ag", "version": 1},
        {"id": "r-draft", "kind": "agent", "agent": "ag", "draft": "{}"},
        {"id": "r-tool", "kind": "tool", "tool": "some.tool"},
        {"id": "r-workflow", "kind": "workflow", "wf": "wf1", "wfv": 2},
    ):
        conn.execute(text(ins), {**base, **row})
    for run, key in (("r-agent", "model:0"), ("r-agent", "model:1"), ("r-tool", "tool:0"), ("r-workflow", "wf:0")):
        conn.execute(text(_USAGE.format(extra_cols="", extra_vals="")), {"run": run, "key": key})
    conn.execute(text(
        "INSERT INTO execution_events (run_id, tenant_id, seq, type, data, ts, event_key)"
        " VALUES ('r-agent', 't1', 0, 'run_started', '{}', now(), 'e0')"
    ))


def _runs(conn):
    return [tuple(r) for r in conn.execute(text(f"SELECT {_RUN_COLS} FROM runs ORDER BY id"))]


def test_populated_0010_upgrade_downgrade_upgrade():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0011a_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0010_run_options")
            _populate_0010(conn)
            check_0010 = _kind_check(conn)
            runs_before = _runs(conn)
            usage_before = conn.execute(text("SELECT run_id, request_key, input_tokens FROM run_model_requests ORDER BY 1, 2")).all()
        assert len(runs_before) == 4

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0011_knowledge_bases")
        with engine.connect() as conn:
            # Every pre-existing run satisfies the new check (it is validated, not NOT VALID) and is untouched.
            assert conn.execute(text(
                "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_runs_kind_target' AND conrelid = 'runs'::regclass"
            )).scalar() is True
            assert _runs(conn) == runs_before
            assert conn.execute(text("SELECT count(*) FROM runs WHERE knowledge_base_id IS NOT NULL")).scalar() == 0
            assert conn.execute(text(
                "SELECT DISTINCT operation FROM run_model_requests")).scalars().all() == ["chat"]
            assert conn.execute(text(
                "SELECT run_id, request_key, input_tokens FROM run_model_requests ORDER BY 1, 2")).all() == usage_before
            default = conn.execute(text(
                "SELECT column_default FROM information_schema.columns WHERE table_name = 'run_model_requests'"
                " AND column_name = 'operation' AND table_schema = current_schema()")).scalar()
            assert "chat" in default
        # A usage insert that omits `operation` (an older worker) still works and reads back 'chat'.
        with engine.begin() as conn:
            conn.execute(text(_USAGE.format(extra_cols="", extra_vals="")), {"run": "r-tool", "key": "old-worker"})
            assert conn.execute(text(
                "SELECT operation FROM run_model_requests WHERE request_key = 'old-worker'")).scalar() == "chat"
            # An embedding row on an existing agent run (the retrieve tool) must survive the downgrade.
            conn.execute(text(_USAGE.format(extra_cols=", operation", extra_vals=", 'embedding'")),
                         {"run": "r-agent", "key": "embed:retrieve:1"})

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0010_run_options")
        with engine.connect() as conn:
            assert _kind_check(conn) == check_0010  # byte-identical to 0010's check
            assert _runs(conn) == runs_before
            assert "knowledge_base_id" not in {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'runs'"
                " AND table_schema = current_schema()"))}
            assert conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")).scalar() == 1
            assert conn.execute(text("SELECT count(*) FROM run_model_requests")).scalar() == len(usage_before) + 2
        # The restored check rejects the new kind again.
        with pytest.raises(IntegrityError, match="ck_runs_kind_target"):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at)"
                    " VALUES ('x', 't1', 'kb_ingest', 'queued', now(), now())"))

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
            assert _runs(conn) == runs_before
            assert conn.execute(text("SELECT count(*) FROM run_model_requests WHERE operation = 'embedding'")).scalar() == 0
    finally:
        engine.dispose()
    check = create_engine(schema_url(base, schema, public=True))
    try:
        with check.connect() as conn:
            assert schema_diff(conn) == []
    finally:
        check.dispose()
        drop_schema(base, schema)



def test_alembic_check_is_clean_at_head():
    """`alembic check` (the autogenerate comparison, as the CLI runs it) reports nothing at head."""
    import subprocess
    from pathlib import Path

    base = _base_url()
    schema = f"eap_test_chk_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "EAP_DATABASE_URL": schema_url(base, schema)}
    try:
        for args in (["upgrade", "head"], ["check"]):
            done = subprocess.run(
                ["uv", "run", "alembic", "-c", "packages/db/alembic.ini", *args],
                cwd=root, env=env, capture_output=True, text=True, timeout=180,
            )
            assert done.returncode == 0, done.stdout + done.stderr
        assert "No new upgrade operations detected" in done.stdout + done.stderr
    finally:
        drop_schema(base, schema)
