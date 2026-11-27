"""The roles a run's person held when it started.

Revision ID: 0041_run_actor_roles
Revises: 0040_run_grant_set
Create Date: 2026-11-25 12:00:00

`runs.actor_roles` holds the tenant roles (a JSON array of names, possibly empty) of the person who started the run, as
their identity provider named them; a child run copies its parent's. The worker asks the policy engine with them whether
that person may write: a built-in Architect run of one who may not gets read tools only (docs/iam.md, "Viewers and the
Architect"). NULL means "not started by a person": API keys, schedules, services and every run from before.

Expand-only (one nullable column). Downgrade drops it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041_run_actor_roles"
down_revision: Union[str, Sequence[str], None] = "0040_run_grant_set"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("actor_roles", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "actor_roles")
