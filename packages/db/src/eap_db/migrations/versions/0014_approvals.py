"""Approvals: the `pending_approvals` projection (tool and workflow-step approvals) and `runs.output_format`.

Revision ID: 0014_approvals
Revises: 0013_semantic_recall
Create Date: 2026-11-15 10:00:00

- `pending_approvals`: one row per human decision a run waits for. `kind` is `tool` (an agent run's
  gated tool call; `approval_key` = the tool call id) or `step` (a workflow approval step;
  `approval_key` = the step id). PK `(tenant_id, run_id, kind, approval_key)`. The row is written in
  the transaction of the waiting event (`tool:{id}:approval:waiting` / `step:{id}:waiting`) and
  updated in the transaction of the decision event (`tool:{id}:approval:decision` /
  `step:{id}:decision`), so it never disagrees with the event log, which stays the source of truth.
  - `root_run_id`: the top-level run (a workflow agent step's child run names its workflow run;
    otherwise the run itself). `agent_id` / `workflow_id`: the run's agent and the workflow (the
    workflow's own id, or its parent workflow run's for a child run), copied for inbox filters.
  - `tool_name` is set for `tool` rows only; `prompt` is the step's prompt; `args` is the tool call's
    arguments (at most 8 KiB, else `{"truncated": true}`; NULL for steps).
  - `decision` is `approved` | `denied` | `timed_out`, set together with `decided_at`
    (`decided_by` is the approving principal, NULL for a timeout).
  - Composite tenant FKs to `runs` for `run_id` and `root_run_id`, ON DELETE CASCADE (the
    projection never outlives its run).
  - Indexes lead with `tenant_id`: open rows by `requested_at` (partial, the pending inbox), decided
    rows by `decided_at` (partial, the decided inbox), and `(tenant_id, root_run_id)` (a workflow
    run's waits, the display status and the cascade).
- Backfill: one `kind='step'` row per `step_waiting` event that has no `step:{id}:decision` event
  (an open workflow approval at upgrade time), from the event's data (prompt, deadline) and its run.
  The deadline is cast only when it has the ISO 8601 shape with an offset that
  eap.workflow_approval_begin writes; anything else gets the event time + 24 hours.
- `runs.output_format`: `text` | `json` (NULL = text): `json` when `runs.output` is a structured
  output's canonical JSON text.

Lock footprint: `lock_timeout` is 5 s for the migration's transaction (a busy `runs` fails the
upgrade fast instead of queueing every run write behind it). The new table and its backfill come
first; `runs` is altered last, so its ACCESS EXCLUSIVE lock is held only briefly: the column has no
default (a catalog-only change) and `ck_runs_output_format` is added NOT VALID (no table scan; the
column is all NULL, and new writes are checked).

Every check text is inline (same texts as eap_run_store.approvals and eap_run_store.store).

Downgrade drops the table and the column: the projection (rebuildable from the event log) and the
output format markers are lost.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0014_approvals"
down_revision: Union[str, Sequence[str], None] = "0013_semantic_recall"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store (a migration never imports app code).
_RUN_OUTPUT_FORMAT_CHECK = "output_format IN ('text', 'json')"
_PA_KIND_CHECK = "kind IN ('tool', 'step')"
_PA_TOOL_NAME_CHECK = "(kind = 'tool' AND tool_name IS NOT NULL) OR (kind = 'step' AND tool_name IS NULL)"
_PA_DECISION_CHECK = "decision IS NULL OR decision IN ('approved', 'denied', 'timed_out')"
_PA_DECIDED_CHECK = "(decided_at IS NULL) = (decision IS NULL)"


# The deadline text eap.workflow_approval_begin writes (datetime.isoformat() of an aware datetime).
_ISO_DEADLINE_PATTERN = (
    r"^[0-9]{4}-[01][0-9]-[0-3][0-9][T ][0-2][0-9][:][0-5][0-9][:][0-5][0-9]([.][0-9]{1,6})?"
    r"([+-][0-2][0-9][:]?[0-5][0-9]|Z)$"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.create_table(
        "pending_approvals",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("approval_key", sa.String(), nullable=False),
        sa.Column("root_run_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("workflow_id", sa.String(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "run_id", "kind", "approval_key", name="pending_approvals_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_pending_approvals_tenant_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "root_run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_pending_approvals_tenant_root_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_PA_KIND_CHECK, name="ck_pending_approvals_kind"),
        sa.CheckConstraint(_PA_TOOL_NAME_CHECK, name="ck_pending_approvals_tool_name"),
        sa.CheckConstraint(_PA_DECISION_CHECK, name="ck_pending_approvals_decision"),
        sa.CheckConstraint(_PA_DECIDED_CHECK, name="ck_pending_approvals_decided"),
    )
    # The pending inbox (open rows, by request time).
    op.create_index(
        "ix_pending_approvals_tenant_open",
        "pending_approvals",
        ["tenant_id", "requested_at"],
        unique=False,
        postgresql_where=sa.text("decided_at IS NULL"),
    )
    # The decided inbox (by decision time).
    op.create_index(
        "ix_pending_approvals_tenant_decided",
        "pending_approvals",
        ["tenant_id", "decided_at"],
        unique=False,
        postgresql_where=sa.text("decided_at IS NOT NULL"),
    )
    # A workflow run's waits (its steps' and its agent steps' child runs'), and the root FK's cascade.
    op.create_index(
        "ix_pending_approvals_tenant_root_run", "pending_approvals", ["tenant_id", "root_run_id"], unique=False
    )

    # Backfill the workflow approval steps waiting at upgrade time: a `step:{id}:waiting` event
    # without its `step:{id}:decision` event. The deadline is the one the event records (always
    # written by eap.workflow_approval_begin); a hand-made event without one gets 24 hours.
    op.execute(
        """
        INSERT INTO pending_approvals (
            tenant_id, run_id, kind, approval_key, root_run_id, agent_id, workflow_id, tool_name, prompt,
            args, requested_at, deadline
        )
        SELECT
            w.tenant_id,
            w.run_id,
            'step',
            substr(w.event_key, 6, length(w.event_key) - 13),
            COALESCE(p.parent_run_id, r.parent_run_id, r.id),
            r.agent_id,
            COALESCE(r.workflow_id, p.workflow_id),
            NULL,
            w.data ->> 'prompt',
            NULL,
            w.ts,
            CASE
                WHEN jsonb_typeof(w.data -> 'deadline') = 'string'
                     AND (w.data ->> 'deadline') ~ '{iso}'
                    THEN (w.data ->> 'deadline')::timestamptz
                ELSE w.ts + interval '24 hours'
            END
        FROM execution_events w
        JOIN runs r ON r.tenant_id = w.tenant_id AND r.id = w.run_id
        LEFT JOIN runs p ON p.tenant_id = r.tenant_id AND p.id = r.parent_run_id
        WHERE w.type = 'step_waiting'
          AND w.event_key ~ '^step[:].+[:]waiting$'
          AND NOT EXISTS (
              SELECT 1 FROM execution_events d
              WHERE d.run_id = w.run_id
                AND d.event_key = substr(w.event_key, 1, length(w.event_key) - 7) || 'decision'
          )
        ON CONFLICT DO NOTHING
        """.format(iso=_ISO_DEADLINE_PATTERN)
    )

    # `runs` last: a catalog-only column add and a NOT VALID check (no scan under the lock).
    op.add_column("runs", sa.Column("output_format", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_runs_output_format", "runs", _RUN_OUTPUT_FORMAT_CHECK, postgresql_not_valid=True
    )


def downgrade() -> None:
    op.drop_index("ix_pending_approvals_tenant_root_run", table_name="pending_approvals")
    op.drop_index("ix_pending_approvals_tenant_decided", table_name="pending_approvals")
    op.drop_index("ix_pending_approvals_tenant_open", table_name="pending_approvals")
    op.drop_table("pending_approvals")  # the projection is lost (the event log keeps every decision)
    op.drop_constraint("ck_runs_output_format", "runs", type_="check")
    op.drop_column("runs", "output_format")
