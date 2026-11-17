"""Knowledge-base source sync: connections, sources, syncs, records, entities, and run kind `kb_source_sync`.

Revision ID: 0029_kb_sources
Revises: 0028_mcp_catalog_source
Create Date: 2026-11-18 18:00:00

- `settings_connections`: a tenant's connection to an external provider. `config` is never secret;
  `credential_refs` maps logical names to `secret:<name>` references (resolved by the step that uses them).
  PK `(tenant_id, id)`; unique `(tenant_id, name)`.
- `kb_sources`: binds a connection to a knowledge base. Tenant-composite FKs: to `knowledge_bases` (CASCADE)
  and to the connection (RESTRICT: a connection in use cannot be deleted). `last_checkpoint` is at most 8 KiB.
- `kb_source_syncs`: one row per sync run (`run_id` = the run's id, unique per tenant; no FK to `runs`).
- `kb_source_records`: raw provider payloads, unique `(tenant, source, external_id, record_type)`.
- `kb_entities`, `kb_entity_identifiers`, `kb_entity_parameters`, `kb_entity_documents`: the structured store
  and the document ledger between discovery and `eap.kb_ingest`. Everything cascades with its source.
- `ck_runs_kind_target` gains kind `kb_source_sync`: it names its knowledge base (the source's) only, like
  `kb_ingest`. Added NOT VALID (the new text is the old one plus a branch, so existing rows satisfy it).

Lock footprint: eight new tables, one constraint swap on `runs` (no rewrite, no validation scan).

Downgrade: every row of these tables is lost; `kb_source_sync` runs are deleted (events, usage and side-effect
rows cascade); other runs keep their data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0029_kb_sources"
down_revision: Union[str, Sequence[str], None] = "0028_mcp_catalog_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.kb_sources (a migration never imports app code).
_PROVIDER_CHECK = "provider ~ '^[a-z][a-z0-9_]{0,63}$'"

# eap_run_store.store.RUN_KIND_TARGET_CHECK as of 0022 (without the kb_source_sync branch).
_KIND_TARGET_CHECK_0022 = (
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
    " OR (kind = 'eval' AND agent_id IS NOT NULL AND (agent_version IS NULL) <> (draft_spec IS NULL)"
    " AND eval_suite_id IS NOT NULL AND tool_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL"
    " AND knowledge_base_id IS NULL AND conversation_id IS NULL)"
    " OR (kind = 'eval' AND agent_id IS NULL AND agent_version IS NULL AND draft_spec IS NULL"
    " AND workflow_id IS NOT NULL AND workflow_version IS NOT NULL AND eval_suite_id IS NOT NULL"
    " AND tool_id IS NULL AND knowledge_base_id IS NULL AND conversation_id IS NULL)"
)
_KIND_TARGET_CHECK = (
    _KIND_TARGET_CHECK_0022
    + " OR (kind = 'kb_source_sync' AND knowledge_base_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND draft_spec IS NULL AND tool_id IS NULL AND conversation_id IS NULL AND workflow_id IS NULL"
    " AND workflow_version IS NULL AND eval_suite_id IS NULL)"
)


def _now() -> sa.TextClause:
    return sa.text("now()")


def _fk(cols, refcols, name, ondelete="CASCADE"):
    return sa.ForeignKeyConstraint(cols, refcols, name=name, ondelete=ondelete)


def _jsonb(name: str, default: str = "{}") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "settings_connections",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        _jsonb("config"),
        _jsonb("credential_refs"),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(), nullable=True),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="settings_connections_pkey"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_settings_connections_tenant_name"),
        sa.CheckConstraint("kind IN ('oauth2_client_credentials')", name="ck_settings_connections_kind"),
        sa.CheckConstraint("last_status IS NULL OR last_status IN ('ok', 'error')", name="ck_settings_connections_status"),
    )
    op.create_table(
        "kb_sources",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("knowledge_base_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        _jsonb("config"),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_successful_sync_at", sa.DateTime(timezone=True), nullable=True),
        _jsonb("last_checkpoint"),
        sa.Column("last_source_revision", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="kb_sources_pkey"),
        sa.UniqueConstraint("tenant_id", "knowledge_base_id", "name", name="uq_kb_sources_tenant_kb_name"),
        _fk(["tenant_id", "knowledge_base_id"], ["knowledge_bases.tenant_id", "knowledge_bases.id"], "fk_kb_sources_tenant_kb"),
        _fk(
            ["tenant_id", "connection_id"], ["settings_connections.tenant_id", "settings_connections.id"],
            "fk_kb_sources_tenant_connection", "RESTRICT",
        ),
        sa.CheckConstraint("kind IN ('product_catalog')", name="ck_kb_sources_kind"),
        sa.CheckConstraint(_PROVIDER_CHECK, name="ck_kb_sources_provider"),
        sa.CheckConstraint("octet_length(last_checkpoint::text) <= 8192", name="ck_kb_sources_checkpoint_size"),
    )
    op.create_index("ix_kb_sources_tenant_kb", "kb_sources", ["tenant_id", "knowledge_base_id"])
    op.create_index("ix_kb_sources_tenant_connection", "kb_sources", ["tenant_id", "connection_id"])
    op.create_table(
        "kb_source_syncs",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("parent_run_id", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'running'"), nullable=False),
        _jsonb("checkpoint"),
        _jsonb("counters"),
        _jsonb("bounded_errors", "[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="kb_source_syncs_pkey"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_kb_source_syncs_tenant_run"),
        _fk(["tenant_id", "source_id"], ["kb_sources.tenant_id", "kb_sources.id"], "fk_kb_source_syncs_tenant_source"),
        sa.CheckConstraint("mode IN ('targeted', 'incremental', 'full')", name="ck_kb_source_syncs_mode"),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'incomplete', 'failed', 'cancelled', 'needs_attention')",
            name="ck_kb_source_syncs_status",
        ),
    )
    op.create_index("ix_kb_source_syncs_source_started", "kb_source_syncs", ["tenant_id", "source_id", "started_at"])
    op.create_table(
        "kb_source_records",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("record_type", sa.String(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="kb_source_records_pkey"),
        sa.UniqueConstraint(
            "tenant_id", "source_id", "external_id", "record_type", name="uq_kb_source_records_tenant_source_ext_type"
        ),
        _fk(["tenant_id", "source_id"], ["kb_sources.tenant_id", "kb_sources.id"], "fk_kb_source_records_tenant_source"),
        sa.CheckConstraint("status IN ('active', 'removed')", name="ck_kb_source_records_status"),
    )
    op.create_table(
        "kb_entities",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("lifecycle_status", sa.String(), nullable=True),
        _jsonb("attributes"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="kb_entities_pkey"),
        sa.UniqueConstraint(
            "tenant_id", "source_id", "entity_type", "canonical_name", name="uq_kb_entities_tenant_source_type_name"
        ),
        _fk(["tenant_id", "source_id"], ["kb_sources.tenant_id", "kb_sources.id"], "fk_kb_entities_tenant_source"),
    )
    op.create_table(
        "kb_entity_identifiers",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("scheme", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "entity_id", "scheme", "value", name="kb_entity_identifiers_pkey"),
        _fk(["tenant_id", "entity_id"], ["kb_entities.tenant_id", "kb_entities.id"], "fk_kb_entity_identifiers_tenant_entity"),
    )
    op.create_index("ix_kb_entity_identifiers_scheme_value", "kb_entity_identifiers", ["tenant_id", "scheme", "value"])
    op.create_table(
        "kb_entity_parameters",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("source_name", sa.String(), nullable=False),
        sa.Column("value_type", sa.String(), nullable=False),
        sa.Column("numeric_value", sa.Float(53), nullable=True),
        sa.Column("text_value", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("normalized_value", sa.Float(53), nullable=True),
        sa.Column("normalized_unit", sa.String(), nullable=True),
        sa.Column("min_value", sa.Float(53), nullable=True),
        sa.Column("typ_value", sa.Float(53), nullable=True),
        sa.Column("max_value", sa.Float(53), nullable=True),
        sa.Column("test_conditions", sa.Text(), nullable=True),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(53), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "id", name="kb_entity_parameters_pkey"),
        sa.UniqueConstraint(
            "tenant_id", "entity_id", "canonical_name", "source_name", name="uq_kb_entity_parameters_entity_name"
        ),
        _fk(["tenant_id", "entity_id"], ["kb_entities.tenant_id", "kb_entities.id"], "fk_kb_entity_parameters_tenant_entity"),
        sa.CheckConstraint("value_type IN ('numeric', 'text', 'boolean', 'range')", name="ck_kb_entity_parameters_type"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_kb_entity_parameters_confidence"
        ),
    )
    op.create_index(
        "ix_kb_entity_parameters_canonical", "kb_entity_parameters", ["tenant_id", "canonical_name", "normalized_value"]
    )
    op.create_table(
        "kb_entity_documents",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("document_key", sa.String(), nullable=False),
        sa.Column("kb_document_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("relationship", sa.String(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'discovered'"), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "entity_id", "document_key", name="kb_entity_documents_pkey"),
        _fk(["tenant_id", "entity_id"], ["kb_entities.tenant_id", "kb_entities.id"], "fk_kb_entity_documents_tenant_entity"),
        _fk(
            ["tenant_id", "kb_document_id"], ["kb_documents.tenant_id", "kb_documents.id"],
            "fk_kb_entity_documents_tenant_document",
        ),
        sa.CheckConstraint("status IN ('discovered', 'queued', 'failed')", name="ck_kb_entity_documents_status"),
        sa.CheckConstraint("char_length(document_key) BETWEEN 1 AND 256", name="ck_kb_entity_documents_key"),
    )
    op.create_index("ix_kb_entity_documents_pending", "kb_entity_documents", ["tenant_id", "status", "discovered_at"])

    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK, postgresql_not_valid=True)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM runs WHERE kind = 'kb_source_sync'")  # events, usage, side effects cascade
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK_0022)
    for table in (
        "kb_entity_documents", "kb_entity_parameters", "kb_entity_identifiers", "kb_entities",
        "kb_source_records", "kb_source_syncs", "kb_sources", "settings_connections",
    ):
        op.drop_table(table)
