"""MCP catalog source: `mcp_catalog_entries.source` and `mcp_catalog_entries.meta`.

Revision ID: 0028_mcp_catalog_source
Revises: 0027_tenant_secrets
Create Date: 2026-11-18 15:00:00

The catalog mirrors two registries now: the official MCP registry and ToolHive's curated one.

- `source` ('official' | 'toolhive', default 'official'): which registry an entry came from; each
  mirror run only removes its own source's rows.
- `meta` (jsonb, nullable): the publisher-provided block we keep for ToolHive entries (tier, tags,
  network permissions, tool names and count, stars). Null for official entries.

Existing rows: every entry becomes `source = 'official'`, `meta = NULL`.

Lock footprint: two ADD COLUMNs (a constant default and a nullable column, no table rewrite) and a
small index on a ~7000-row table.

Downgrade: ToolHive entries are deleted (they have no place in 0027's shape); the columns are dropped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0028_mcp_catalog_source"
down_revision: Union[str, Sequence[str], None] = "0027_tenant_secrets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "mcp_catalog_entries", sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'official'"))
    )
    op.add_column("mcp_catalog_entries", sa.Column("meta", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_mcp_catalog_entries_source", "mcp_catalog_entries", "source IN ('official', 'toolhive')"
    )
    op.create_index("ix_mcp_catalog_entries_source", "mcp_catalog_entries", ["source", "status"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM mcp_catalog_entries WHERE source <> 'official'")
    op.drop_index("ix_mcp_catalog_entries_source", table_name="mcp_catalog_entries")
    op.drop_constraint("ck_mcp_catalog_entries_source", "mcp_catalog_entries", type_="check")
    op.drop_column("mcp_catalog_entries", "meta")
    op.drop_column("mcp_catalog_entries", "source")
