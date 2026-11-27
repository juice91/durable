"""Inbound webhooks (start a run): triggers, their audit events, the delivery log and `runs.trigger_id`.

Revision ID: 0042_webhook_triggers
Revises: 0041_run_actor_roles
Create Date: 2026-11-26 12:00:00

Design: an internal design note (sections 2 and 8).

- `webhook_triggers`: a tenant's inbound endpoint, pinned to one workflow. Secrets are `secret:<name>` references only
  (1 or 2 active). Created `disabled`; a person enables it; `approved_config_hash` binds that approval to the grant.
- `webhook_trigger_events`: append-only lifecycle audit of a trigger (created, enabled, rotated, ...). Never a secret.
- `webhook_deliveries`: one row per authenticated delivery that reached the claim. The partial unique index on
  `(tenant_id, trigger_id, dedupe_key)` over live claims is the exactly-once guard. Never a body, signature or header.
- `runs.trigger_id` (no FK, like `schedule_id`): the trigger that started the run, plus a partial index for the
  per-trigger concurrency count.

Downgrade: drops the three tables, the index and the column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042_webhook_triggers"
down_revision: Union[str, Sequence[str], None] = "0041_run_actor_roles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "webhook_triggers",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=True),
        sa.Column("preset", sa.String(), nullable=False),
        sa.Column("preset_config", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("secrets", _JSONB, nullable=False),
        sa.Column("tolerance_seconds", sa.Integer(), nullable=False, server_default=sa.text("300")),
        sa.Column("actions", _JSONB, nullable=False),
        sa.Column("limits", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'disabled'")),
        sa.Column("config_hash", sa.String(), nullable=False),
        sa.Column("approved_config_hash", sa.String(), nullable=True),
        sa.Column("enable_requested_by", sa.String(), nullable=True),
        sa.Column("enable_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled_by", sa.String(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="webhook_triggers_pkey"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_webhook_triggers_tenant_name"),
        sa.CheckConstraint(
            "name ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'", name="ck_webhook_triggers_name"
        ),
        sa.CheckConstraint("preset IN ('github', 'standard_webhooks', 'custom')", name="ck_webhook_triggers_preset"),
        sa.CheckConstraint(
            "status IN ('disabled', 'pending_enable', 'enabled', 'archived')", name="ck_webhook_triggers_status"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(secrets) = 'array' AND jsonb_array_length(secrets) BETWEEN 1 AND 2",
            name="ck_webhook_triggers_secrets",
        ),
        sa.CheckConstraint("tolerance_seconds BETWEEN 60 AND 900", name="ck_webhook_triggers_tolerance"),
    )
    op.create_index(
        "ix_webhook_triggers_workflow",
        "webhook_triggers",
        ["tenant_id", "workflow_id"],
        postgresql_where=sa.text("status <> 'archived'"),
    )

    op.create_table(
        "webhook_trigger_events",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("trigger_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("detail", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.PrimaryKeyConstraint("id", name="webhook_trigger_events_pkey"),
    )
    op.create_index("ix_webhook_trigger_events_trigger_at", "webhook_trigger_events", ["tenant_id", "trigger_id", "at"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("trigger_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("state", sa.String(), nullable=False, server_default=sa.text("'processing'")),
        sa.Column("outcome", sa.String(), nullable=False, server_default=sa.text("'processing'")),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("dedupe_key", sa.String(), nullable=True),
        sa.Column("key_kind", sa.String(), nullable=True),
        sa.Column("provider_delivery_id", sa.String(), nullable=True),
        sa.Column("event_label", sa.String(), nullable=True),
        sa.Column("body_sha256", sa.String(), nullable=True),
        sa.Column("body_bytes", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("timestamp_skew_seconds", sa.Integer(), nullable=True),
        sa.Column("secret_slot", sa.Integer(), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.PrimaryKeyConstraint("id", name="webhook_deliveries_pkey"),
        sa.CheckConstraint("state IN ('processing', 'done')", name="ck_webhook_deliveries_state"),
    )
    op.create_index(
        "ix_webhook_deliveries_trigger_received",
        "webhook_deliveries",
        ["tenant_id", "trigger_id", sa.text("received_at DESC")],
    )
    op.create_index(
        "uq_webhook_deliveries_live_claim",
        "webhook_deliveries",
        ["tenant_id", "trigger_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("outcome IN ('processing', 'accepted')"),
    )

    op.create_index(
        "ix_webhook_deliveries_run", "webhook_deliveries", ["tenant_id", "run_id"], postgresql_where=sa.text("run_id IS NOT NULL")
    )

    op.add_column("runs", sa.Column("trigger_id", postgresql.UUID(as_uuid=False), nullable=True))
    op.create_index(
        "ix_runs_trigger_active",
        "runs",
        ["tenant_id", "trigger_id"],
        postgresql_where=sa.text(
            "trigger_id IS NOT NULL AND status NOT IN ('completed', 'failed', 'needs_attention', 'skipped')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_trigger_active", table_name="runs")
    op.drop_column("runs", "trigger_id")
    op.drop_index("ix_webhook_deliveries_run", table_name="webhook_deliveries")
    op.drop_index("uq_webhook_deliveries_live_claim", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_trigger_received", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_trigger_events_trigger_at", table_name="webhook_trigger_events")
    op.drop_table("webhook_trigger_events")
    op.drop_index("ix_webhook_triggers_workflow", table_name="webhook_triggers")
    op.drop_table("webhook_triggers")
