"""The built-in admin: its account, first-run setup code, sessions and sign-in throttle (the localadmin plug-in's own branch)."""

import sqlalchemy as sa
from alembic import op

revision = "localadmin_0001"
down_revision = None
branch_labels = ("iam_localadmin",)
depends_on = None


def upgrade() -> None:
    op.create_table(
        "iam_localadmin_account",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("must_change", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # One row per tenant: there is exactly one built-in admin, and creating it is atomic.
        sa.PrimaryKeyConstraint("tenant_id", name="iam_localadmin_account_pkey"),
        sa.CheckConstraint("source IN ('env', 'cli', 'setup', 'user')", name="ck_iam_localadmin_account_source"),
    )
    op.create_table(
        "iam_localadmin_setup",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="iam_localadmin_setup_pkey"),
    )
    op.create_table(
        "iam_localadmin_session",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("refresh_hash", sa.String(), nullable=False),
        sa.Column("previous_hash", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="iam_localadmin_session_pkey"),
    )
    op.create_index("ix_iam_localadmin_session_tenant", "iam_localadmin_session", ["tenant_id", "expires_at"])
    op.create_table(
        "iam_localadmin_throttle",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "key", name="iam_localadmin_throttle_pkey"),
    )


def downgrade() -> None:
    op.drop_table("iam_localadmin_throttle")
    op.drop_index("ix_iam_localadmin_session_tenant", table_name="iam_localadmin_session")
    op.drop_table("iam_localadmin_session")
    op.drop_table("iam_localadmin_setup")
    op.drop_table("iam_localadmin_account")
