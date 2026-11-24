"""System-aware Architect, phase C: content-addressed capability snapshots.

Revision ID: 0038_capability_snapshots
Revises: 0037_definition_provenance
Create Date: 2026-11-23 12:00:01

`capability_snapshots(hash PK, body JSONB, created_at)`: what the registry looked like to a principal when the
Architect built its context, trimmed to entry ids with their status and a short content hash (no schemas, no
descriptions, no tenant text), so "what did the Architect see" can be reconstructed after the registry changes. One
row per hash, shared by every run that saw it; pruned by retention (default 400 days).

Expand-only (a new table). Downgrade drops it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_capability_snapshots"
down_revision: Union[str, Sequence[str], None] = "0037_definition_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "capability_snapshots",
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("hash", name="capability_snapshots_pkey"),
    )
    op.create_index("ix_capability_snapshots_created_at", "capability_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_capability_snapshots_created_at", table_name="capability_snapshots")
    op.drop_table("capability_snapshots")
