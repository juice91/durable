"""0023_attachments_r2: chat attachment bytes move from Postgres to the BlobStore.

Adds `storage_key`, backfills existing rows' bytes into the store (refusing when rows exist and no
backend is configured), drops `data`, and adds the partial index of unsent uploads.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from eap_blob_store import FsBlobStore, attachment_key
from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
ID_A, ID_B = str(uuid.uuid4()), str(uuid.uuid4())
INSERT = (
    "INSERT INTO chat_attachments (id, tenant_id, name, media_type, size, sha256, data, message_id, created_by)"
    " VALUES (:id, :t, :n, :m, :s, :h, :d, :msg, 'alice')"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _columns(conn) -> dict[str, tuple[str, str]]:
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute(text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_name = 'chat_attachments' AND table_schema = current_schema()"
        )).all()
    }


@pytest.fixture
def schema_engine():
    base = _base_url()
    schema = f"eap_test_0023_{uuid.uuid4().hex[:8]}"
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


def _upgrade(engine, revision):
    from alembic import command

    from eap_db import alembic_config

    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), revision)


def _seed(engine):
    _upgrade(engine, "0022_parity_round_2")
    with engine.begin() as conn:
        conn.execute(text(INSERT), {"id": ID_A, "t": "t1", "n": "a.png", "m": "image/png", "s": len(PNG),
                                    "h": hashlib.sha256(PNG).hexdigest(), "d": PNG, "msg": "m1"})
        conn.execute(text(INSERT), {"id": ID_B, "t": "t2", "n": "n.txt", "m": "text/plain", "s": 5,
                                    "h": hashlib.sha256(b"notes").hexdigest(), "d": b"notes", "msg": None})


def _is_0025(diff) -> bool:
    return any(name in repr(diff) for name in ("'revision'", "'builtin'", "'created_via'", "'slug'", "'archived_at'", "ix_spans_external", "ck_spans_source", "<spans>", "_tenant_slug", "tenant_secrets", "workflow_explanations", "mcp_catalog_entries", "settings_connections", "kb_sources", "kb_source_syncs", "kb_source_records", "kb_entit", "'keep_warm'"))


def test_backfill_moves_bytes_into_the_store_and_drops_the_column(schema_engine, monkeypatch, tmp_path):
    _seed(schema_engine)
    monkeypatch.setenv("EAP_BLOB_BACKEND", "fs")
    monkeypatch.setenv("EAP_BLOB_DIR", str(tmp_path))
    _upgrade(schema_engine, "0023_attachments_r2")
    with schema_engine.connect() as conn:
        cols = _columns(conn)
        assert "data" not in cols
        assert cols["storage_key"] == ("text", "NO")
        keys = dict(conn.execute(text("SELECT id::text, storage_key FROM chat_attachments")).all())
        assert keys == {ID_A: attachment_key("t1", ID_A), ID_B: attachment_key("t2", ID_B)}
        indexdef = dict(conn.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chat_attachments'"
            " AND schemaname = current_schema()"
        )).all())["ix_chat_attachments_unsent_created_at"]
        assert "(created_at)" in indexdef and "message_id IS NULL" in indexdef
        # At 0023 the models already carry 0025's builtin and created_via columns; nothing else differs.
        assert [d for d in schema_diff(conn) if not _is_0025(d)] == []
    store = FsBlobStore(tmp_path)

    async def read(key):
        return b"".join([c async for c in store.open(key)])

    assert asyncio.run(read(attachment_key("t1", ID_A))) == PNG
    assert asyncio.run(read(attachment_key("t2", ID_B))) == b"notes"


def test_rows_without_a_configured_backend_refuse_to_migrate(schema_engine, monkeypatch, tmp_path):
    _seed(schema_engine)
    monkeypatch.delenv("EAP_BLOB_BACKEND", raising=False)
    monkeypatch.setenv("EAP_BLOB_DIR", str(tmp_path))
    with pytest.raises(Exception, match="EAP_BLOB_BACKEND"):
        _upgrade(schema_engine, "0023_attachments_r2")
    with schema_engine.connect() as conn:  # nothing changed: the bytes are still in the rows
        assert "data" in _columns(conn) and "storage_key" not in _columns(conn)
        assert conn.execute(text("SELECT count(*) FROM chat_attachments")).scalar_one() == 2
    assert list(tmp_path.iterdir()) == []


def test_an_empty_table_migrates_without_a_backend(schema_engine, monkeypatch):
    _upgrade(schema_engine, "0022_parity_round_2")
    monkeypatch.delenv("EAP_BLOB_BACKEND", raising=False)
    _upgrade(schema_engine, "head")
    with schema_engine.connect() as conn:
        assert _columns(conn)["storage_key"] == ("text", "NO") and "data" not in _columns(conn)
        assert schema_diff(conn) == []


def test_downgrade_re_adds_a_nullable_data_column_without_the_bytes(schema_engine, monkeypatch, tmp_path):
    from alembic import command

    from eap_db import alembic_config

    _seed(schema_engine)
    monkeypatch.setenv("EAP_BLOB_BACKEND", "fs")
    monkeypatch.setenv("EAP_BLOB_DIR", str(tmp_path))
    _upgrade(schema_engine, "0023_attachments_r2")
    with schema_engine.begin() as conn:
        command.downgrade(alembic_config(conn), "0024_mcp_runtime")  # the chain is 0022 -> 0024 -> 0023
    with schema_engine.connect() as conn:
        cols = _columns(conn)
        assert cols["data"] == ("bytea", "YES") and "storage_key" not in cols
        assert conn.execute(text("SELECT count(*) FROM chat_attachments WHERE data IS NULL")).scalar_one() == 2
        assert "ix_chat_attachments_unsent_created_at" not in set(conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'chat_attachments' AND schemaname = current_schema()"
        )).scalars())
