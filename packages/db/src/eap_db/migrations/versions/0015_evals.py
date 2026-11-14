"""Evals: test suites, their cases, per-run case results, `runs.eval_suite_id` and run kind `eval`.

Revision ID: 0015_evals
Revises: 0014_approvals
Create Date: 2026-11-15 14:00:00

- `eval_suites`: an agent's editable test suite (working data). `evaluators` is the suite-level
  evaluator list (JSONB array of `eap_schemas.evals` evaluator specs). Unique
  `(tenant_id, agent_id, name)` (archived suites keep their name). `archived_at` set = archived.
  `agent_id` has no FK (agents live in the API's metadata and have no `(tenant_id, id)` key; like
  `runs.agent_id`).
- `eval_cases`: one case of a suite: `input` (≤ 32 KiB UTF-8), `expected_output` (JSONB, SQL NULL =
  none), `metadata` (a flat JSON object), its own `evaluators`, and `position` (execution order, then
  id). Unique `(suite_id, name)`. Composite tenant FK to the suite, ON DELETE CASCADE.
- `eval_case_results`: one row per case of an eval run, immutable once written (first writer wins
  per `(tenant_id, run_id, case_id)`). `case_name` and `position` are copied from the run's snapshot,
  so a result outlives its case being edited or deleted (no FK to `eval_cases`). `child_run_id` is the
  case's child agent run. `status` is `passed` | `failed` | `error`, and `error_code` is set exactly
  for `error`. Composite tenant FK to the eval run, ON DELETE CASCADE.
- `runs.eval_suite_id`: the suite an `eval` run executes (no FK: the run outlives a deleted suite as
  its audit and cost record; its snapshot is `runs.input`), and `runs.eval_suite_hash`: the
  snapshot's `suite_hash()`, recorded once at creation so comparing runs never re-validates an old
  snapshot (NULL for every other kind). Partial index
  `(tenant_id, eval_suite_id, created_at) WHERE eval_suite_id IS NOT NULL` for a suite's runs.
- Run kind `eval` in `ck_runs_kind_target`: an eval run names its agent with exactly one of
  `agent_version` and `draft_spec`, and its suite, and no tool, workflow, knowledge base or
  conversation. The other kinds' texts are unchanged (copied inline, `kb_ingest` included), so every
  existing run satisfies the new check.

Lock footprint: `lock_timeout` is 5 s (a busy `runs` fails the upgrade fast instead of queueing
every run write behind it). The new tables come first, then `runs`:
- the two column adds have no default, so they are catalog-only, but they take ACCESS EXCLUSIVE on
  `runs` until the migration commits;
- the partial index is built with a plain CREATE INDEX (CONCURRENTLY cannot run inside the
  migration's transaction), so it scans `runs` once while that lock is held; the predicate keeps
  the index itself empty at upgrade time;
- the new `ck_runs_kind_target` is added NOT VALID (no second scan): its text is the 0014 text
  plus an `eval` alternative, a strict superset, so every existing row already satisfies it, and
  new writes are checked. As in 0014, it is never validated in this migration.

Every check text is inline (same texts as eap_run_store.evals and eap_run_store.store).

Downgrade: eval runs are deleted with their events, usage rows (judge requests), side_effects
reservations and approval rows; their child agent runs are kept, detached from the parent (they
become standalone agent runs with their own events and cost). Then the 0014 kind check is restored
and the column and tables are dropped: every suite, case and result is lost.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0015_evals"
down_revision: Union[str, Sequence[str], None] = "0014_approvals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.store.RUN_KIND_TARGET_CHECK (a migration never imports app code).
_KIND_TARGET_CHECK_0014 = (
    "(kind = 'agent' AND agent_id IS NOT NULL AND (agent_version IS NULL) <> (draft_spec IS NULL)"
    " AND tool_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL AND knowledge_base_id IS NULL)"
    " OR (kind = 'tool' AND tool_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND draft_spec IS NULL AND conversation_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND workflow_version IS NOT NULL"
    " AND agent_id IS NULL AND agent_version IS NULL AND draft_spec IS NULL AND tool_id IS NULL"
    " AND conversation_id IS NULL AND knowledge_base_id IS NULL)"
    " OR (kind = 'kb_ingest' AND knowledge_base_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND draft_spec IS NULL AND tool_id IS NULL AND conversation_id IS NULL AND workflow_id IS NULL"
    " AND workflow_version IS NULL)"
)
_KIND_TARGET_CHECK = (
    _KIND_TARGET_CHECK_0014
    + " OR (kind = 'eval' AND agent_id IS NOT NULL AND (agent_version IS NULL) <> (draft_spec IS NULL)"
    " AND eval_suite_id IS NOT NULL AND tool_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL"
    " AND knowledge_base_id IS NULL AND conversation_id IS NULL)"
)
# Same texts as eap_run_store.evals.
_CASE_INPUT_CHECK = "octet_length(input) <= 32768"
_CASE_POSITION_CHECK = "position >= 0"
_CASE_METADATA_CHECK = "jsonb_typeof(metadata) = 'object'"
_SUITE_EVALUATORS_CHECK = "jsonb_typeof(evaluators) = 'array'"
_CASE_EVALUATORS_CHECK = "jsonb_typeof(evaluators) = 'array'"
_RESULT_STATUS_CHECK = "status IN ('passed', 'failed', 'error')"
_RESULT_ERROR_CHECK = "(status = 'error') = (error_code IS NOT NULL)"
_RESULT_OBJECTS_CHECK = (
    "jsonb_typeof(assertions) = 'object' AND jsonb_typeof(scores) = 'object' AND jsonb_typeof(labels) = 'object'"
    " AND jsonb_typeof(evaluator_failures) = 'array'"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.create_table(
        "eval_suites",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "evaluators", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="eval_suites_pkey"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_eval_suites_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "agent_id", "name", name="uq_eval_suites_tenant_agent_name"),
        sa.CheckConstraint(_SUITE_EVALUATORS_CHECK, name="ck_eval_suites_evaluators"),
    )

    op.create_table(
        "eval_cases",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("suite_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("expected_output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "evaluators", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="eval_cases_pkey"),
        sa.UniqueConstraint("suite_id", "name", name="uq_eval_cases_suite_name"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "suite_id"],
            ["eval_suites.tenant_id", "eval_suites.id"],
            name="fk_eval_cases_tenant_suite",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_CASE_INPUT_CHECK, name="ck_eval_cases_input_size"),
        sa.CheckConstraint(_CASE_POSITION_CHECK, name="ck_eval_cases_position"),
        sa.CheckConstraint(_CASE_METADATA_CHECK, name="ck_eval_cases_metadata"),
        sa.CheckConstraint(_CASE_EVALUATORS_CHECK, name="ck_eval_cases_evaluators"),
    )
    # A suite's cases in execution order (and the suite FK's cascade).
    op.create_index(
        "ix_eval_cases_tenant_suite_position", "eval_cases", ["tenant_id", "suite_id", "position"], unique=False
    )

    op.create_table(
        "eval_case_results",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("case_name", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("child_run_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "assertions", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "scores", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "labels", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "evaluator_failures",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(20, 10), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "run_id", "case_id", name="eval_case_results_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_eval_case_results_tenant_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_RESULT_STATUS_CHECK, name="ck_eval_case_results_status"),
        sa.CheckConstraint(_RESULT_ERROR_CHECK, name="ck_eval_case_results_error"),
        sa.CheckConstraint(_RESULT_OBJECTS_CHECK, name="ck_eval_case_results_json"),
    )

    # `runs` last: catalog-only column adds, the partial index (one scan), the check NOT VALID.
    op.add_column("runs", sa.Column("eval_suite_id", postgresql.UUID(as_uuid=False), nullable=True))
    op.add_column("runs", sa.Column("eval_suite_hash", sa.String(), nullable=True))
    op.create_index(
        "ix_runs_tenant_eval_suite_created",
        "runs",
        ["tenant_id", "eval_suite_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("eval_suite_id IS NOT NULL"),
    )
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK, postgresql_not_valid=True)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Eval runs go; their child agent runs stay as standalone agent runs (ck_runs_parent_link
    # allows a run without parent and step).
    op.execute(
        "UPDATE runs c SET parent_run_id = NULL, parent_step_id = NULL FROM runs p"
        " WHERE p.kind = 'eval' AND c.tenant_id = p.tenant_id AND c.parent_run_id = p.id"
    )
    op.execute(
        "DELETE FROM side_effects s USING runs r"
        " WHERE r.kind = 'eval' AND s.tenant_id = r.tenant_id AND s.key LIKE r.id || ':%'"
    )
    op.execute(
        "DELETE FROM run_model_requests q USING runs r"
        " WHERE r.kind = 'eval' AND q.tenant_id = r.tenant_id AND q.run_id = r.id"
    )
    op.execute(
        "DELETE FROM execution_events e USING runs r"
        " WHERE r.kind = 'eval' AND e.tenant_id = r.tenant_id AND e.run_id = r.id"
    )
    # pending_approvals and eval_case_results rows cascade with the run.
    op.execute("DELETE FROM runs WHERE kind = 'eval'")

    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK_0014)
    op.drop_index("ix_runs_tenant_eval_suite_created", table_name="runs")
    op.drop_column("runs", "eval_suite_hash")
    op.drop_column("runs", "eval_suite_id")

    op.drop_table("eval_case_results")
    op.drop_index("ix_eval_cases_tenant_suite_position", table_name="eval_cases")
    op.drop_table("eval_cases")
    op.drop_table("eval_suites")
