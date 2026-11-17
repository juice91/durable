"""Attachments round 2: chat attachment bytes move from Postgres to the BlobStore.

Revision ID: 0023_attachments_r2
Revises: 0022_parity_round_2
Create Date: 2026-11-17 12:00:00

- `chat_attachments.storage_key TEXT` is added, every existing row's bytes are written to the
  BlobStore (eap_blob_store, the backend `EAP_BLOB_BACKEND` selects) under
  `attachments/{tenant_id}/{id}` and that key is recorded, then the column is set NOT NULL and
  `data` is dropped, all in this one migration transaction. The backfill reads and writes row by
  row (one blob in memory at a time). It is idempotent: a rerun after a failure overwrites the same
  keys.
- The backfill REFUSES to run when rows exist and `EAP_BLOB_BACKEND` is unset (an unset backend
  would silently mean "a local directory", which a deployment never wants): the error names the
  variables to set. With no rows the migration needs no backend.
- `chat_attachments.run_id TEXT NULL`: the native run whose message linked the upload, so an API key
  reads a linked upload only through a run of an agent it may use (not by a message id that two
  agents' runs can share). Rows linked before it existed have no run and stay unreadable to a key
  that did not upload them (a key's own uploads and the dev bearer are unaffected).
- `ix_chat_attachments_unsent_created_at` ON (created_at) WHERE message_id IS NULL: the scan of
  unsent uploads (the retention pass).

Lock footprint: one ADD COLUMN (no default, no rewrite), the backfill's per-row UPDATEs, one SET NOT
NULL (a scan of the table) and a DROP COLUMN (catalog only); the table holds only chat uploads.

Downgrade: re-adds a nullable `data` column WITHOUT restoring the bytes (they stay in the
BlobStore; rows are kept with `data` NULL, so their downloads fail after a downgrade) and drops
`storage_key`, `run_id` and the partial index.
"""
import asyncio
import concurrent.futures
import os
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0023_attachments_r2"
down_revision: Union[str, Sequence[str], None] = "0022_parity_round_2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "ix_chat_attachments_unsent_created_at"


def _run(coro: Any) -> Any:
    """Run a coroutine to completion from sync migration code, even inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def _one_chunk(data: bytes):
    yield data


def _backfill(conn: sa.Connection) -> None:
    rows = conn.execute(sa.text("SELECT id::text, tenant_id FROM chat_attachments ORDER BY created_at, id")).all()
    if not rows:
        return
    if not os.environ.get("EAP_BLOB_BACKEND", "").strip():
        raise RuntimeError(
            f"0023_attachments_r2: chat_attachments has {len(rows)} row(s) whose bytes must move to object "
            "storage, but EAP_BLOB_BACKEND is not set. Set EAP_BLOB_BACKEND=fs (with EAP_BLOB_DIR) or "
            "EAP_BLOB_BACKEND=s3 (with EAP_BLOB_S3_ENDPOINT, EAP_BLOB_S3_BUCKET, EAP_BLOB_S3_REGION, "
            "EAP_BLOB_S3_ACCESS_KEY, EAP_BLOB_S3_SECRET_KEY) and run the migration again."
        )
    from eap_blob_store import attachment_key, blob_store_from_env

    store = blob_store_from_env()
    for attachment_id, tenant_id in rows:
        media_type, data = conn.execute(
            sa.text("SELECT media_type, data FROM chat_attachments WHERE id = CAST(:i AS uuid)"),
            {"i": attachment_id},
        ).one()
        data = bytes(data)
        key = attachment_key(tenant_id, attachment_id)
        _run(store.put(key, _one_chunk(data), len(data), media_type))
        conn.execute(
            sa.text("UPDATE chat_attachments SET storage_key = :k WHERE id = CAST(:i AS uuid)"),
            {"k": key, "i": attachment_id},
        )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("chat_attachments", sa.Column("storage_key", sa.Text(), nullable=True))
    op.add_column("chat_attachments", sa.Column("run_id", sa.Text(), nullable=True))
    _backfill(op.get_bind())
    op.alter_column("chat_attachments", "storage_key", existing_type=sa.Text(), nullable=False)
    op.drop_column("chat_attachments", "data")
    op.create_index(
        _INDEX, "chat_attachments", ["created_at"], unique=False, postgresql_where=sa.text("message_id IS NULL")
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index(_INDEX, table_name="chat_attachments")
    op.add_column("chat_attachments", sa.Column("data", sa.LargeBinary(), nullable=True))
    op.drop_column("chat_attachments", "storage_key")
    op.drop_column("chat_attachments", "run_id")
