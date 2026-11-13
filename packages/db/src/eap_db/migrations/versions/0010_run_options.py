"""Run options: per-run request context and draft test runs; agent-filtered conversation list index.

Revision ID: 0010_run_options
Revises: 0009_version_restored_from
Create Date: 2026-11-13 23:59:00

- `runs.request_context` (JSONB, NULL = no context): the validated request context of an agent run
  or turn (the declared fields only). The worker reads it in `eap.start_run`, so it travels on the
  row, not in the hand-off arguments. NULL for every run recorded before this revision.
- `runs.draft_spec` (JSONB): the agent draft snapshot a draft test run executes. A draft run has
  `agent_version` NULL and `draft_spec` set; a run of a published version has the opposite. A retry
  relaunches from this snapshot, never from the live draft.
- `ck_runs_kind_target` is widened accordingly: an agent run has exactly one of `agent_version` and
  `draft_spec`; tool and workflow runs have neither. Every existing row satisfies it (the column is
  new and NULL, and existing agent runs all have a version).
- `ix_conversations_tenant_agent_updated` (tenant_id, agent_id, updated_at DESC, id DESC): one
  agent's conversations, most recently active first (GET /conversations?agent_id=, the Studio
  playground rail), in the list's exact order so paging is an index range scan.

Downgrade deletes draft runs (test runs that have no published version to fall back to) with their
events, usage rows and side_effects reservations, restores the 0007 check, then drops the columns
(request context values are lost) and the index.

Run the downgrade only with the worker queue drained (no queued or running agent workflows): a
draft workflow still in flight would otherwise be revived by the old worker's `eap.start_run`
safety net, which re-inserts the run as a published-version run with agent_version=0. Deleting
draft conversation turns leaves gaps in those conversations' turn numbering (the turn numbers of
the remaining turns are not renumbered).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010_run_options"
down_revision: Union[str, Sequence[str], None] = "0009_version_restored_from"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same text as eap_run_store.store.RUN_KIND_TARGET_CHECK (a migration never imports app code).
_KIND_TARGET_CHECK = (
    "(kind = 'agent' AND agent_id IS NOT NULL AND (agent_version IS NULL) <> (draft_spec IS NULL)"
    " AND tool_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'tool' AND tool_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND draft_spec IS NULL AND conversation_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND workflow_version IS NOT NULL"
    " AND agent_id IS NULL AND agent_version IS NULL AND draft_spec IS NULL AND tool_id IS NULL"
    " AND conversation_id IS NULL)"
)
# 0007's check, restored by the downgrade.
_KIND_TARGET_CHECK_0007 = (
    "(kind = 'agent' AND agent_id IS NOT NULL AND agent_version IS NOT NULL AND tool_id IS NULL"
    " AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'tool' AND tool_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND conversation_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND workflow_version IS NOT NULL"
    " AND agent_id IS NULL AND agent_version IS NULL AND tool_id IS NULL AND conversation_id IS NULL)"
)


def upgrade() -> None:
    op.add_column("runs", sa.Column("request_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("runs", sa.Column("draft_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK)
    op.create_index(
        "ix_conversations_tenant_agent_updated",
        "conversations",
        ["tenant_id", "agent_id", sa.text("updated_at DESC"), sa.text("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_tenant_agent_updated", table_name="conversations")
    # Doomed runs: draft test runs. chr(58) = ':' (no bind-param parsing).
    doomed = "SELECT id, tenant_id FROM runs WHERE draft_spec IS NOT NULL"
    op.execute(
        f"DELETE FROM side_effects s USING ({doomed}) r"
        " WHERE s.tenant_id = r.tenant_id AND left(s.key, length(r.id) + 1) = r.id || chr(58)"
    )
    op.execute(f"DELETE FROM run_model_requests WHERE run_id IN (SELECT id FROM ({doomed}) d)")
    op.execute(f"DELETE FROM execution_events WHERE run_id IN (SELECT id FROM ({doomed}) d)")
    op.execute("DELETE FROM runs WHERE draft_spec IS NOT NULL")
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK_0007)
    op.drop_column("runs", "draft_spec")
    op.drop_column("runs", "request_context")
