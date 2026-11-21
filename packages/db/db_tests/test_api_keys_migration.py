"""0017_api_keys: upgrade, downgrade and upgrade on a populated 0016 schema (a throwaway schema).
The checks hold, the downgrade drops only the keys, and the result matches the models."""

from __future__ import annotations

import hashlib
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

HASH = hashlib.sha256(b"k").hexdigest()
KID = "0123456789abcdef01234567"


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _insert(conn, key_id=KID, name="n", secret_hash=HASH, extra_cols="", extra_vals=""):
    conn.execute(text(
        f"INSERT INTO api_keys (id, tenant_id, name, secret_hash, created_by{extra_cols})"
        f" VALUES (:i, 't1', :n, :h, 'alice'{extra_vals})"
    ), {"i": key_id, "n": name, "h": secret_hash})


def test_api_keys_revision_round_trip():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0017_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0016_ag_ui")
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, created_at, updated_at)"
                " VALUES ('r1', 't1', 'agent', 'ag', 1, 'completed', 'hi', now(), now())"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0017_api_keys")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0017_api_keys"
            _insert(conn)
            row = conn.execute(text("SELECT scopes, agent_ids, revoked_at FROM api_keys")).one()
            assert row == (["agui:run"], None, None)
        bad = [
            (lambda c: _insert(c), "api_keys_pkey"),
            (lambda c: _insert(c, key_id="XYZ"), "ck_api_keys_id"),
            (lambda c: _insert(c, key_id="1" * 24, secret_hash="plain"), "ck_api_keys_secret_hash"),
            (lambda c: _insert(c, key_id="2" * 24, name=""), "ck_api_keys_name"),
            (lambda c: _insert(c, key_id="3" * 24, extra_cols=", scopes", extra_vals=", '{}'::jsonb"),
             "ck_api_keys_scopes"),
            (lambda c: _insert(c, key_id="4" * 24, extra_cols=", agent_ids", extra_vals=", '\"a\"'::jsonb"),
             "ck_api_keys_agent_ids"),
            (lambda c: _insert(c, key_id="5" * 24, extra_cols=", workflow_ids", extra_vals=", '1'::jsonb"),
             "ck_api_keys_workflow_ids"),
            (lambda c: _insert(c, key_id="6" * 24, extra_cols=", allowed_origins", extra_vals=", '{}'::jsonb"),
             "ck_api_keys_allowed_origins"),
        ]
        for write, constraint in bad:
            with pytest.raises(IntegrityError, match=constraint):
                with engine.begin() as conn:
                    write(conn)

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0016_ag_ui")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('api_keys')")).scalar_one() is None
            assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 1
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM api_keys")).scalar_one() == 0
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)


def test_api_keys_revision_follows_ag_ui():
    from alembic.script import ScriptDirectory

    from eap_db import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_revision("0017_api_keys").down_revision == "0016_ag_ui"
    assert len(script.get_heads()) == 1  # later revisions build on 0017; test_migrations pins the head
