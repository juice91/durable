"""The migrations own the eap_app schema: head equals the models, and apps refuse an unmigrated database."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from eap_db import NOT_AT_HEAD_MESSAGE, SchemaNotAtHead, current_revisions, head_revisions, require_schema_at_head
from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, create_migrated_schema, drop_schema, schema_url

URL = os.environ.get("EAP_TEST_DATABASE_URL", "")
requires_schema = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


def _base_url() -> str:
    # The session URL without its search_path option: the database the session schema lives in.
    from sqlalchemy.engine import make_url

    url = make_url(URL)
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


@requires_schema
def test_session_schema_is_at_head_and_matches_the_models():
    engine = create_engine(URL)
    try:
        assert current_revisions(engine) == head_revisions()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_schema()")).scalar_one() == os.environ[SCHEMA_ENV]
            assert schema_diff(conn) == []
        require_schema_at_head(engine)
    finally:
        engine.dispose()


@requires_schema
def test_empty_schema_is_refused_with_the_upgrade_command():
    base = _base_url()
    schema = f"eap_test_empty_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with pytest.raises(SchemaNotAtHead) as err:
            require_schema_at_head(engine)
        assert str(err.value).startswith(NOT_AT_HEAD_MESSAGE)
        assert "uv run alembic -c packages/db/alembic.ini upgrade head" in str(err.value)
    finally:
        engine.dispose()
        drop_schema(base, schema)


@requires_schema
def test_upgrade_then_downgrade_round_trips():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_rt_{uuid.uuid4().hex[:8]}"
    url = create_migrated_schema(base, schema)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "base")
        with engine.connect() as conn:
            tables = conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"), {"s": schema}
            ).scalars().all()
        assert tables == ["alembic_version"]
        with pytest.raises(SchemaNotAtHead):
            require_schema_at_head(engine)
    finally:
        engine.dispose()
        drop_schema(base, schema)


@requires_schema
def test_conversations_revision_keeps_existing_runs_as_standalone():
    """0001 -> head on a database with rows: nothing is rewritten, old runs have no conversation."""
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0002_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0001")
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, agent_id, agent_version, status, input, created_at, updated_at) "
                "VALUES ('r1', 't1', 'a1', 1, 'completed', 'hi', now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts) "
                "VALUES ('t1', 'r1', 0, 'start', 'run_started', '{}', now())"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, status, conversation_id, turn, messages FROM runs")).all()
            assert [tuple(r) for r in rows] == [("r1", "completed", None, None, None)]
            assert conn.execute(text("SELECT count(*) FROM execution_events")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 0
            assert schema_diff(conn) == []
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0001")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 1
    finally:
        engine.dispose()
        drop_schema(base, schema)


def test_single_head_is_the_kb_sources_revision():
    assert head_revisions() == {"0032_external_spans"}


def test_revisions_after_api_keys_form_one_chain():
    from alembic.script import ScriptDirectory

    from eap_db import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    chain = ["0017_api_keys", "0018_run_model_settings", "0019_trace_feedback", "0020_scorers", "0021_chat_attachments", "0022_parity_round_2", "0024_mcp_runtime", "0023_attachments_r2", "0025_architect", "0026_architect_slugs", "0027_tenant_secrets", "0028_mcp_catalog_source", "0029_kb_sources", "0030_kb_source_kinds", "0031_workflow_archived_at", "0032_external_spans"]
    for down, up in zip(chain, chain[1:]):
        assert script.get_revision(up).down_revision == down


@requires_schema
def test_mcp_servers_revision_keeps_existing_rows_and_round_trips():
    """0003 -> 0004 on a database with rows: nothing existing changes; the new tables are
    tenant-composite (a secret cannot point at another tenant's server) and cascade on delete;
    downgrade to 0003 removes exactly the two new tables."""
    from alembic import command
    from sqlalchemy.exc import IntegrityError

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0004_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    counted = ("agents", "agent_versions", "runs", "execution_events", "conversations", "side_effects")
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0003")
            conn.execute(text(
                "INSERT INTO agents (id, tenant_id, name, archived, created_at, updated_at) "
                "VALUES ('a1', 't1', 'A', false, now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, agent_id, agent_version, status, input, created_at, updated_at) "
                "VALUES ('r1', 't1', 'a1', 1, 'completed', 'hi', now(), now())"
            ))
            before = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in counted}
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in counted} == before
            assert schema_diff(conn) == []
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO mcp_servers (id, tenant_id, name, url, created_at, updated_at) "
                "VALUES ('s1', 't1', 'docs', 'https://example.com/mcp', now(), now())"
            ))
            row = conn.execute(text("SELECT transport, enabled, last_status FROM mcp_servers")).one()
            assert tuple(row) == ("streamable_http", True, None)
            conn.execute(text(
                "INSERT INTO mcp_server_secrets (tenant_id, server_id, header_name, ciphertext, key_id) "
                "VALUES ('t1', 's1', 'authorization', '\\x00'::bytea, 'k')"
            ))
        for statement in (
            # another tenant's server id
            "INSERT INTO mcp_server_secrets (tenant_id, server_id, header_name, ciphertext, key_id) "
            "VALUES ('t2', 's1', 'authorization', '\\x00'::bytea, 'k')",
            # same name twice in one tenant
            "INSERT INTO mcp_servers (id, tenant_id, name, url, created_at, updated_at) "
            "VALUES ('s2', 't1', 'docs', 'https://example.com/mcp', now(), now())",
            # only streamable_http
            "INSERT INTO mcp_servers (id, tenant_id, name, url, transport, created_at, updated_at) "
            "VALUES ('s3', 't1', 'cmd', 'https://example.com/mcp', 'stdio', now(), now())",
        ):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text(statement))
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO mcp_servers (id, tenant_id, name, url, created_at, updated_at) "
                "VALUES ('s4', 't2', 'docs', 'https://example.com/mcp', now(), now())"
            ))  # the same name in another tenant is fine
            conn.execute(text("DELETE FROM mcp_servers WHERE id = 's1'"))
            assert conn.execute(text("SELECT count(*) FROM mcp_server_secrets")).scalar_one() == 0
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0003")
        with engine.connect() as conn:
            tables = set(conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"), {"s": schema}
            ).scalars())
            assert not {"mcp_servers", "mcp_server_secrets"} & tables
            assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in counted} == before
    finally:
        engine.dispose()
        drop_schema(base, schema)
