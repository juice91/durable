"""AGUI.2 acceptance: 0016_ag_ui on a populated 0015 schema (throwaway schemas only). Up, down and up
keep every run, conversation and event; the downgrade removes only the AG-UI side; the partial
unique index allows NULLs and is per tenant and agent; `alembic check` is clean; one head."""

from __future__ import annotations

import hashlib
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
from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)
FP = hashlib.sha256(b"fp").hexdigest()


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


@contextmanager
def throwaway():
    base = _base_url()
    schema = f"eap_test_agui2_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
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


def _populate(conn):
    for t in ("t1", "t2"):
        conn.execute(text(
            "INSERT INTO conversations (id, tenant_id, agent_id, created_at, updated_at)"
            " VALUES (:c, :t, 'ag', now(), now())"), {"c": f"{t}-c", "t": t})
        conn.execute(text(
            "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, conversation_id, turn,"
            " created_at, updated_at) VALUES (:r, :t, 'agent', 'ag', 1, 'completed', 'hi', :c, 1, now(), now())"),
            {"r": f"{t}-r", "t": t, "c": f"{t}-c"})
    conn.execute(text(
        "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
        " VALUES ('t1', 't1-r', 0, 'k', 'start', '{}'::jsonb, now())"))


def _state(conn):
    return {
        "runs": [tuple(r) for r in conn.execute(text(
            "SELECT id, tenant_id, agent_id, conversation_id, turn FROM runs ORDER BY id"))],
        "convs": [tuple(r) for r in conn.execute(text("SELECT id, tenant_id, agent_id FROM conversations ORDER BY id"))],
        "events": [tuple(r) for r in conn.execute(text("SELECT run_id, seq, event_key FROM execution_events"))],
    }


def _ag(conn, tenant, run, er="x", thread="th", scope="ag"):
    conn.execute(text(
        "INSERT INTO ag_ui_runs (tenant_id, scope_kind, scope_id, external_thread_id, external_run_id, run_id,"
        " from_seq, fingerprint) VALUES (:t, 'agent', :s, :th, :er, :r, -1, :f)"),
        {"t": tenant, "s": scope, "th": thread, "er": er, "r": run, "f": FP})


def test_up_down_up_on_a_populated_schema_keeps_every_existing_row():
    with throwaway() as engine:
        _to(engine, "0015_evals")
        with engine.begin() as conn:
            _populate(conn)
        with engine.connect() as conn:
            before = _state(conn)
        _to(engine, "0016_ag_ui")
        with engine.connect() as conn:
            assert _state(conn) == before
            assert conn.execute(text("SELECT count(*) FROM ag_ui_runs")).scalar_one() == 0
        with engine.begin() as conn:
            _ag(conn, "t1", "t1-r")
            conn.execute(text("UPDATE conversations SET external_thread_id = 'th' WHERE id = 't1-c'"))
            conn.execute(text("UPDATE runs SET client_tools = '[]'::jsonb WHERE id = 't1-r'"))
        _to(engine, "0015_evals", down=True)
        with engine.connect() as conn:
            assert _state(conn) == before
            assert conn.execute(text("SELECT to_regclass('ag_ui_runs')")).scalar_one() is None
        _to(engine, "0016_ag_ui")
        with engine.connect() as conn:
            assert _state(conn) == before
            assert conn.execute(text("SELECT count(*) FROM ag_ui_runs")).scalar_one() == 0


def test_partial_unique_index_allows_nulls_and_is_per_tenant_and_agent():
    with throwaway() as engine:
        _to(engine, "head")
        ins = ("INSERT INTO conversations (id, tenant_id, agent_id, external_thread_id, created_at, updated_at)"
               " VALUES (:i, :t, :a, :x, now(), now())")
        with engine.begin() as conn:
            # Any number of NULLs for one tenant and agent.
            for i in range(3):
                conn.execute(text(ins), {"i": f"n{i}", "t": "t1", "a": "ag", "x": None})
            conn.execute(text(ins), {"i": "a", "t": "t1", "a": "ag", "x": "th"})
            conn.execute(text(ins), {"i": "b", "t": "t1", "a": "ag2", "x": "th"})
            conn.execute(text(ins), {"i": "c", "t": "t2", "a": "ag", "x": "th"})
        with pytest.raises(IntegrityError, match="uq_conversations_tenant_agent_external_thread"):
            with engine.begin() as conn:
                conn.execute(text(ins), {"i": "d", "t": "t1", "a": "ag", "x": "th"})


def test_ag_ui_primary_key_is_the_five_part_key():
    with throwaway() as engine:
        _to(engine, "head")
        with engine.begin() as conn:
            _populate(conn)
            _ag(conn, "t1", "t1-r")
            _ag(conn, "t1", "t1-r", er="y")          # another run id
            _ag(conn, "t1", "t1-r", thread="th2")    # another thread
            _ag(conn, "t1", "t1-r", scope="ag2")     # another scope id
            _ag(conn, "t2", "t2-r")                  # another tenant
            conn.execute(text(  # another scope kind
                "INSERT INTO ag_ui_runs (tenant_id, scope_kind, scope_id, external_thread_id, external_run_id,"
                " run_id, from_seq, fingerprint) VALUES ('t1', 'workflow', 'ag', 'th', 'x', 't1-r', 3, :f)"), {"f": FP})
            assert conn.execute(text("SELECT count(*) FROM ag_ui_runs")).scalar_one() == 6
        with pytest.raises(IntegrityError, match="ag_ui_runs_pkey"):
            with engine.begin() as conn:
                _ag(conn, "t1", "t1-r")


def test_alembic_check_is_clean_at_head_in_a_throwaway_schema():
    with throwaway() as engine:
        _to(engine, "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
        with engine.begin() as conn:
            command.check(alembic_config(conn))


def test_a_single_head_that_includes_0016():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    chain = [r.revision for r in script.walk_revisions()]
    assert chain.count("0016_ag_ui") == 1
    assert script.get_revision("0016_ag_ui").down_revision == "0015_evals"
