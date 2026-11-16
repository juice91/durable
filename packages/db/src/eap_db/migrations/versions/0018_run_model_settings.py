"""Per-run model settings overrides: `runs.model_settings`.

Revision ID: 0018_run_model_settings
Revises: 0017_api_keys
Create Date: 2026-11-16 10:00:00

A run start may carry `model_settings` (temperature, top_p, top_k, penalties, seed, max_tokens,
max_steps, max_retries). The API stores the fields the caller set, as a JSONB object (NULL = no
override), and the worker merges them over the spec's model settings.

Lock footprint: one nullable column, no default, no rewrite.

Downgrade: the overrides are lost; runs launched afterwards use the spec's settings.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0018_run_model_settings"
down_revision: Union[str, Sequence[str], None] = "0017_api_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("model_settings", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "model_settings")
