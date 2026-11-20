"""revision: the optimistic-concurrency token of the definition-class resources.

Revision ID: 0033_revisions
Revises: 0032_external_spans
Create Date: 2026-11-19 18:00:00

`revision INTEGER NOT NULL DEFAULT 1` on agents, workflows, workflow_schedules, eval_suites,
eval_cases, settings_connections, kb_sources, scorers and mcp_servers. Every write that changes one
of these rows adds 1 (eap_run_store.revisions); the API serves it as the `ETag` and refuses a write
whose `If-Match` is stale. A constant default is a catalog-only change in Postgres 11+ (no rewrite).

Downgrade: drops the columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033_revisions"
down_revision: Union[str, Sequence[str], None] = "0032_external_spans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = (
    "agents", "workflows", "workflow_schedules", "eval_suites", "eval_cases", "settings_connections",
    "kb_sources", "scorers", "mcp_servers",
)


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_column(table, "revision")
