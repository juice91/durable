"""Trace feedback: `trace_feedback`.

Revision ID: 0019_trace_feedback
Revises: 0018_run_model_settings
Create Date: 2026-11-16 12:00:00

A trace is a run (its id is the run id). A viewer leaves feedback on a whole trace or on one of its
spans (`span_id` NULL = the trace): a `rating` ('up' | 'down'), an optional numeric `score`
(-1000000 to 1000000) and an optional `comment` (at most 2000 characters), recorded with who
(`created_by`) and when (`created_at`). Rows are append-only; one trace or span may have many.

- Composite tenant FK `(tenant_id, run_id)` to `runs`, ON DELETE CASCADE: another tenant's run can
  never be referenced, and feedback goes with its run.
- Index `(tenant_id, run_id, created_at)` for a trace's feedback list.

Lock footprint: one new table and one index on it; `runs` is only referenced by its existing
`(tenant_id, id)` unique key, so adding the FK takes a SHARE ROW EXCLUSIVE lock on `runs` for the
duration of the (empty-table) constraint creation.

Downgrade: every feedback row is lost (the table is dropped); runs are unchanged.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0019_trace_feedback"
down_revision: Union[str, Sequence[str], None] = "0018_run_model_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.trace_feedback.
_RATING_CHECK = "rating IN ('up', 'down')"
_COMMENT_CHECK = "comment IS NULL OR char_length(comment) <= 2000"
_SPAN_CHECK = "span_id IS NULL OR char_length(span_id) BETWEEN 1 AND 200"
_SCORE_CHECK = "score IS NULL OR score BETWEEN -1000000 AND 1000000"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "trace_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("span_id", sa.String(), nullable=True),
        sa.Column("rating", sa.String(), nullable=False),
        sa.Column("score", sa.Numeric(20, 6), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="trace_feedback_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_trace_feedback_tenant_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_RATING_CHECK, name="ck_trace_feedback_rating"),
        sa.CheckConstraint(_COMMENT_CHECK, name="ck_trace_feedback_comment"),
        sa.CheckConstraint(_SPAN_CHECK, name="ck_trace_feedback_span"),
        sa.CheckConstraint(_SCORE_CHECK, name="ck_trace_feedback_score"),
    )
    op.create_index(
        "ix_trace_feedback_tenant_run_created", "trace_feedback", ["tenant_id", "run_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_trace_feedback_tenant_run_created", table_name="trace_feedback")
    op.drop_table("trace_feedback")
