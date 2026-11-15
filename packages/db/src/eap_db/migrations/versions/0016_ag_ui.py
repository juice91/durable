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
- One commit order per root run (AGUI.8, D7): `runs.event_order_next` (bigint, default 0) is a
  counter on the ROOT run's row (a run without `parent_run_id` is its own root; a workflow's agent
  steps and an eval's cases are children of it). Every event append takes the root's row lock
  (root first, then the child) and stamps `execution_events.root_order` from it, so commit order
  equals `root_order` and readers always see a gapless prefix. NULL for events recorded before 0016
  (those are ordered by (ts, run_id, seq)).
- `ag_ui_runs.from_order` / `end_order`: where a workflow AG-UI run starts and ends in its root's
  `root_order` (NULL: the beginning / not ended yet); recorded, never re-derived.

- `ix_execution_events_run_root_order` on `execution_events (run_id, root_order)`, partial
  (`root_order IS NOT NULL`): a workflow stream's poll reads each run of the tree after a position
  as an index range (`EventLog.list_root_events`), not the whole tree.
- `trg_execution_events_root_order` (BEFORE INSERT): stamps `root_order` the same way when a writer
  left it NULL, which is an old-code worker during a rolling deploy. It locks the root row through
  the counter UPDATE. Current code always sets it, so the trigger does nothing for it. An old
  writer has already locked the child row, so the trigger takes child before root, the reverse of
  current code. The deadlock detector then aborts one of the two, and the DBOS step retries.
  Drain workers before deploying AG-UI (apps/worker/README.md).

Lock footprint: `lock_timeout` is 5 s. The tables are taken in the order the running code takes
them, so the upgrade cannot deadlock with live writers: create_turn locks a conversation and then
inserts a run, and every event append locks runs rows and then inserts into execution_events.
- `conversations` first: the column add (catalog-only) and the partial unique index, built with a
  plain CREATE INDEX (CONCURRENTLY cannot run inside the migration's transaction). It scans
  `conversations` once under the lock; the predicate keeps the index empty at upgrade time.
- then `ag_ui_runs` (its FK takes SHARE ROW EXCLUSIVE on `runs`) and the `runs` column adds:
  - `event_order_next` has a constant default and the others none, so all are catalog-only
    (PG 11+);
  - they take ACCESS EXCLUSIVE on `runs` until the migration commits;
  - the two JSON-array checks are added NOT VALID (no scan): every existing row has NULL in the
    new columns, so it already satisfies them, and new writes are checked.
- `execution_events` last:
  - the nullable column add is catalog-only;
  - the partial index is built with a plain CREATE INDEX. It scans `execution_events` once while
    ACCESS EXCLUSIVE is held, which blocks appends for the scan; every existing row has a NULL
    `root_order`, so the index is empty;
  - then the trigger.

Every check text is inline (same texts as eap_run_store.ag_ui and eap_run_store.store).

Downgrade: the commit order (`root_order`, the counters) is dropped; every `ag_ui_runs` row is lost (an AG-UI re-POST of a known runId can no longer attach),
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
# Same rule as eap_run_store.lock_run_for_append / next_root_order, for a writer that left
# root_order NULL (an old-code worker during a rolling deploy).
_ROOT_ORDER_FUNCTION = """
CREATE FUNCTION execution_events_root_order() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    root_id text;
BEGIN
    IF NEW.root_order IS NOT NULL THEN
        RETURN NEW;
    END IF;
    SELECT COALESCE(parent_run_id, id) INTO root_id FROM runs WHERE id = NEW.run_id;
    IF root_id IS NULL THEN
        RETURN NEW;
    END IF;
    UPDATE runs SET event_order_next = event_order_next + 1 WHERE id = root_id
        RETURNING event_order_next - 1 INTO NEW.root_order;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # The lock order of the running code: conversations, then runs, then execution_events.
    op.add_column("conversations", sa.Column("external_thread_id", sa.String(), nullable=True))
    op.create_index(
        "uq_conversations_tenant_agent_external_thread",
        "conversations",
        ["tenant_id", "agent_id", "external_thread_id"],
        unique=True,
        postgresql_where=sa.text("external_thread_id IS NOT NULL"),
    )

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
        sa.Column("from_order", sa.BigInteger(), nullable=True),
        sa.Column("end_order", sa.BigInteger(), nullable=True),
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

    # `runs`: catalog-only column adds (a constant default is catalog-only too, PG 11+), the
    # checks NOT VALID.
    op.add_column(
        "runs", sa.Column("event_order_next", sa.BigInteger(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column("runs", sa.Column("client_tools", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("runs", sa.Column("client_tool_results", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint("ck_runs_client_tools", "runs", _CLIENT_TOOLS_CHECK, postgresql_not_valid=True)
    op.create_check_constraint(
        "ck_runs_client_tool_results", "runs", _CLIENT_TOOL_RESULTS_CHECK, postgresql_not_valid=True
    )

    # `execution_events` last (after `runs`, as every appender locks them).
    op.add_column("execution_events", sa.Column("root_order", sa.BigInteger(), nullable=True))
    op.create_index(
        "ix_execution_events_run_root_order",
        "execution_events",
        ["run_id", "root_order"],
        unique=False,
        postgresql_where=sa.text("root_order IS NOT NULL"),
    )
    op.execute(_ROOT_ORDER_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_execution_events_root_order BEFORE INSERT ON execution_events"
        " FOR EACH ROW EXECUTE FUNCTION execution_events_root_order()"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The same lock order as the upgrade: conversations, runs (with ag_ui_runs), execution_events.
    op.drop_index("uq_conversations_tenant_agent_external_thread", table_name="conversations")
    op.drop_column("conversations", "external_thread_id")

    op.drop_index("ix_ag_ui_runs_tenant_run", table_name="ag_ui_runs")
    op.drop_table("ag_ui_runs")
    op.drop_constraint("ck_runs_client_tool_results", "runs", type_="check")
    op.drop_constraint("ck_runs_client_tools", "runs", type_="check")
    op.drop_column("runs", "client_tool_results")
    op.drop_column("runs", "client_tools")
    op.drop_column("runs", "event_order_next")

    op.execute("DROP TRIGGER IF EXISTS trg_execution_events_root_order ON execution_events")
    op.execute("DROP FUNCTION IF EXISTS execution_events_root_order()")
    op.drop_index("ix_execution_events_run_root_order", table_name="execution_events")
    op.drop_column("execution_events", "root_order")
