"""MCP server registry: tenant-owned remote MCP servers and their encrypted header secrets.

Revision ID: 0004_mcp_servers
Revises: 0003
Create Date: 2026-11-13 12:00:00

- `mcp_servers` (id, tenant_id, name, url, transport, enabled, created_at, updated_at,
  last_checked_at, last_status, last_error_code). UNIQUE (tenant_id, id) so secrets reference it per
  tenant; UNIQUE (tenant_id, name). `transport` is fixed to 'streamable_http' (no stdio servers);
  `last_status` is NULL (never tested), 'ok' or 'error', with a safe error code, never error text.
- `mcp_server_secrets` (tenant_id, server_id, header_name, ciphertext, key_id): one AES-GCM sealed
  header value per row (eap_secrets), keyed by (tenant_id, server_id, header_name). The composite
  foreign key (tenant_id, server_id) -> mcp_servers(tenant_id, id) means a secret can only belong to a
  server of its own tenant; deleting a server deletes its secrets.

Only new tables: no existing row is touched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_mcp_servers"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("transport", sa.String(), server_default=sa.text("'streamable_http'"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="mcp_servers_pkey"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_mcp_servers_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_mcp_servers_tenant_name"),
        sa.CheckConstraint("transport = 'streamable_http'", name="ck_mcp_servers_transport"),
        sa.CheckConstraint(
            "last_status IS NULL OR last_status IN ('ok', 'error')", name="ck_mcp_servers_last_status"
        ),
    )
    op.create_table(
        "mcp_server_secrets",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("header_name", sa.String(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "server_id"],
            ["mcp_servers.tenant_id", "mcp_servers.id"],
            name="fk_mcp_server_secrets_tenant_server",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "server_id", "header_name", name="mcp_server_secrets_pkey"),
    )


def downgrade() -> None:
    op.drop_table("mcp_server_secrets")
    op.drop_table("mcp_servers")
