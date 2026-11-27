"""External agents (inbound): external clients, the `run` API key, workflow exports, A2A endpoints and external runs.

Revision ID: 0043_external_access
Revises: 0042_webhook_triggers
Create Date: 2026-11-26 15:00:00

Design: an internal design note (sections 3, 4, 7 and 9).

- `external_clients`: the identity of an external agent; it owns the runs it starts, so its keys can be rotated.
- `api_keys.external_client_id`: only a `run` key has one. The database holds the key rules too: a `run` key carries
  that scope alone, names a client, has a non-empty workflow allowlist, no agent allowlist, no owner and an expiry.
- `workflow_exports`: the public contract of a workflow, disabled until a person approves it.
- `external_endpoints`: the A2A endpoint of a tenant (used by a later release; created here with the rest).
- `external_runs`: the idempotency claim of a run an external client started, taken BEFORE the run row (so no foreign
  key to `runs`): ownership, quotas, the deadline and its cancel reason. `closed_at` takes ended runs out of the
  indexes the quota count and the worker's sweep read.

All tables are keyed by tenant, with composite foreign keys. Downgrade drops them and the column (run keys with it:
they would otherwise be unrestricted in no way but unusable, so they are deleted).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_external_access"
down_revision: Union[str, Sequence[str], None] = "0042_webhook_triggers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_UUID = postgresql.UUID(as_uuid=False)
_NOW = sa.text("now()")
RUN_KEY_CHECK = (
    "NOT (scopes @> '[\"run\"]'::jsonb) OR (jsonb_array_length(scopes) = 1 AND external_client_id IS NOT NULL"
    " AND workflow_ids IS NOT NULL AND jsonb_array_length(workflow_ids) > 0 AND agent_ids IS NULL"
    " AND expires_at IS NOT NULL AND owner_subject IS NULL)"
)
CLIENT_KEY_CHECK = "external_client_id IS NULL OR scopes @> '[\"run\"]'::jsonb"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
    ]


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "external_clients",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("max_active_runs", sa.Integer(), nullable=True),
        sa.Column("max_runs_per_day", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="external_clients_pkey"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_external_clients_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_external_clients_tenant_name"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="ck_external_clients_name"),
        sa.CheckConstraint(
            "(max_active_runs IS NULL OR max_active_runs >= 0) AND (max_runs_per_day IS NULL OR max_runs_per_day >= 0)",
            name="ck_external_clients_quotas",
        ),
    )

    op.add_column("api_keys", sa.Column("external_client_id", _UUID, nullable=True))
    op.create_foreign_key(
        "fk_api_keys_external_client", "api_keys", "external_clients",
        ["tenant_id", "external_client_id"], ["tenant_id", "id"],
    )
    op.create_check_constraint("ck_api_keys_run_key", "api_keys", sa.text(RUN_KEY_CHECK))
    op.create_check_constraint("ck_api_keys_external_client", "api_keys", sa.text(CLIENT_KEY_CHECK))
    op.create_index(
        "ix_api_keys_external_client", "api_keys", ["tenant_id", "external_client_id"],
        postgresql_where=sa.text("external_client_id IS NOT NULL"),
    )

    op.create_table(
        "workflow_exports",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("tags", _JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("examples", _JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("workflow_id", sa.String(), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=True),
        sa.Column("protocols", _JSONB, nullable=False),
        sa.Column("result_mode", sa.String(), nullable=False, server_default=sa.text("'output'")),
        sa.Column("client_inputs", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("max_runtime_seconds", sa.Integer(), nullable=False, server_default=sa.text("3600")),
        sa.Column("require_idempotency_key", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("disabled_reason", sa.String(), nullable=True),
        sa.Column("config_hash", sa.String(), nullable=False),
        sa.Column("approved_hash", sa.String(), nullable=True),
        sa.Column("approved_contract_hash", sa.String(), nullable=True),
        sa.Column("enabled_by", sa.String(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="workflow_exports_pkey"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_workflow_exports_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_workflow_exports_tenant_name"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"], ["workflows.tenant_id", "workflows.id"], name="fk_workflow_exports_workflow"
        ),
        sa.CheckConstraint("name ~ '^[a-z][a-z0-9_]{0,47}$'", name="ck_workflow_exports_name"),
        sa.CheckConstraint("result_mode IN ('output', 'status_only')", name="ck_workflow_exports_result_mode"),
        sa.CheckConstraint(
            "jsonb_typeof(protocols) = 'array' AND jsonb_array_length(protocols) BETWEEN 1 AND 2",
            name="ck_workflow_exports_protocols",
        ),
        sa.CheckConstraint("max_runtime_seconds >= 1", name="ck_workflow_exports_max_runtime"),
        sa.CheckConstraint(
            "NOT enabled OR (approved_hash IS NOT NULL AND approved_contract_hash IS NOT NULL)",
            name="ck_workflow_exports_approved",
        ),
    )
    op.create_index("ix_workflow_exports_workflow", "workflow_exports", ["tenant_id", "workflow_id"])

    op.create_table(
        "external_endpoints",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("endpoint_id", sa.String(), nullable=False),
        sa.Column("card_name", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("card_description", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        *_timestamps(),
        sa.PrimaryKeyConstraint("tenant_id", name="external_endpoints_pkey"),
        sa.UniqueConstraint("endpoint_id", name="uq_external_endpoints_endpoint_id"),
        sa.CheckConstraint("endpoint_id ~ '^[0-9a-f]{32}$'", name="ck_external_endpoints_endpoint_id"),
    )

    op.create_table(
        "external_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("client_id", _UUID, nullable=False),
        sa.Column("key_id", sa.String(), nullable=False),
        sa.Column("export_id", _UUID, nullable=False),
        sa.Column("protocol", sa.String(), nullable=False),
        sa.Column("idempotency_hash", sa.String(), nullable=True),
        sa.Column("input_hash", sa.String(), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("context_label", sa.String(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancel_reason", sa.String(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.PrimaryKeyConstraint("run_id", name="external_runs_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "client_id"], ["external_clients.tenant_id", "external_clients.id"],
            name="fk_external_runs_client",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "export_id"], ["workflow_exports.tenant_id", "workflow_exports.id"],
            name="fk_external_runs_export",
        ),
        sa.CheckConstraint("protocol IN ('mcp', 'a2a')", name="ck_external_runs_protocol"),
    )
    op.create_index(
        "uq_external_runs_idempotency", "external_runs",
        ["tenant_id", "client_id", "export_id", "idempotency_hash"], unique=True,
        postgresql_where=sa.text("idempotency_hash IS NOT NULL"),
    )
    op.create_index("ix_external_runs_client_created", "external_runs", ["tenant_id", "client_id", "created_at"])
    op.create_index(
        "ix_external_runs_open", "external_runs", ["tenant_id", "client_id"], postgresql_where=sa.text("closed_at IS NULL")
    )
    op.create_index(
        "ix_external_runs_deadline", "external_runs", ["deadline_at"], postgresql_where=sa.text("closed_at IS NULL")
    )


def downgrade() -> None:
    op.drop_table("external_runs")
    op.drop_table("external_endpoints")
    op.drop_table("workflow_exports")
    op.drop_index("ix_api_keys_external_client", table_name="api_keys")
    op.drop_constraint("ck_api_keys_external_client", "api_keys", type_="check")
    op.drop_constraint("ck_api_keys_run_key", "api_keys", type_="check")
    op.drop_constraint("fk_api_keys_external_client", "api_keys", type_="foreignkey")
    op.execute("DELETE FROM api_keys WHERE external_client_id IS NOT NULL")
    op.drop_column("api_keys", "external_client_id")
    op.drop_table("external_clients")
