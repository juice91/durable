"""spans: external (OTLP) spans from outside the platform.

Revision ID: 0032_external_spans
Revises: 0031_workflow_archived_at
Create Date: 2026-11-19 15:00:00

A tenant API key with the `otlp` scope may send OpenTelemetry traces (POST /api/v1/otlp/v1/traces);
they are stored in `spans` next to the platform's own:

- `source` (text NOT NULL DEFAULT 'platform'): 'platform' for the worker's materialised spans,
  'external' for spans sent over OTLP. A constant default: no table rewrite.
- `trace_id` (text, NULL): the OpenTelemetry trace id as sent (32 hex); NULL for platform spans.
- `service_name` (text, NULL): the resource's `service.name`. `api_key_id` (text, NULL): the key that sent it.
- `attributes` and `resource_attributes` (jsonb, NULL): the redacted span and resource attributes.
- `ix_spans_external_trace` (tenant_id, trace_id, start), partial on source = 'external': a trace's spans.
- `ix_spans_external_start` (tenant_id, start DESC), partial on source = 'external': the trace list.

An external span's `run_id` is a UUID derived from (tenant, trace id) so the PK `(run_id, span_id)` keeps its
shape and one trace's spans share it; no run has that id (`GET /runs/{id}` is a 404 for it).

Downgrade: external spans are deleted, then the columns and indexes are dropped.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032_external_spans"
down_revision: Union[str, Sequence[str], None] = "0031_workflow_archived_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("spans", sa.Column("source", sa.String(), nullable=False, server_default="platform"))
    op.add_column("spans", sa.Column("trace_id", sa.String(), nullable=True))
    op.add_column("spans", sa.Column("service_name", sa.String(), nullable=True))
    op.add_column("spans", sa.Column("api_key_id", sa.String(), nullable=True))
    op.add_column("spans", sa.Column("attributes", postgresql.JSONB(), nullable=True))
    op.add_column("spans", sa.Column("resource_attributes", postgresql.JSONB(), nullable=True))
    op.create_check_constraint("ck_spans_source", "spans", "source IN ('platform', 'external')")
    op.create_index(
        "ix_spans_external_trace", "spans", ["tenant_id", "trace_id", "start"],
        postgresql_where=sa.text("source = 'external'"),
    )
    op.create_index(
        "ix_spans_external_start", "spans", ["tenant_id", sa.text("start DESC")],
        postgresql_where=sa.text("source = 'external'"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM spans WHERE source = 'external'")
    op.drop_index("ix_spans_external_start", table_name="spans")
    op.drop_index("ix_spans_external_trace", table_name="spans")
    op.drop_constraint("ck_spans_source", "spans", type_="check")
    for column in ("resource_attributes", "attributes", "api_key_id", "service_name", "trace_id", "source"):
        op.drop_column("spans", column)
