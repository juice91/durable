"""Architect slug ids on agents and workflows.

Revision ID: 0026_architect_slugs
Revises: 0025_architect
Create Date: 2026-11-18 11:00:00

- `agents.slug TEXT NULL` and `workflows.slug TEXT NULL`: the short id (`^[a-z][a-z0-9-]{1,47}$`) the
  Architect and its page use for a definition they made; NULL for every hand-made one. The row id
  (a UUID) stays the primary key and never appears in the Architect conversation. A slug is unique
  per tenant (partial unique index, NULLs excluded), so the slug also marks "made by the Architect".

Lock footprint: two nullable ADD COLUMNs (no rewrite) and two indexes over tables with no slug yet
(every value is NULL, so the partial indexes are empty).

Downgrade drops the indexes and the columns (the slugs are lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0026_architect_slugs"
down_revision: Union[str, Sequence[str], None] = "0025_architect"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("agents", "workflows"):
        op.add_column(table, sa.Column("slug", sa.String(), nullable=True))
        op.create_index(
            f"uq_{table}_tenant_slug", table, ["tenant_id", "slug"], unique=True,
            postgresql_where=sa.text("slug IS NOT NULL"),
        )


def downgrade() -> None:
    for table in ("workflows", "agents"):
        op.drop_index(f"uq_{table}_tenant_slug", table_name=table)
        op.drop_column(table, "slug")
