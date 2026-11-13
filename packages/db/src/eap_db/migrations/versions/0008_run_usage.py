"""Run usage and cost: one row per model request, run totals and timestamps.

Revision ID: 0008_run_usage
Revises: 0007_workflow_runs
Create Date: 2026-11-13 23:00:00

- `run_model_requests`: one row per model request of a run (`request_key` is the request's durable
  event key without the `:final` suffix, e.g. `model:0`). Written by `EventLog.append` in the same
  transaction as that request's `model_message` event, `ON CONFLICT DO NOTHING`, so it is idempotent
  by the event's own `(run_id, event_key)` rule. Tokens as reported by the provider (zeros and
  `usage_measured = false` when it reported none). `cost_usd` is computed once at record time and
  never recomputed; NULL = no price for the model (`price_source` NULL too). Composite FK
  (tenant_id, run_id) -> runs(tenant_id, id): a row always belongs to its run's tenant.
  Indexes `(tenant_id, created_at)` and `(tenant_id, model_id, created_at)` serve the metrics API.
- `runs`: `model_id` (the effective model, `provider:name`) and `model_overridden` (default false),
  totals `input_tokens`, `output_tokens`, `cost_usd` (set when the run finishes: the SUM over its
  rows, and over its child runs' rows for a workflow run; cost NULL if any row is unpriced), and
  `started_at` (first time a worker marks it running) / `finished_at` (when it reaches a terminal
  status). All NULL for runs recorded before this revision ("not recorded"); no backfill.
- `ix_execution_events_tenant_type_ts` (tenant_id, type, ts): events of one type across a tenant's
  runs (processor results).

Downgrade drops the table, the index and the columns (usage data is lost; events keep their data).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_run_usage"
down_revision: Union[str, Sequence[str], None] = "0007_workflow_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_model_requests",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("provider_model_name", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("usage_measured", sa.Boolean(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(20, 10), nullable=True),
        sa.Column("price_source", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "request_key", name="run_model_requests_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"], name="fk_run_model_requests_tenant_run"
        ),
    )
    op.create_index(
        "ix_run_model_requests_tenant_created", "run_model_requests", ["tenant_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_run_model_requests_tenant_model_created",
        "run_model_requests",
        ["tenant_id", "model_id", "created_at"],
        unique=False,
    )
    op.add_column("runs", sa.Column("model_id", sa.String(), nullable=True))
    op.add_column(
        "runs", sa.Column("model_overridden", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column("runs", sa.Column("input_tokens", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("output_tokens", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("cost_usd", sa.Numeric(20, 10), nullable=True))
    op.add_column("runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_execution_events_tenant_type_ts", "execution_events", ["tenant_id", "type", "ts"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_execution_events_tenant_type_ts", table_name="execution_events")
    op.drop_column("runs", "finished_at")
    op.drop_column("runs", "started_at")
    op.drop_column("runs", "cost_usd")
    op.drop_column("runs", "output_tokens")
    op.drop_column("runs", "input_tokens")
    op.drop_column("runs", "model_overridden")
    op.drop_column("runs", "model_id")
    op.drop_index("ix_run_model_requests_tenant_model_created", table_name="run_model_requests")
    op.drop_index("ix_run_model_requests_tenant_created", table_name="run_model_requests")
    op.drop_table("run_model_requests")
