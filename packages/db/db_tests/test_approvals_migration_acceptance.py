"""AC.3 acceptance: 0014_approvals on a realistic populated 0013 schema (throwaway schemas only).

Open and decided step waits (approved, rejected, timed out), nested child runs, the same step id in
two runs, events without a usable deadline, tool waits and look-alike events that must NOT backfill;
then backfill correctness, nothing lost, `alembic check` clean, and a downgrade/upgrade round trip
that never resurrects a decided wait."""

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

DL = "2030-05-06T07:08:09+00:00"
EVENT_TS = "2029-12-31T00:00:00+00:00"
NEW_INDEXES = {"ix_pending_approvals_tenant_open", "ix_pending_approvals_tenant_decided",
               "ix_pending_approvals_tenant_root_run"}


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _run(conn, run_id, tenant, kind, status="running", **cols):
    base = {"id": run_id, "t": tenant, "kind": kind, "status": status}
    base.update({"wf": None, "wfv": None, "ag": None, "agv": None, "parent": None, "pstep": None, "out": None})
    base.update(cols)
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, status, workflow_id, workflow_version, agent_id, agent_version,"
        " parent_run_id, parent_step_id, input, output, created_at, updated_at)"
        " VALUES (:id, :t, :kind, :status, :wf, :wfv, :ag, :agv, :parent, :pstep, '{}', :out, now(), now())"
    ), base)


def _event(conn, tenant, run, seq, key, typ, data):
    conn.execute(text(
        "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
        " VALUES (:t, :r, :s, :k, :ty, CAST(:d AS jsonb), :ts)"
    ), {"t": tenant, "r": run, "s": seq, "k": key, "ty": typ, "d": data, "ts": EVENT_TS})


def _wait(conn, tenant, run, seq, step, deadline=f'"{DL}"', typ="step_waiting", key=None):
    data = '{"step_id": "%s", "prompt": "p-%s"%s}' % (step, step, "" if deadline is None else f', "deadline": {deadline}')
    _event(conn, tenant, run, seq, key or f"step:{step}:waiting", typ, data)


def _decide(conn, tenant, run, seq, step, typ):
    _event(conn, tenant, run, seq, f"step:{step}:decision", typ, '{"step_id": "%s", "decided_by": "u"}' % step)


def _populate_0013(conn) -> None:
    _run(conn, "wfA", "t1", "workflow", wf="wfdefA", wfv=2)
    _run(conn, "wfB", "t1", "workflow", "completed", wf="wfdefB", wfv=1, out="done")
    _run(conn, "wfZ", "t2", "workflow", wf="wfdefZ", wfv=1)
    for child, agent, status, step in (("chA1", "agA", "running", "s1"), ("chA2", "agB", "completed", "s2"),
                                       ("chA3", "agA", "running", "s3")):
        _run(conn, child, "t1", "agent", status, ag=agent, agv=1, parent="wfA", pstep=step)
    _run(conn, "plain", "t1", "agent", ag="agP", agv=1)
    # wfA: open waits with and without a usable deadline, decided waits, and look-alikes.
    _wait(conn, "t1", "wfA", 0, "gate")
    _wait(conn, "t1", "wfA", 1, "nodl", deadline=None)
    _wait(conn, "t1", "wfA", 2, "nulldl", deadline="null")
    _wait(conn, "t1", "wfA", 3, "numdl", deadline="12345")
    _wait(conn, "t1", "wfA", 4, "tz", deadline='"2031-01-01T10:00:00+02:00"')
    _wait(conn, "t1", "wfA", 5, "a.b*c")
    for seq, (step, typ) in enumerate((("ok", "step_approved"), ("rej", "step_rejected"), ("to", "step_timed_out")), 6):
        _wait(conn, "t1", "wfA", seq * 2, step)
        _decide(conn, "t1", "wfA", seq * 2 + 1, step, typ)
    _decide(conn, "t1", "wfA", 30, "orphan", "step_approved")  # a decision without a wait
    _event(conn, "t1", "wfA", 31, "step:gate:started", "step_started", '{"step_id": "gate"}')
    _wait(conn, "t1", "wfA", 32, "x", key="legacy-wait")  # step_waiting type, key not a step wait
    _wait(conn, "t1", "wfA", 33, "typo", typ="step_started")  # a step wait key, another type
    # Child runs: an open wait on a running and on a completed child, a decided wait on a third.
    _wait(conn, "t1", "chA1", 0, "ask")
    _event(conn, "t1", "chA1", 1, "tool:c1:approval:waiting", "tool_approval_waiting",
           '{"tool_call_id": "c1", "tool_name": "send", "args": {}, "deadline": "%s"}' % DL)
    _wait(conn, "t1", "chA2", 0, "late")
    _wait(conn, "t1", "chA3", 0, "dec")
    _decide(conn, "t1", "chA3", 1, "dec", "step_approved")
    # The same step id decided in one run, open in another.
    _wait(conn, "t1", "wfB", 0, "gate")
    _decide(conn, "t1", "wfB", 1, "gate", "step_approved")
    _wait(conn, "t1", "wfB", 2, "gate2")
    _wait(conn, "t2", "wfZ", 0, "gate")
    _event(conn, "t1", "plain", 0, "tool:c9:approval:waiting", "tool_approval_waiting",
           '{"tool_call_id": "c9", "tool_name": "send", "args": {}, "deadline": "%s"}' % DL)


def _state(conn) -> dict:
    q = lambda sql: [tuple(r) for r in conn.execute(text(sql))]  # noqa: E731
    return {
        "runs": q("SELECT id, tenant_id, kind, status, workflow_id, agent_id, parent_run_id, output FROM runs ORDER BY id"),
        "events": q("SELECT run_id, seq, event_key, type, data::text FROM execution_events ORDER BY 1, 2"),
    }


