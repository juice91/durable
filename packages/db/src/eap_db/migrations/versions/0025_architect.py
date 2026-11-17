"""Architect: builtin agents and the created_via marker on versions.

Revision ID: 0025_architect
Revises: 0023_attachments_r2
Create Date: 2026-11-18 09:00:00

- `agents.builtin BOOLEAN NOT NULL DEFAULT false`: a platform-seeded agent (the Architect and its
  drafter). The API refuses to archive one and refuses `architect.*` tool ids on any other agent.
- `agent_versions.created_via TEXT NULL` and `workflow_versions.created_via TEXT NULL`: who made the
  version, `architect:<run_id>` (the Architect agent in a run) or `architect-page` (the Architect
  page); NULL for every hand-made version. It lives on the row, not in the spec, so spec hashes
  do not change.

Lock footprint: three ADD COLUMNs; the constant default of `builtin` and the nullable columns need no
table rewrite.

Downgrade drops the three columns (the marker and the flag are lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0025_architect"
down_revision: Union[str, Sequence[str], None] = "0023_attachments_r2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("agent_versions", sa.Column("created_via", sa.Text(), nullable=True))
    op.add_column("workflow_versions", sa.Column("created_via", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_versions", "created_via")
    op.drop_column("agent_versions", "created_via")
    op.drop_column("agents", "builtin")
