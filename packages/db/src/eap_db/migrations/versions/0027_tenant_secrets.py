"""Tenant secrets: `tenant_secrets`, the per-tenant encrypted values agents reference as `secret:<name>`.

Revision ID: 0027_tenant_secrets
Revises: 0026_architect_slugs
Create Date: 2026-11-18 12:00:00

One row per (tenant, name). `ciphertext` is AES-GCM sealed with the platform key (EAP_SECRETS_KEY)
and bound to (tenant, name) as associated data; `key_id` names the key. Values are write-only: no
route returns one, only the worker and the API's own resolve path decrypt.

Lock footprint: a new table only. The name check has the same text as eap_schemas.secrets.

Downgrade: every stored secret is lost; `secret:<name>` references then fail with secret_not_found.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0027_tenant_secrets"
down_revision: Union[str, Sequence[str], None] = "0026_architect_slugs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same text as eap_schemas.secrets.SECRET_NAME_PATTERN (a migration never imports app code).
_NAME_CHECK = "name ~ '^[a-z][a-z0-9_.-]{0,63}$'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "tenant_secrets",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "name", name="tenant_secrets_pkey"),
        sa.CheckConstraint(_NAME_CHECK, name="ck_tenant_secrets_name"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("tenant_secrets")
