"""0010_run_options: existing runs keep their rows (options NULL), the widened kind check, the
agent-filtered conversations index, and a clean round trip (downgrade drops draft runs)."""

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

_RUN = (
    "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, created_at, updated_at{extra})"
    " VALUES (:id, 't1', 'agent', 'ag', :version, 'queued', 'hi', now(), now(){values})"
)


_USAGE = (
    "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
    " cache_read_tokens, cache_write_tokens, usage_measured, created_at)"
    " VALUES ('t1', :run, 'model:0', 'test:echo', 1, 1, 0, 0, true, now())"
)
# (tenant, key): the downgrade deletes the draft run's `draft:` keys in its own tenant only; `draft`
# without the colon, `drafty:...` (another run id that shares the prefix), another tenant's
# `draft:...` and the published run's keys stay.
_SIDE_EFFECTS = (
    ("t1", "draft:tool"),
    ("t1", "draft:wf:step"),
    ("t1", "draft"),
    ("t1", "drafty:tool"),
    ("t1", "old:tool"),
    ("t2", "draft:tool"),
)


def _base_url() -> str:
    from sqlalchemy.engine import make_url

    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t AND table_schema = current_schema()"
    ), {"t": table}).scalars())


def _indexes(conn, table: str) -> dict[str, str]:
    return dict(conn.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t AND schemaname = current_schema()"
    ), {"t": table}).all())


@requires_schema
def test_run_options_revision_round_trip():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0010_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0009_version_restored_from")
            conn.execute(text(_RUN.format(extra="", values="")), {"id": "old", "version": 1})
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0010_run_options")
        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT request_context IS NULL AND draft_spec IS NULL FROM runs WHERE id = 'old'"
            )).scalar() is True
            index = _indexes(conn, "conversations")["ix_conversations_tenant_agent_updated"]
            assert "(tenant_id, agent_id, updated_at DESC, id DESC)" in index

        draft = _RUN.format(extra=", draft_spec, request_context", values=", CAST(:draft AS jsonb), CAST(:ctx AS jsonb)")
        with engine.begin() as conn:
            conn.execute(text(draft), {"id": "draft", "version": None, "draft": '{"name": "A"}', "ctx": '{"x": 1}'})
            conn.execute(text("INSERT INTO execution_events (run_id, tenant_id, seq, type, data, ts, event_key)"
                              " VALUES ('draft', 't1', 0, 'run_started', '{}', now(), 'start')"))
            for run_id in ("draft", "old"):  # usage rows: the draft run's go, the published run's stay
                conn.execute(text(_USAGE), {"run": run_id})
            # side_effects: only `{draft run id}:` keys of the draft run's tenant go.
            for tenant, key in _SIDE_EFFECTS:
                conn.execute(text(
                    "INSERT INTO side_effects (tenant_id, key, state, created_at) VALUES (:t, :k, 'done', now())"
                ), {"t": tenant, "k": key})
        for version, spec in ((None, None), (2, '{"name": "A"}')):  # neither target, both targets
            with pytest.raises(IntegrityError, match="ck_runs_kind_target"):
                with engine.begin() as conn:
                    conn.execute(text(draft), {"id": f"bad{version}", "version": version, "draft": spec, "ctx": None})

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0009_version_restored_from")
        with engine.connect() as conn:
            assert {"request_context", "draft_spec"}.isdisjoint(_columns(conn, "runs"))
            assert conn.execute(text("SELECT id FROM runs ORDER BY id")).scalars().all() == ["old"]
            assert conn.execute(text("SELECT count(*) FROM execution_events")).scalar() == 0
            assert conn.execute(text("SELECT run_id FROM run_model_requests")).scalars().all() == ["old"]
            kept = conn.execute(text("SELECT tenant_id, key FROM side_effects ORDER BY tenant_id, key")).all()
            assert [tuple(r) for r in kept] == sorted(
                (t, k) for t, k in _SIDE_EFFECTS if not (t == "t1" and k.startswith("draft:"))
            )
            assert "ix_conversations_tenant_agent_updated" not in _indexes(conn, "conversations")

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)
