"""mcp_servers.keep_warm: a managed server the idle sweep never stops.

Revision ID: 0035_mcp_servers_keep_warm
Revises: 0034_workflow_explanations
Create Date: 2026-11-20 15:00:00

`keep_warm` (boolean, default false): when true the runtime sweep (eap_worker.mcp_runtime_sweep)
skips the server's runtime however long it has been idle.

Downgrade: drops the column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035_mcp_servers_keep_warm"
down_revision: Union[str, Sequence[str], None] = "0034_workflow_explanations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("keep_warm", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("mcp_servers", "keep_warm")