def _rows(conn) -> dict:
    out = {}
    for r in conn.execute(text(
        "SELECT tenant_id, run_id, approval_key, kind, root_run_id, agent_id, workflow_id, prompt, tool_name, args,"
        " to_char(deadline AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),"
        " to_char(requested_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), decided_at, decision, decided_by"
        " FROM pending_approvals"
    )):
        out[(r[0], r[1], r[2])] = tuple(r[3:])
    return out


def _expect(root, agent, wf, key, deadline=DL):
    dl = deadline.replace("T", " ")[:19]
    return ("step", root, agent, wf, f"p-{key}", None, None, dl, "2029-12-31 00:00:00", None, None, None)


DL_TXT = "2030-05-06 07:08:09"
DEFAULT_DL = "2030-01-01 00:00:00"  # event ts + 24 h
EXPECTED = {
    ("t1", "chA1", "ask"): _expect("wfA", "agA", "wfdefA", "ask", DL_TXT),
    ("t1", "chA2", "late"): _expect("wfA", "agB", "wfdefA", "late", DL_TXT),
    ("t1", "wfA", "gate"): _expect("wfA", None, "wfdefA", "gate", DL_TXT),
    ("t1", "wfA", "nodl"): _expect("wfA", None, "wfdefA", "nodl", DEFAULT_DL),
    ("t1", "wfA", "nulldl"): _expect("wfA", None, "wfdefA", "nulldl", DEFAULT_DL),
    ("t1", "wfA", "numdl"): _expect("wfA", None, "wfdefA", "numdl", DEFAULT_DL),
    ("t1", "wfA", "tz"): _expect("wfA", None, "wfdefA", "tz", "2031-01-01 08:00:00"),
    ("t1", "wfA", "a.b*c"): _expect("wfA", None, "wfdefA", "a.b*c", DL_TXT),
    ("t1", "wfB", "gate2"): _expect("wfB", None, "wfdefB", "gate2", DL_TXT),
    ("t2", "wfZ", "gate"): _expect("wfZ", None, "wfdefZ", "gate", DL_TXT),
}


def _migrated_schema():
    base = _base_url()
    schema = f"eap_test_0014a_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    return base, schema, create_engine(schema_url(base, schema))


def _check_clean(base, schema):
    check = create_engine(schema_url(base, schema, public=True))
    try:
        with check.connect() as conn:
            assert schema_diff(conn) == []  # what `alembic check` reports
    finally:
        check.dispose()


def test_backfill_on_realistic_0013_data_then_round_trip():
    from alembic import command

    from eap_db import alembic_config

    base, schema, engine = _migrated_schema()
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0013_semantic_recall")
            _populate_0013(conn)
            before = _state(conn)
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0014_approvals")
        with engine.connect() as conn:
            assert _rows(conn) == EXPECTED  # exactly the open step waits, nothing for look-alikes/tool waits
            assert _state(conn) == before  # nothing lost or changed
            assert conn.execute(text("SELECT count(*) FROM runs WHERE output_format IS NOT NULL")).scalar_one() == 0
            idx = set(conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")).scalars())
            assert NEW_INDEXES <= idx
            # Every index leads with tenant_id.
            lead = conn.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()"
                " AND tablename = 'pending_approvals'")).all()
            assert all("(tenant_id" in d for _, d in lead), lead
        # The models are compared at head only (below): a later revision (0015_evals) changes them.

        # Decide one backfilled wait (event + row, as the store does), then downgrade.
        with engine.begin() as conn:
            _decide(conn, "t1", "wfA", 40, "gate", "step_rejected")
            conn.execute(text(
                "UPDATE pending_approvals SET decision = 'denied', decided_at = now(), decided_by = 'u'"
                " WHERE run_id = 'wfA' AND approval_key = 'gate'"))
            before_down = _state(conn)
            command.downgrade(alembic_config(conn), "0013_semantic_recall")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('pending_approvals')")).scalar_one() is None
            cols = set(conn.execute(text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = 'runs'")).scalars())
            assert "output_format" not in cols
            assert _state(conn) == before_down  # the event log keeps everything
            cons = set(conn.execute(text(
                "SELECT c.conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
                " WHERE n.nspname = current_schema()")).scalars())
            assert not {"ck_runs_output_format", "pending_approvals_pkey"} & cons
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0013_semantic_recall"

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            again = _rows(conn)
        want = dict(EXPECTED)
        del want[("t1", "wfA", "gate")]  # decided before the downgrade: its decision event keeps it out
        assert again == want
        _check_clean(base, schema)
        # The CHECKs are enforced after the second upgrade too.
        with pytest.raises(IntegrityError, match="ck_runs_output_format"):
            with engine.begin() as conn:
                conn.execute(text("UPDATE runs SET output_format = 'yaml' WHERE id = 'wfA'"))
    finally:
        engine.dispose()
        drop_schema(base, schema)


def test_upgrade_on_an_empty_0013_schema_backfills_nothing_and_keeps_run_defaults():
    from alembic import command

    from eap_db import alembic_config

    base, schema, engine = _migrated_schema()
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0013_semantic_recall")
            _run(conn, "r1", "t1", "workflow", wf="w", wfv=1)  # a run with no events at all
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM pending_approvals")).scalar_one() == 0
            assert conn.execute(text("SELECT output_format FROM runs WHERE id = 'r1'")).scalar_one() is None
            for ok in ("text", "json", None):
                conn.execute(text("UPDATE runs SET output_format = :f WHERE id = 'r1'"), {"f": ok})
            conn.rollback()
        _check_clean(base, schema)
    finally:
        engine.dispose()
        drop_schema(base, schema)
