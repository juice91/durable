"""Tool runs: a run can execute one platform tool call instead of an agent version.

Revision ID: 0006_tool_runs
Revises: 0005_workflows
Create Date: 2026-11-13 18:00:00

- `runs.kind` ('agent' | 'tool'), NOT NULL with server default 'agent': every existing row becomes
  an agent run, unchanged otherwise.
- `runs.tool_id`: the tool a tool run calls (NULL for agent runs).
- `runs.agent_id` and `runs.agent_version` become nullable (NULL for tool runs).
- `ck_runs_kind_target`: an agent run has agent_id + agent_version and no tool_id; a tool run has
  tool_id, no agent and no conversation. Every existing row satisfies it (both agent columns were
  NOT NULL and tool_id is new).
- `ix_runs_tenant_kind_created` (tenant_id, kind, created_at) and `ix_runs_tenant_tool_created`
  (tenant_id, tool_id, created_at): GET /runs?kind= and ?tool_id=, newest first.

Downgrade deletes tool runs, their events and their side_effects reservations (`{run_id}:tool`,
`{run_id}:tool_run`, same tenant), so a later re-upgrade cannot hand a reused client run_id an old
completed result or an old `reserved` state; then it restores NOT NULL on the agent columns. The
fake external ticket service's rows are left alone: they stand for an external system.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_tool_runs"
down_revision: Union[str, Sequence[str], None] = "0005_workflows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same text as eap_run_store.store.RUN_KIND_TARGET_CHECK (a migration never imports app code).
_KIND_TARGET_CHECK = (
    "(kind = 'agent' AND agent_id IS NOT NULL AND agent_version IS NOT NULL AND tool_id IS NULL)"
    " OR (kind = 'tool' AND tool_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND conversation_id IS NULL)"
)


def upgrade() -> None:
    op.add_column("runs", sa.Column("kind", sa.String(), server_default=sa.text("'agent'"), nullable=False))
    op.add_column("runs", sa.Column("tool_id", sa.String(), nullable=True))
    op.alter_column("runs", "agent_id", existing_type=sa.String(), nullable=True)
    op.alter_column("runs", "agent_version", existing_type=sa.Integer(), nullable=True)
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK)
    op.create_index("ix_runs_tenant_kind_created", "runs", ["tenant_id", "kind", "created_at"], unique=False)
    op.create_index("ix_runs_tenant_tool_created", "runs", ["tenant_id", "tool_id", "created_at"], unique=False)


def downgrade() -> None:
    # if_exists: 0006 was amended in place to add these indexes; a database migrated by the first
    # version of 0006 has the revision but not the indexes, and must still downgrade cleanly.
    op.drop_index("ix_runs_tenant_tool_created", table_name="runs", if_exists=True)
    op.drop_index("ix_runs_tenant_kind_created", table_name="runs", if_exists=True)
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.execute(
        "DELETE FROM side_effects s USING runs r WHERE r.kind <> 'agent' AND s.tenant_id = r.tenant_id"
        " AND s.key IN (r.id || chr(58) || 'tool', r.id || chr(58) || 'tool_run')"  # chr(58) = ':' (no bind-param parsing)
    )
    op.execute("DELETE FROM execution_events WHERE run_id IN (SELECT id FROM runs WHERE kind <> 'agent')")
    op.execute("DELETE FROM runs WHERE kind <> 'agent'")
    op.alter_column("runs", "agent_version", existing_type=sa.Integer(), nullable=False)
    op.alter_column("runs", "agent_id", existing_type=sa.String(), nullable=False)
    op.drop_column("runs", "tool_id")
    op.drop_column("runs", "kind")
