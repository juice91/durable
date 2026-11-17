"""MCP local runtime: registry catalog mirror, managed runtimes, managed servers, env secrets.

Revision ID: 0024_mcp_runtime
Revises: 0022_parity_round_2
Create Date: 2026-11-17 14:00:00

(down_revision is 0022 because the attachments revision 0023 has not landed yet; the finish step
re-points it.)

- `mcp_catalog_entries`: a platform-wide, read-only mirror of the official MCP registry (no tenant).
  PK `name`; `packages` and `remotes` are the registry's JSON; `hidden` lets an operator hide an entry.
  Indexed `(is_latest, status)` for the listing and `lower(name)` for search.
- `mcp_runtimes`: one row per managed server (tenant-scoped): the chosen package, image, namespace,
  custom resource name, lifecycle `state`, the in-cluster `endpoint`, `last_error`, `last_used_at`.
  Unique `(tenant_id, server_id)`; composite FK `(tenant_id, server_id)` -> `mcp_servers` ON DELETE CASCADE.
- `mcp_servers.runtime` ('external' | 'managed', default 'external') and `url` nullable:
  `ck_mcp_servers_runtime_url` requires an external server to have a URL and a managed one to have none
  (its endpoint lives on `mcp_runtimes`).
- `mcp_server_secrets.kind` ('header' | 'env', default 'header'); the primary key widens to
  `(tenant_id, server_id, kind, header_name)`. `header_name` also holds an env variable's name.

Existing rows: every server becomes `runtime = 'external'`, every secret `kind = 'header'`.

Downgrade: managed servers (with their runtimes and secrets) and env secrets are deleted; the rest is
restored to 0022's shape.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0024_mcp_runtime"
down_revision: Union[str, Sequence[str], None] = "0022_parity_round_2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_catalog_entries",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("packages", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("remotes", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("licence", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.PrimaryKeyConstraint("name", name="mcp_catalog_entries_pkey"),
    )
    op.create_index("ix_mcp_catalog_entries_latest_status", "mcp_catalog_entries", ["is_latest", "status"])
    op.create_index("ix_mcp_catalog_entries_name_lower", "mcp_catalog_entries", [sa.text("lower(name)")])

    op.add_column(
        "mcp_servers", sa.Column("runtime", sa.Text(), nullable=False, server_default=sa.text("'external'"))
    )
    op.alter_column("mcp_servers", "url", existing_type=sa.Text(), nullable=True)
    op.create_check_constraint(
        "ck_mcp_servers_runtime", "mcp_servers", "runtime IN ('external', 'managed')"
    )
    op.create_check_constraint(
        "ck_mcp_servers_runtime_url", "mcp_servers", "(runtime = 'external') = (url IS NOT NULL)"
    )

    op.add_column(
        "mcp_server_secrets", sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'header'"))
    )
    op.create_check_constraint("ck_mcp_server_secrets_kind", "mcp_server_secrets", "kind IN ('header', 'env')")
    op.drop_constraint("mcp_server_secrets_pkey", "mcp_server_secrets", type_="primary")
    op.create_primary_key(
        "mcp_server_secrets_pkey", "mcp_server_secrets", ["tenant_id", "server_id", "kind", "header_name"]
    )

    op.create_table(
        "mcp_runtimes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("catalog_name", sa.Text(), nullable=True),
        sa.Column("package", postgresql.JSONB(), nullable=False),
        sa.Column("image", sa.Text(), nullable=False),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("cr_name", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="mcp_runtimes_pkey"),
        sa.UniqueConstraint("tenant_id", "server_id", name="uq_mcp_runtimes_tenant_server"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "server_id"],
            ["mcp_servers.tenant_id", "mcp_servers.id"],
            name="fk_mcp_runtimes_tenant_server",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'starting', 'running', 'idle', 'stopping', 'stopped', 'failed')",
            name="ck_mcp_runtimes_state",
        ),
    )
    op.create_index("ix_mcp_runtimes_state_last_used", "mcp_runtimes", ["state", "last_used_at"])


def downgrade() -> None:
    op.drop_table("mcp_runtimes")
    op.execute("DELETE FROM mcp_servers WHERE runtime = 'managed'")  # secrets cascade
    op.execute("DELETE FROM mcp_server_secrets WHERE kind = 'env'")
    op.drop_constraint("mcp_server_secrets_pkey", "mcp_server_secrets", type_="primary")
    op.create_primary_key("mcp_server_secrets_pkey", "mcp_server_secrets", ["tenant_id", "server_id", "header_name"])
    op.drop_constraint("ck_mcp_server_secrets_kind", "mcp_server_secrets", type_="check")
    op.drop_column("mcp_server_secrets", "kind")
    op.drop_constraint("ck_mcp_servers_runtime_url", "mcp_servers", type_="check")
    op.drop_constraint("ck_mcp_servers_runtime", "mcp_servers", type_="check")
    op.alter_column("mcp_servers", "url", existing_type=sa.Text(), nullable=False)
    op.drop_column("mcp_servers", "runtime")
    op.drop_table("mcp_catalog_entries")
