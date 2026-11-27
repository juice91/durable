"""Agent-bound grants: the start-time grant set of a run.

Revision ID: 0040_run_grant_set
Revises: 0039_api_key_owner
Create Date: 2026-11-26 09:00:00

`runs.grant_set` holds what the run may use, computed once when it starts (docs/iam.md, "Agent and workflow grants"):
`{"enforced": true, "subject": {"kind", "id", "name"}, "mcp_servers": [<server id>...], "secrets": [<name>...],
"unavailable": false}`. NULL means "not enforced": every run from before, and every run while enforcement is off
(`EAP_AGENT_GRANTS_ENFORCE` unset or the policy engine keeps no grants). The set is written once and never changed, so a
revoke reaches the next run and not one in flight.

Expand-only (one nullable column). Downgrade drops it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_run_grant_set"
down_revision: Union[str, Sequence[str], None] = "0039_api_key_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("grant_set", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "grant_set")
