"""workflows.archived_at: when the workflow was archived.

Revision ID: 0031_workflow_archived_at
Revises: 0030_kb_source_kinds
Create Date: 2026-11-19 12:00:00

A nullable timestamptz (no table rewrite). Workflows archived before this revision get their
`updated_at` (the archive was the last write that matters) so the Studio can show a date.

Downgrade: drops the column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_workflow_archived_at"
down_revision: Union[str, Sequence[str], None] = "0030_kb_source_kinds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workflows", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE workflows SET archived_at = updated_at WHERE archived IS TRUE")


def downgrade() -> None:
    op.drop_column("workflows", "archived_at")
