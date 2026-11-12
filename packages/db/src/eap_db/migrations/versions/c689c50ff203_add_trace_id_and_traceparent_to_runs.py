"""add trace_id and traceparent to runs

The creating API request's trace context: the worker parents the run span on `traceparent`, the
Studio links to the trace by `trace_id`. Nullable (NULL when telemetry is off); no row is rewritten.

Revision ID: c689c50ff203
Revises: 0002
Create Date: 2026-11-12 03:54:33.888204+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c689c50ff203'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('trace_id', sa.String(), nullable=True))
    op.add_column('runs', sa.Column('traceparent', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('runs', 'traceparent')
    op.drop_column('runs', 'trace_id')
