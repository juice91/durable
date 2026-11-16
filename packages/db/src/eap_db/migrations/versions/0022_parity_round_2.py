"""Studio parity round 2: re-run link, schedules, step-by-step flag, workflow experiments, spans.

Revision ID: 0022_parity_round_2
Revises: 0021_chat_attachments
Create Date: 2026-11-17 09:00:00

- `runs.rerun_of` (UUID, NULL, indexed): the run a re-run repeats. `runs.schedule_id` (UUID, NULL,
  indexed): the schedule that started the run. `runs.pause_after_each_step` (BOOLEAN NOT NULL
  DEFAULT false): the workflow run pauses after each top-level step. No FKs: a run outlives the
  run it repeats and the schedule that started it as its audit record.
- `workflow_schedules`: a cron schedule that starts a workflow's latest published version.
  Unique `(tenant_id, workflow_id, name)`; `(active, next_fire_at)` indexed for the tick's due scan.
  `last_error` is set when the tick stops a schedule.
- `eval_suites.workflow_id` (NULL) and `eval_suites.agent_id` nullable: a suite targets exactly one
  of the two (`ck_eval_suites_one_target`); `uq_eval_suites_tenant_workflow_name` sits beside the
  agent one (NULLs never collide in a unique constraint, so each only constrains its own kind).
- `ck_runs_kind_target` is recreated: kind `eval` may carry `workflow_id` + `workflow_version`
  with `agent_id` NULL (a workflow suite's run). Every other rule is unchanged. Added NOT VALID
  (no second scan): the new text is the old one plus a branch, so existing rows satisfy it.
- `spans`: a flat, materialised span list filled when a run finishes. PK `(run_id, span_id)`;
  `(tenant_id, start DESC)` indexed. No backfill of older runs.

- `ck_runs_parent_link` is recreated too (NOT VALID, same reason): a parented run may be kind `agent` or
  `workflow`, so a workflow suite's case runs are children of their eval run (cancel, cost and tree
  queries follow `parent_run_id`).

Lock footprint: `runs` takes three ADD COLUMNs (constant default, no rewrite), two indexes and one
constraint swap; the other tables are new or small.

Downgrade: workflow schedules and spans are lost; workflow eval suites (with their cases) and
workflow eval runs (with their events, usage rows, side effects) are deleted; their child runs stay
as standalone runs (a workflow run's parent link is cleared). Agent suites and every other run keep their data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0022_parity_round_2"
down_revision: Union[str, Sequence[str], None] = "0021_chat_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.store.RUN_KIND_TARGET_CHECK (a migration never imports app code).
_KIND_TARGET_CHECK_0021 = (
    "(kind = 'agent' AND agent_id IS NOT NULL AND (agent_version IS NULL) <> (draft_spec IS NULL)"
    " AND tool_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL AND knowledge_base_id IS NULL)"
    " OR (kind = 'tool' AND tool_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND draft_spec IS NULL AND conversation_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND workflow_version IS NOT NULL"
    " AND agent_id IS NULL AND agent_version IS NULL AND draft_spec IS NULL AND tool_id IS NULL"
    " AND conversation_id IS NULL AND knowledge_base_id IS NULL)"
    " OR (kind = 'kb_ingest' AND knowledge_base_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND draft_spec IS NULL AND tool_id IS NULL AND conversation_id IS NULL AND workflow_id IS NULL"
    " AND workflow_version IS NULL)"
    " OR (kind = 'eval' AND agent_id IS NOT NULL AND (agent_version IS NULL) <> (draft_spec IS NULL)"
    " AND eval_suite_id IS NOT NULL AND tool_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL"
    " AND knowledge_base_id IS NULL AND conversation_id IS NULL)"
)
_KIND_TARGET_CHECK = (
    _KIND_TARGET_CHECK_0021
    + " OR (kind = 'eval' AND agent_id IS NULL AND agent_version IS NULL AND draft_spec IS NULL"
    " AND workflow_id IS NOT NULL AND workflow_version IS NOT NULL AND eval_suite_id IS NOT NULL"
    " AND tool_id IS NULL AND knowledge_base_id IS NULL AND conversation_id IS NULL)"
)
# 0007's parent-link check (only an agent run may have a parent), and the widened one: a workflow run
# may have one too (the child of a workflow suite's eval run; the parent is never itself a child).
_PARENT_LINK_CHECK_0007 = (
    "(parent_run_id IS NULL AND parent_step_id IS NULL)"
    " OR (parent_run_id IS NOT NULL AND parent_step_id IS NOT NULL AND kind = 'agent' AND conversation_id IS NULL)"
)
_PARENT_LINK_CHECK = (
    "(parent_run_id IS NULL AND parent_step_id IS NULL)"
    " OR (parent_run_id IS NOT NULL AND parent_step_id IS NOT NULL AND kind IN ('agent', 'workflow')"
    " AND conversation_id IS NULL)"
)
_ONE_TARGET_CHECK = "(agent_id IS NULL) <> (workflow_id IS NULL)"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("runs", sa.Column("rerun_of", postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("runs", sa.Column("schedule_id", postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column(
        "runs",
        sa.Column("pause_after_each_step", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index("ix_runs_rerun_of", "runs", ["rerun_of"], unique=False)
    op.create_index("ix_runs_schedule_id", "runs", ["schedule_id"], unique=False)

    op.create_table(
        "workflow_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("cron", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), server_default=sa.text("'UTC'"), nullable=False),
        sa.Column(
            "input", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="workflow_schedules_pkey"),
        sa.UniqueConstraint("tenant_id", "workflow_id", "name", name="uq_workflow_schedules_tenant_workflow_name"),
    )
    op.create_index(
        "ix_workflow_schedules_active_next_fire", "workflow_schedules", ["active", "next_fire_at"], unique=False
    )

    op.add_column("eval_suites", sa.Column("workflow_id", sa.String(), nullable=True))
    op.alter_column("eval_suites", "agent_id", existing_type=sa.String(), nullable=True)
    op.create_check_constraint("ck_eval_suites_one_target", "eval_suites", _ONE_TARGET_CHECK)
    op.create_unique_constraint(
        "uq_eval_suites_tenant_workflow_name", "eval_suites", ["tenant_id", "workflow_id", "name"]
    )

    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK, postgresql_not_valid=True)
    op.drop_constraint("ck_runs_parent_link", "runs", type_="check")
    op.create_check_constraint("ck_runs_parent_link", "runs", _PARENT_LINK_CHECK, postgresql_not_valid=True)

    op.create_table(
        "spans",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("span_id", sa.String(), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("run_id", "span_id", name="spans_pkey"),
    )
    op.create_index(
        "ix_spans_tenant_start", "spans", ["tenant_id", sa.text("start DESC")], unique=False
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_spans_tenant_start", table_name="spans")
    op.drop_table("spans")

    # Workflow eval runs go (their child runs stay as standalone runs); then workflow suites. A workflow
    # run with a parent is not allowed by 0007's check: every such link is cleared first.
    op.execute(
        "UPDATE runs SET parent_run_id = NULL, parent_step_id = NULL"
        " WHERE kind = 'workflow' AND parent_run_id IS NOT NULL"
    )
    op.execute(
        "UPDATE runs c SET parent_run_id = NULL, parent_step_id = NULL FROM runs p"
        " WHERE p.kind = 'eval' AND p.agent_id IS NULL AND c.tenant_id = p.tenant_id AND c.parent_run_id = p.id"
    )
    op.drop_constraint("ck_runs_parent_link", "runs", type_="check")
    op.create_check_constraint("ck_runs_parent_link", "runs", _PARENT_LINK_CHECK_0007)
    op.execute(
        "DELETE FROM side_effects s USING runs r"
        " WHERE r.kind = 'eval' AND r.agent_id IS NULL AND s.tenant_id = r.tenant_id AND s.key LIKE r.id || ':%'"
    )
    op.execute(
        "DELETE FROM run_model_requests q USING runs r"
        " WHERE r.kind = 'eval' AND r.agent_id IS NULL AND q.tenant_id = r.tenant_id AND q.run_id = r.id"
    )
    op.execute(
        "DELETE FROM execution_events e USING runs r"
        " WHERE r.kind = 'eval' AND r.agent_id IS NULL AND e.tenant_id = r.tenant_id AND e.run_id = r.id"
    )
    # pending_approvals and eval_case_results rows cascade with the run.
    op.execute("DELETE FROM runs WHERE kind = 'eval' AND agent_id IS NULL")
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK_0021)

    op.execute("DELETE FROM eval_suites WHERE agent_id IS NULL")  # cases cascade
    op.drop_constraint("uq_eval_suites_tenant_workflow_name", "eval_suites", type_="unique")
    op.drop_constraint("ck_eval_suites_one_target", "eval_suites", type_="check")
    op.alter_column("eval_suites", "agent_id", existing_type=sa.String(), nullable=False)
    op.drop_column("eval_suites", "workflow_id")

    op.drop_index("ix_workflow_schedules_active_next_fire", table_name="workflow_schedules")
    op.drop_table("workflow_schedules")

    op.drop_index("ix_runs_schedule_id", table_name="runs")
    op.drop_index("ix_runs_rerun_of", table_name="runs")
    op.drop_column("runs", "pause_after_each_step")
    op.drop_column("runs", "schedule_id")
    op.drop_column("runs", "rerun_of")
