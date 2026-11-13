"""Workflow runs: a run can execute a published workflow version; agent steps run as child runs.

Revision ID: 0007_workflow_runs
Revises: 0006_tool_runs
Create Date: 2026-11-13 21:00:00

- `runs.workflow_id`, `runs.workflow_version`: the published workflow version a run of kind
  'workflow' executes (NULL for agent and tool runs).
- `runs.parent_run_id`, `runs.parent_step_id`: an agent step of a workflow run executes as a child
  run of kind 'agent' (the unchanged `eap.agent_run` workflow) linked to its workflow run and step.
  Both NULL for every other run. Composite FK (tenant_id, parent_run_id) -> runs(tenant_id, id): a
  child always belongs to its parent's tenant.
- `ck_runs_kind_target` is widened: an agent run has its agent version and no tool or workflow; a
  tool run has its tool only; a workflow run has workflow_id + workflow_version, no agent, tool or
  conversation. Every existing row satisfies it (the workflow columns are new and NULL).
- `ck_runs_parent_link`: parent_run_id and parent_step_id are both set or both NULL, only on agent
  runs, never on a conversation turn.
- `ix_runs_tenant_workflow_created` (tenant_id, workflow_id, created_at): a workflow's run history,
  newest first. `ix_runs_parent_run` (parent_run_id): a workflow run's child runs.

Per-step state is not a table: it is derived from the run's durable events (`step:{id}:started`,
`...:waiting`, `...:decision`, `...:completed`, `...:failed`), which are idempotent per key and
gap-free, and which the Studio already streams.

Downgrade deletes workflow runs and their child agent runs, with their events and side_effects
reservations (keys `{run_id}:...` of those runs, same tenant), then restores the 0006 check and
drops the columns. The fake external ticket service's rows are left alone (an external system).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_workflow_runs"
down_revision: Union[str, Sequence[str], None] = "0006_tool_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same text as eap_run_store.store.RUN_KIND_TARGET_CHECK / RUN_PARENT_LINK_CHECK (a migration never
# imports app code).
_KIND_TARGET_CHECK = (
    "(kind = 'agent' AND agent_id IS NOT NULL AND agent_version IS NOT NULL AND tool_id IS NULL"
    " AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'tool' AND tool_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND conversation_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND workflow_version IS NOT NULL"
    " AND agent_id IS NULL AND agent_version IS NULL AND tool_id IS NULL AND conversation_id IS NULL)"
)
_PARENT_LINK_CHECK = (
    "(parent_run_id IS NULL AND parent_step_id IS NULL)"
    " OR (parent_run_id IS NOT NULL AND parent_step_id IS NOT NULL AND kind = 'agent' AND conversation_id IS NULL)"
)
# 0006's check, restored by the downgrade.
_KIND_TARGET_CHECK_0006 = (
    "(kind = 'agent' AND agent_id IS NOT NULL AND agent_version IS NOT NULL AND tool_id IS NULL)"
    " OR (kind = 'tool' AND tool_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND conversation_id IS NULL)"
)


def upgrade() -> None:
    op.add_column("runs", sa.Column("workflow_id", sa.String(), nullable=True))
    op.add_column("runs", sa.Column("workflow_version", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("parent_run_id", sa.String(), nullable=True))
    op.add_column("runs", sa.Column("parent_step_id", sa.String(), nullable=True))
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK)
    op.create_check_constraint("ck_runs_parent_link", "runs", _PARENT_LINK_CHECK)
    op.create_foreign_key(
        "fk_runs_tenant_parent_run", "runs", "runs", ["tenant_id", "parent_run_id"], ["tenant_id", "id"]
    )
    op.create_index("ix_runs_tenant_workflow_created", "runs", ["tenant_id", "workflow_id", "created_at"], unique=False)
    op.create_index("ix_runs_parent_run", "runs", ["parent_run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_parent_run", table_name="runs")
    op.drop_index("ix_runs_tenant_workflow_created", table_name="runs")
    # Doomed runs: workflow runs and their child agent runs. chr(58) = ':' (no bind-param parsing).
    doomed = "SELECT id, tenant_id FROM runs WHERE kind = 'workflow' OR parent_run_id IS NOT NULL"
    op.execute(
        f"DELETE FROM side_effects s USING ({doomed}) r"
        " WHERE s.tenant_id = r.tenant_id AND left(s.key, length(r.id) + 1) = r.id || chr(58)"
    )
    op.execute(f"DELETE FROM execution_events WHERE run_id IN (SELECT id FROM ({doomed}) d)")
    op.drop_constraint("fk_runs_tenant_parent_run", "runs", type_="foreignkey")
    op.execute("DELETE FROM runs WHERE kind = 'workflow' OR parent_run_id IS NOT NULL")
    op.drop_constraint("ck_runs_parent_link", "runs", type_="check")
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK_0006)
    op.drop_column("runs", "parent_step_id")
    op.drop_column("runs", "parent_run_id")
    op.drop_column("runs", "workflow_version")
    op.drop_column("runs", "workflow_id")
