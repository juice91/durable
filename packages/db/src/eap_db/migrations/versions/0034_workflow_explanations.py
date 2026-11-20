"""workflow_explanations: the AI explanation of a published workflow version, cached per locale.

Revision ID: 0034_workflow_explanations
Revises: 0033_revisions
Create Date: 2026-11-20 12:00:00

One row per (tenant, workflow, version, locale): `body` is the validated explainer answer
(summary, inputs, outcomes, steps), `model` the model that wrote it. A version is immutable, so the row
never goes stale; a refresh replaces it. The composite FK to workflows deletes the rows with the workflow.

Downgrade: drops the table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_workflow_explanations"
down_revision: Union[str, Sequence[str], None] = "0033_revisions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_explanations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("locale", sa.String(), nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="workflow_explanations_pkey"),
        sa.UniqueConstraint("tenant_id", "workflow_id", "version", "locale", name="uq_workflow_explanations_version_locale"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"], ["workflows.tenant_id", "workflows.id"],
            name="fk_workflow_explanations_tenant_workflow", ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("workflow_explanations")
