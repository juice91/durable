"""0024_mcp_runtime: catalog mirror, runtimes, managed servers, env secrets; upgrade, constraints, downgrade."""

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

SERVER_SQL = (
    "INSERT INTO mcp_servers (id, tenant_id, name, url, runtime, created_at, updated_at)"
    " VALUES (:id, :t, :n, :url, :rt, now(), now())"
)
RUNTIME_SQL = (
    "INSERT INTO mcp_runtimes (id, tenant_id, server_id, package, image, namespace, cr_name, created_at, updated_at)"
    " VALUES (:id, :t, :s, '{}', 'img', 'ns', 'cr', now(), now())"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t AND table_schema = current_schema()"
    ), {"t": table}).scalars())


def _tables(conn) -> set[str]:
    return set(conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )).scalars())


def _server(conn, sid, tenant, name, url, runtime):
    conn.execute(text(SERVER_SQL), {"id": sid, "t": tenant, "n": name, "url": url, "rt": runtime})


def test_upgrade_constraints_downgrade_and_reupgrade():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0024_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0022_parity_round_2")
            conn.execute(text(
                "INSERT INTO mcp_servers (id, tenant_id, name, url, created_at, updated_at)"
                " VALUES ('old', 't1', 'old', 'https://x', now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO mcp_server_secrets (tenant_id, server_id, header_name, ciphertext, key_id)"
                " VALUES ('t1', 'old', 'Authorization', '\\x00', 'k')"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0024_mcp_runtime")
        with engine.connect() as conn:
            assert {"mcp_catalog_entries", "mcp_runtimes"} <= _tables(conn)
            assert "runtime" in _columns(conn, "mcp_servers") and "kind" in _columns(conn, "mcp_server_secrets")
            assert conn.execute(text("SELECT runtime, url FROM mcp_servers WHERE id = 'old'")).one() == ("external", "https://x")
            assert conn.execute(text("SELECT kind FROM mcp_server_secrets")).scalar_one() == "header"
            assert schema_diff(conn) == []

        # external keeps a URL; managed must not have one.
        with engine.begin() as conn:
            _server(conn, "m1", "t1", "managed", None, "managed")
        for sid, url, rt in (("b1", None, "external"), ("b2", "https://y", "managed"), ("b3", "https://y", "weird")):
            with pytest.raises(IntegrityError, match="ck_mcp_servers_runtime"):
                with engine.begin() as conn:
                    _server(conn, sid, "t1", sid, url, rt)

        # runtimes: one per (tenant, server), tenant-composite FK, state check.
        rid = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(text(RUNTIME_SQL), {"id": rid, "t": "t1", "s": "m1"})
            assert conn.execute(text("SELECT state, endpoint FROM mcp_runtimes")).one() == ("pending", None)
        with pytest.raises(IntegrityError, match="uq_mcp_runtimes_tenant_server"):
            with engine.begin() as conn:
                conn.execute(text(RUNTIME_SQL), {"id": str(uuid.uuid4()), "t": "t1", "s": "m1"})
        with pytest.raises(IntegrityError, match="fk_mcp_runtimes_tenant_server"):
            with engine.begin() as conn:
                conn.execute(text(RUNTIME_SQL), {"id": str(uuid.uuid4()), "t": "t2", "s": "m1"})
        with pytest.raises(IntegrityError, match="ck_mcp_runtimes_state"):
            with engine.begin() as conn:
                conn.execute(text("UPDATE mcp_runtimes SET state = 'bogus'"))
        with engine.begin() as conn:  # the sweep's transient state is allowed
            conn.execute(text("UPDATE mcp_runtimes SET state = 'stopping'"))
            conn.execute(text("UPDATE mcp_runtimes SET state = 'pending'"))

        # secrets: header and env rows may share a name; env rows are removed by the downgrade.
        with engine.begin() as conn:
            for kind in ("header", "env"):
                conn.execute(text(
                    "INSERT INTO mcp_server_secrets (tenant_id, server_id, kind, header_name, ciphertext, key_id)"
                    " VALUES ('t1', 'm1', :k, 'TOKEN', '\\x00', 'k')"
                ), {"k": kind})
        with pytest.raises(IntegrityError, match="ck_mcp_server_secrets_kind"):
            with engine.begin() as conn:
                conn.execute(text("UPDATE mcp_server_secrets SET kind = 'x' WHERE server_id = 'm1'"))

        # deleting a server deletes its runtime
        with engine.begin() as conn:
            _server(conn, "m2", "t1", "managed2", None, "managed")
            conn.execute(text(RUNTIME_SQL), {"id": str(uuid.uuid4()), "t": "t1", "s": "m2"})
            conn.execute(text("DELETE FROM mcp_servers WHERE id = 'm2'"))
            assert conn.execute(text("SELECT count(*) FROM mcp_runtimes")).scalar_one() == 1

        # catalog: defaults and the lower(name) / status indexes exist
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO mcp_catalog_entries (name, version, fetched_at) VALUES ('io.x/y', '1', now())"))
            assert conn.execute(text("SELECT status, is_latest, hidden, packages, remotes FROM mcp_catalog_entries")).one() == (
                "active", True, False, [], [])
            idx = set(conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'mcp_catalog_entries' AND schemaname = current_schema()"
            )).scalars())
            assert {"ix_mcp_catalog_entries_latest_status", "ix_mcp_catalog_entries_name_lower"} <= idx

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0022_parity_round_2")
        with engine.connect() as conn:
            assert {"mcp_catalog_entries", "mcp_runtimes"}.isdisjoint(_tables(conn))
            assert "runtime" not in _columns(conn, "mcp_servers") and "kind" not in _columns(conn, "mcp_server_secrets")
            assert conn.execute(text("SELECT id FROM mcp_servers")).scalars().all() == ["old"]
            assert conn.execute(text("SELECT count(*) FROM mcp_server_secrets")).scalar_one() == 1
        with pytest.raises(IntegrityError):  # url NOT NULL again
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO mcp_servers (id, tenant_id, name, created_at, updated_at) VALUES ('n', 't', 'n', now(), now())"
                ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        with engine.connect() as conn:
            assert schema_diff(conn) == []
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
