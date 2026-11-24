"""System-aware Architect, phase C: provenance of the Architect's drafts and publish cards.

Revision ID: 0037_definition_provenance
Revises: 0036_iam_actor_audit
Create Date: 2026-11-23 12:00:00

`definition_provenance`: one append-only row per draft save and per publish card, keyed by the definition's
`spec_hash`. Beside the definition, never inside it: the spec, `canonical_spec_hash` and the version rows are
untouched. `body` is the DraftProvenance (hashes, principal summary, tool-call names and argument hashes, never
arguments or tenant text). `version` is set on a publish card.

Expand-only (a new table). Downgrade drops it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037_definition_provenance"
down_revision: Union[str, Sequence[str], None] = "0036_iam_actor_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "definition_provenance",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("definition_id", sa.String(), nullable=False),
        sa.Column("spec_hash", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("plan_id", sa.String(), nullable=True),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id", name="definition_provenance_pkey"),
        sa.CheckConstraint("kind IN ('agent', 'workflow')", name="ck_definition_provenance_kind"),
    )
    op.create_index(
        "ix_definition_provenance_def_hash", "definition_provenance", ["tenant_id", "definition_id", "spec_hash"]
    )
    op.execute(
        """
        CREATE FUNCTION definition_provenance_refuse_update() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'definition_provenance is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER definition_provenance_no_update BEFORE UPDATE ON definition_provenance "
        "FOR EACH ROW EXECUTE FUNCTION definition_provenance_refuse_update()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS definition_provenance_no_update ON definition_provenance")
    op.execute("DROP FUNCTION IF EXISTS definition_provenance_refuse_update()")
    op.drop_index("ix_definition_provenance_def_hash", table_name="definition_provenance")
    op.drop_table("definition_provenance")
