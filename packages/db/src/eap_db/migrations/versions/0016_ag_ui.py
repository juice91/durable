"""AG-UI: `ag_ui_runs`, `conversations.external_thread_id`, `runs.client_tools` and `runs.client_tool_results`.

Revision ID: 0016_ag_ui
Revises: 0015_evals
Create Date: 2026-11-15 18:00:00

- `ag_ui_runs`: one row per AG-UI run (D4 of docs/plans/2026-11-13-milestone-ag-ui.md). It maps the
  client's `(threadId, runId)` within one scope (`scope_kind` `agent` | `workflow`, `scope_id` the
  agent or workflow id) of one tenant to our `run_id`, the seq the AG-UI run streams after
  (`from_seq`: -1 for an agent turn, the seq after `step_waiting` for a workflow resume run), the
  request's `fingerprint` (sha256 hex) and the client's ids of the input messages
  (`input_message_ids`, a JSONB array of strings). Primary key
  `(tenant_id, scope_kind, scope_id, external_thread_id, external_run_id)`: the same external ids
  under two tenants, agents or workflows are distinct rows. Composite tenant FK to the run, ON
  DELETE CASCADE, with its index `(tenant_id, run_id)` (several AG-UI runs may name one workflow run).
- `conversations.external_thread_id`: the raw AG-UI threadId of a conversation derived from it (D3),
  NULL otherwise. Unique per tenant and agent (partial index, NULLs excluded).
- `runs.client_tools`: a turn's client (frontend) tool declarations, a JSONB array (D10);
  `runs.client_tool_results`: the client's results for the previous turn's pending calls, a JSONB
  array. Both NULL for every other run and for every run recorded before 0016.

Lock footprint: `lock_timeout` is 5 s. The new table comes first, then `conversations`, then `runs`:
- the column adds have no default, so they are catalog-only, but they take ACCESS EXCLUSIVE on their
  table until the migration commits;
- the partial unique index on `conversations` is built with a plain CREATE INDEX (CONCURRENTLY
  cannot run inside the migration's transaction), so it scans `conversations` once while that lock
  is held; the predicate keeps the index empty at upgrade time;
- the two JSON-array checks on `runs` are added NOT VALID (no scan): every existing row has NULL in
  the new columns, so it already satisfies them, and new writes are checked.

Every check text is inline (same texts as eap_run_store.ag_ui and eap_run_store.store).

Downgrade: every `ag_ui_runs` row is lost (an AG-UI re-POST of a known runId can no longer attach),
and so are the external thread ids and the client tool columns. Conversations derived from AG-UI
threads stay, as ordinary conversations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0016_ag_ui"
down_revision: Union[str, Sequence[str], None] = "0015_evals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.ag_ui (a migration never imports app code).
_SCOPE_KIND_CHECK = "scope_kind IN ('agent', 'workflow')"
_FROM_SEQ_CHECK = "from_seq >= -1"
_FINGERPRINT_CHECK = "fingerprint ~ '^[0-9a-f]{64}$'"
_INPUT_MESSAGE_IDS_CHECK = "jsonb_typeof(input_message_ids) = 'array'"
# Same texts as eap_run_store.store.
_CLIENT_TOOLS_CHECK = "client_tools IS NULL OR jsonb_typeof(client_tools) = 'array'"
_CLIENT_TOOL_RESULTS_CHECK = "client_tool_results IS NULL OR jsonb_typeof(client_tool_results) = 'array'"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.create_table(
        "ag_ui_runs",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("external_thread_id", sa.String(), nullable=False),
        sa.Column("external_run_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("from_seq", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column(
            "input_message_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "scope_kind", "scope_id", "external_thread_id", "external_run_id", name="ag_ui_runs_pkey"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_ag_ui_runs_tenant_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_SCOPE_KIND_CHECK, name="ck_ag_ui_runs_scope_kind"),
        sa.CheckConstraint(_FROM_SEQ_CHECK, name="ck_ag_ui_runs_from_seq"),
        sa.CheckConstraint(_FINGERPRINT_CHECK, name="ck_ag_ui_runs_fingerprint"),
        sa.CheckConstraint(_INPUT_MESSAGE_IDS_CHECK, name="ck_ag_ui_runs_input_message_ids"),
    )
    # A run's AG-UI runs (and the run FK's cascade).
    op.create_index("ix_ag_ui_runs_tenant_run", "ag_ui_runs", ["tenant_id", "run_id"], unique=False)

    op.add_column("conversations", sa.Column("external_thread_id", sa.String(), nullable=True))
    op.create_index(
        "uq_conversations_tenant_agent_external_thread",
        "conversations",
        ["tenant_id", "agent_id", "external_thread_id"],
        unique=True,
        postgresql_where=sa.text("external_thread_id IS NOT NULL"),
    )

    # `runs` last: catalog-only column adds, the checks NOT VALID.
    op.add_column("runs", sa.Column("client_tools", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("runs", sa.Column("client_tool_results", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint("ck_runs_client_tools", "runs", _CLIENT_TOOLS_CHECK, postgresql_not_valid=True)
    op.create_check_constraint(
        "ck_runs_client_tool_results", "runs", _CLIENT_TOOL_RESULTS_CHECK, postgresql_not_valid=True
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_runs_client_tool_results", "runs", type_="check")
    op.drop_constraint("ck_runs_client_tools", "runs", type_="check")
    op.drop_column("runs", "client_tool_results")
    op.drop_column("runs", "client_tools")

    op.drop_index("uq_conversations_tenant_agent_external_thread", table_name="conversations")
    op.drop_column("conversations", "external_thread_id")

    op.drop_index("ix_ag_ui_runs_tenant_run", table_name="ag_ui_runs")
    op.drop_table("ag_ui_runs")
