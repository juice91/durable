"""Index for listing a tenant's runs newest first (GET /api/v1/runs).

Revision ID: 0003
Revises: c689c50ff203
Create Date: 2026-11-13 00:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "c689c50ff203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_runs_tenant_created", "runs", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_runs_tenant_created", table_name="runs")
