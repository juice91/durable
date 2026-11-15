"""API keys: `api_keys`, the tenant keys third-party AG-UI frontends authenticate with.

Revision ID: 0017_api_keys
Revises: 0016_ag_ui
Create Date: 2026-11-15 20:00:00

One row per key (D16 of docs/plans/2026-11-13-milestone-ag-ui.md). A key reads
`pos_<id>_<secret>`; only `secret_hash`, the sha256 hex of `<id>:<secret>`, is stored, so the
plaintext exists only in the creation response. `id` is globally unique (the key names it before the
tenant is known); every other read is tenant-scoped.

- `scopes`: a JSONB array of scope names (v1: `agui:run`);
- `agent_ids`, `workflow_ids`: optional JSONB allowlists. Both NULL is a tenant-wide key; once one
  is set the key is restricted on both, and the API stores the other as `[]` (nothing);
- `allowed_origins`: optional JSONB array of browser origins (checked by AGUI.9);
- `expires_at`, `revoked_at`: a key past either is refused; `last_used_at` is written at most once a
  minute per key.

Lock footprint: a new table only. Every check text is inline (same texts as eap_run_store.api_keys).

Downgrade: every key is lost; frontends using one get 401.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0017_api_keys"
down_revision: Union[str, Sequence[str], None] = "0016_ag_ui"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.api_keys (a migration never imports app code).
_ID_CHECK = "id ~ '^[0-9a-f]{24}$'"
_SECRET_HASH_CHECK = "secret_hash ~ '^[0-9a-f]{64}$'"
_NAME_CHECK = "char_length(name) BETWEEN 1 AND 100"
_SCOPES_CHECK = "jsonb_typeof(scopes) = 'array'"
_AGENT_IDS_CHECK = "agent_ids IS NULL OR jsonb_typeof(agent_ids) = 'array'"
_WORKFLOW_IDS_CHECK = "workflow_ids IS NULL OR jsonb_typeof(workflow_ids) = 'array'"
_ALLOWED_ORIGINS_CHECK = "allowed_origins IS NULL OR jsonb_typeof(allowed_origins) = 'array'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("secret_hash", sa.String(), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[\"agui:run\"]'::jsonb"),
            nullable=False,
        ),
        sa.Column("agent_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("workflow_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("allowed_origins", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="api_keys_pkey"),
        sa.CheckConstraint(_ID_CHECK, name="ck_api_keys_id"),
        sa.CheckConstraint(_SECRET_HASH_CHECK, name="ck_api_keys_secret_hash"),
        sa.CheckConstraint(_NAME_CHECK, name="ck_api_keys_name"),
        sa.CheckConstraint(_SCOPES_CHECK, name="ck_api_keys_scopes"),
        sa.CheckConstraint(_AGENT_IDS_CHECK, name="ck_api_keys_agent_ids"),
        sa.CheckConstraint(_WORKFLOW_IDS_CHECK, name="ck_api_keys_workflow_ids"),
        sa.CheckConstraint(_ALLOWED_ORIGINS_CHECK, name="ck_api_keys_allowed_origins"),
    )
    # The tenant's keys, newest first (GET /api-keys).
    op.create_index("ix_api_keys_tenant_created", "api_keys", ["tenant_id", "created_at"], unique=False)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_api_keys_tenant_created", table_name="api_keys")
    op.drop_table("api_keys")
