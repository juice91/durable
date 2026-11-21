"""AGUI.3 acceptance: 0017_api_keys on a populated 0016 schema (throwaway schemas only). Up, down and
up keep every existing row; the downgrade drops only api_keys; defaults and checks hold; one head;
`alembic check` is clean."""

from __future__ import annotations

import hashlib
import os
import uuid
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from eap_db import alembic_config
from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)
H = hashlib.sha256(b"k").hexdigest()


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


@contextmanager
def throwaway():
    base = _base_url()
    schema = f"eap_test_ak17_{uuid.uuid4().hex[:8]}"
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
    conn.execute(text(
        "INSERT INTO conversations (id, tenant_id, agent_id, created_at, updated_at)"
        " VALUES ('c1', 't1', 'ag', now(), now())"))
    conn.execute(text(
        "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, conversation_id, turn,"
        " created_at, updated_at) VALUES ('r1', 't1', 'agent', 'ag', 1, 'completed', 'hi', 'c1', 1, now(), now())"))


def _state(conn):
    return [tuple(r) for r in conn.execute(text(
        "SELECT r.id, r.tenant_id, r.conversation_id, c.agent_id FROM runs r JOIN conversations c"
        " ON c.id = r.conversation_id ORDER BY r.id"))]


def _key(conn, key_id, tenant="t1", name="n", h=H, **cols):
    names = "".join(f", {c}" for c in cols)
    vals = "".join(f", {v}" for v in cols.values())
    conn.execute(text(
        f"INSERT INTO api_keys (id, tenant_id, name, secret_hash, created_by{names})"
        f" VALUES (:i, :t, :n, :h, 'alice'{vals})"), {"i": key_id, "t": tenant, "n": name, "h": h})


def test_up_down_up_on_a_populated_0016_schema_keeps_every_row():
    with throwaway() as engine:
        _to(engine, "0016_ag_ui")
        with engine.begin() as conn:
            _populate(conn)
        with engine.connect() as conn:
            before = _state(conn)
            assert before and conn.execute(text("SELECT to_regclass('api_keys')")).scalar_one() is None
        _to(engine, "0017_api_keys")
        with engine.begin() as conn:
            assert _state(conn) == before
            assert conn.execute(text("SELECT count(*) FROM api_keys")).scalar_one() == 0
            _key(conn, "a" * 24)
        _to(engine, "0016_ag_ui", down=True)
        with engine.connect() as conn:
            assert _state(conn) == before
            assert conn.execute(text("SELECT to_regclass('api_keys')")).scalar_one() is None
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0016_ag_ui"
            assert "ag_ui_runs" in inspect(conn).get_table_names()
        _to(engine, "0017_api_keys")
        with engine.connect() as conn:
            assert _state(conn) == before
            assert conn.execute(text("SELECT count(*) FROM api_keys")).scalar_one() == 0
            idx = {i["name"] for i in inspect(conn).get_indexes("api_keys")}
            assert "ix_api_keys_tenant_created" in idx


def test_defaults_and_nullable_columns():
    with throwaway() as engine:
        _to(engine, "head")
        with engine.begin() as conn:
            _key(conn, "b" * 24)
            row = conn.execute(text(
                "SELECT scopes, agent_ids, workflow_ids, allowed_origins, expires_at, revoked_at, last_used_at,"
                " created_at IS NOT NULL FROM api_keys")).one()
        assert row == (["agui:run"], None, None, None, None, None, None, True)


def test_same_id_may_not_repeat_across_tenants_and_hashes_may_repeat():
    with throwaway() as engine:
        _to(engine, "head")
        with engine.begin() as conn:
            _key(conn, "c" * 24, tenant="t1")
            _key(conn, "d" * 24, tenant="t2")  # same hash, other row: allowed (no unique on the hash)
        with pytest.raises(IntegrityError, match="api_keys_pkey"):
            with engine.begin() as conn:
                _key(conn, "c" * 24, tenant="t2")  # the id is globally unique


@pytest.mark.parametrize("bad_id", ["A" * 24, "a" * 23, "a" * 25, "g" * 24, ""])
def test_id_check_rejects_non_lowercase_hex_or_wrong_length(bad_id):
    with throwaway() as engine:
        _to(engine, "head")
        with pytest.raises(IntegrityError, match="ck_api_keys_id"):
            with engine.begin() as conn:
                _key(conn, bad_id)


@pytest.mark.parametrize("bad_hash", ["", "A" * 64, "a" * 63, "a" * 65])
def test_hash_check_rejects_non_sha256_hex(bad_hash):
    with throwaway() as engine:
        _to(engine, "head")
        with pytest.raises(IntegrityError, match="ck_api_keys_secret_hash"):
            with engine.begin() as conn:
                _key(conn, "e" * 24, h=bad_hash)


def test_name_check_bounds():
    with throwaway() as engine:
        _to(engine, "head")
        with engine.begin() as conn:
            _key(conn, "1" * 24, name="x" * 100)
        with pytest.raises(IntegrityError, match="ck_api_keys_name"):
            with engine.begin() as conn:
                _key(conn, "2" * 24, name="x" * 101)


def test_not_null_columns_are_enforced():
    with throwaway() as engine:
        _to(engine, "head")
        for col in ("tenant_id", "name", "secret_hash", "created_by"):
            vals = {"tenant_id": "'t'", "name": "'n'", "secret_hash": f"'{H}'", "created_by": "'a'"}
            vals[col] = "NULL"
            with pytest.raises(IntegrityError):
                with engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO api_keys (id, tenant_id, name, secret_hash, created_by) VALUES"
                        f" ('{'3' * 24}', {vals['tenant_id']}, {vals['name']}, {vals['secret_hash']},"
                        f" {vals['created_by']})"))


def test_a_single_head_that_is_0017_over_0016_and_check_is_clean():
    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1  # later revisions build on 0017; test_migrations pins the head
    assert script.get_revision("0017_api_keys").down_revision == "0016_ag_ui"
    with throwaway() as engine:
        _to(engine, "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
        with engine.begin() as conn:
            command.check(alembic_config(conn))
