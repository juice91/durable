"""Reusable scorers: `scorers` and `agent_scorers`.

Revision ID: 0020_scorers
Revises: 0019_trace_feedback
Create Date: 2026-11-16 14:00:00

A scorer is a tenant-scoped, named, reusable evaluator (one deterministic `eap_schemas.evals`
evaluator: contains, regex, schema_valid, field_equals or max_cost) that the worker applies to the
finished runs of the agents it is attached to. The results are recorded only as `score` run events
written by the worker.

- `scorers`: `(id)` primary key, `(tenant_id, name)` unique, `(tenant_id, id)` unique (the target of
  the attachment's composite FK), `evaluator` a JSONB object.
- `agent_scorers`: which scorer an agent runs, with a `sampling_rate` in (0, 1]; primary key
  `(agent_id, scorer_id)`; composite tenant FK `(tenant_id, scorer_id)` to `scorers`, ON DELETE
  CASCADE, so a scorer can never be attached across tenants and deleting it detaches it everywhere.

Lock footprint: two new tables and their indexes; no existing table is touched.

Downgrade: every scorer and attachment is lost (the tables are dropped); `score` events already
written stay in the runs' event logs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0020_scorers"
down_revision: Union[str, Sequence[str], None] = "0019_trace_feedback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.scorers (a migration never imports app code).
_NAME_CHECK = "char_length(name) BETWEEN 1 AND 100"
_EVALUATOR_CHECK = "jsonb_typeof(evaluator) = 'object'"
_RATE_CHECK = "sampling_rate > 0 AND sampling_rate <= 1"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "scorers",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("evaluator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="scorers_pkey"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_scorers_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_scorers_tenant_name"),
        sa.CheckConstraint(_NAME_CHECK, name="ck_scorers_name"),
        sa.CheckConstraint(_EVALUATOR_CHECK, name="ck_scorers_evaluator"),
    )
    op.create_table(
        "agent_scorers",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("scorer_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("sampling_rate", sa.Numeric(5, 4), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", "scorer_id", name="agent_scorers_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "scorer_id"],
            ["scorers.tenant_id", "scorers.id"],
            name="fk_agent_scorers_tenant_scorer",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_RATE_CHECK, name="ck_agent_scorers_sampling_rate"),
    )
    # The scorers an agent runs (the worker, on every finished run) and an agent list per scorer.
    op.create_index("ix_agent_scorers_tenant_agent", "agent_scorers", ["tenant_id", "agent_id"], unique=False)
    op.create_index("ix_agent_scorers_tenant_scorer", "agent_scorers", ["tenant_id", "scorer_id"], unique=False)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_agent_scorers_tenant_scorer", table_name="agent_scorers")
    op.drop_index("ix_agent_scorers_tenant_agent", table_name="agent_scorers")
    op.drop_table("agent_scorers")
    op.drop_table("scorers")
