"""Workflow definitions: tenant-owned workflow drafts and their immutable published versions.

Revision ID: 0005_workflows
Revises: 0004_mcp_servers
Create Date: 2026-11-13 15:00:00

- `workflows` (id, tenant_id, name, draft_spec JSONB, archived, created_at, updated_at), like
  `agents`. UNIQUE (tenant_id, id) so versions reference it per tenant.
- `workflow_versions` (id, workflow_id, tenant_id, version, spec JSONB, spec_hash, created_at,
  created_by), like `agent_versions`. UNIQUE (workflow_id, version); the composite foreign key
  (tenant_id, workflow_id) -> workflows(tenant_id, id) means a version can only belong to a
  workflow of its own tenant.

Only new tables: no existing row is touched. Models: apps/api/src/eap_api/workflows/models.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_workflows"
down_revision: Union[str, Sequence[str], None] = "0004_mcp_servers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("draft_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="workflows_pkey"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_workflows_tenant_id_id"),
    )
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("spec_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
            name="fk_workflow_versions_tenant_workflow",
        ),
        sa.PrimaryKeyConstraint("id", name="workflow_versions_pkey"),
        sa.UniqueConstraint("workflow_id", "version", name="uq_workflow_version"),
    )


def downgrade() -> None:
    op.drop_table("workflow_versions")
    op.drop_table("workflows")
