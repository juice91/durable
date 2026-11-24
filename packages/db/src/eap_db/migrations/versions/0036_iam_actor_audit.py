"""IAM P0b: the real actor everywhere, and the audit log.

Revision ID: 0036_iam_actor_audit
Revises: 0035_mcp_servers_keep_warm
Create Date: 2026-11-23 10:00:00

- `audit_log` (core table, design section 6): one row per mutating call: actor, action, resource, outcome, request id.
  Append-only (a trigger refuses UPDATE; retention deletes whole old rows). Indexes `(tenant_id, at)` and
  `(tenant_id, resource_kind, resource_id, at)`.
- `runs.principal`, `runs.on_behalf_of`, `runs.started_by_kind`: who a run acts as (design 4.7). NULL reads as legacy.
- `agent_versions.published_by`, `workflow_versions.published_by`: who published or restored the version. NULL is legacy.

Expand-only: nullable columns, nothing backfilled (a NULL means "unknown (legacy)").

Downgrade: drops the table, the trigger and the columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036_iam_actor_audit"
down_revision: Union[str, Sequence[str], None] = "0035_mcp_servers_keep_warm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("actor_kind", sa.String(), nullable=False),
        sa.Column("actor_subject", sa.String(), nullable=False),
        sa.Column("on_behalf_of", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource_kind", sa.String(), nullable=True),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id", name="audit_log_pkey"),
        sa.CheckConstraint("outcome IN ('success', 'denied', 'failed')", name="ck_audit_log_outcome"),
    )
    op.create_index("ix_audit_log_tenant_at", "audit_log", ["tenant_id", "at"])
    op.create_index(
        "ix_audit_log_tenant_resource_at", "audit_log", ["tenant_id", "resource_kind", "resource_id", "at"]
    )
    op.execute(
        """
        CREATE FUNCTION audit_log_refuse_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_append_only BEFORE UPDATE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_log_refuse_update()"
    )
    op.add_column("runs", sa.Column("principal", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("on_behalf_of", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("started_by_kind", sa.Text(), nullable=True))
    op.add_column("agent_versions", sa.Column("published_by", sa.Text(), nullable=True))
    op.add_column("workflow_versions", sa.Column("published_by", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_versions", "published_by")
    op.drop_column("agent_versions", "published_by")
    op.drop_column("runs", "started_by_kind")
    op.drop_column("runs", "on_behalf_of")
    op.drop_column("runs", "principal")
    op.execute("DROP TRIGGER audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION audit_log_refuse_update()")
    op.drop_index("ix_audit_log_tenant_resource_at", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_at", table_name="audit_log")
    op.drop_table("audit_log")
