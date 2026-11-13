"""Version restore provenance: `agent_versions.restored_from`.

Revision ID: 0009_version_restored_from
Revises: 0008_run_usage
Create Date: 2026-11-13 23:30:00

Rollback is a restore: `POST /agents/{id}/versions/{v}/restore` publishes a NEW version whose spec
equals version v, and records v in `restored_from`. Version rows are never updated or deleted, so
history stays intact. NULL for every ordinary publish and for all versions recorded before this
revision (no backfill needed: none of them were restores).

Composite FK (agent_id, restored_from) -> agent_versions (agent_id, version) via `uq_agent_version`:
a version can only name an existing version of the same agent as its source. MATCH SIMPLE, so a
NULL `restored_from` is not checked.

Downgrade drops the FK and the column (provenance is lost; the restored versions themselves stay).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009_version_restored_from"
down_revision: Union[str, Sequence[str], None] = "0008_run_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_versions", sa.Column("restored_from", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_agent_versions_restored_from",
        "agent_versions",
        "agent_versions",
        ["agent_id", "restored_from"],
        ["agent_id", "version"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_agent_versions_restored_from", "agent_versions", type_="foreignkey")
    op.drop_column("agent_versions", "restored_from")
