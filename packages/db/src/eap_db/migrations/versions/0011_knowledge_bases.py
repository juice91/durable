"""Knowledge bases: pgvector chunks with partial HNSW indexes, `kb_ingest` runs, usage operation.

Revision ID: 0011_knowledge_bases
Revises: 0010_run_options
Create Date: 2026-11-13 12:00:00

- The `vector` extension must exist in `public` (created by the admin role: the init script, or
  `scripts/dev-up.sh` for an existing volume). `CREATE EXTENSION IF NOT EXISTS vector SCHEMA public`
  is a no-op then; when it is missing, the app role cannot create it and the upgrade fails with
  the fix. pgvector 0.8.0 or later is required (iterative index scans).
- `knowledge_bases`: tenant-scoped, UUID ids, name unique per tenant (the retrieve tool offers the
  attached knowledge bases by name). The embedding model and dimensions are fixed per generation;
  `active_generation` is what retrieval reads, and a reindex fills `pending_generation` with
  `pending_embedding_model` / `pending_dimensions` until it switches. `last_generation` is the
  highest generation number ever opened: a reindex always takes `last_generation + 1`, so a
  number is never reused after a cancelled reindex.
- `kb_documents`: one row per `(tenant_id, kb_id, document_key)`, the latest uploaded content.
  `latest_version` counts uploads that changed something; `active_version` (NULL until the first
  ingest finalises) is the version retrieval reads.
- `kb_chunks`: chunk rows per `(document_id, doc_version, generation, ordinal)` (the idempotent
  upsert key). `embedding` is an untyped `public.vector`, NULL until embedded, with `dimensions`;
  `ck_kb_chunks_embedding` keeps the two consistent: both NULL, or both set with
  `vector_dims(embedding) = dimensions`, one of 768, 1024, 1536 (explicit IS NOT NULL terms: a
  NULL `dimensions` would otherwise make the check NULL, which passes). One partial expression HNSW index per dimension:
  `((embedding::public.vector(N)) public.vector_cosine_ops) WITH (m = 16, ef_construction = 64)
  WHERE dimensions = N`; retrieval uses the same cast and predicate.
- Composite tenant FKs everywhere; chunks cascade from documents and from knowledge bases,
  documents from knowledge bases.
- `runs.knowledge_base_id` (UUID, no FK: a run outlives its knowledge base as the audit and cost
  record) and kind `kb_ingest` in `ck_runs_kind_target`: a `kb_ingest` run names its knowledge base
  only. A tool run may name one too (the search tester); agent and workflow runs never do.
  Index `(tenant_id, knowledge_base_id, created_at)` for a knowledge base's runs and cost.
- `run_model_requests.operation` (`chat` | `embedding`, server default `chat`).

Downgrade deletes `kb_ingest` runs with their events, usage rows and side_effects reservations,
keeps other runs' embedding usage rows (they are in those runs' totals), restores the 0010 check, drops the columns and tables
(all knowledge base content is lost), and keeps the extension (shared per database).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0011_knowledge_bases"
down_revision: Union[str, Sequence[str], None] = "0010_run_options"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same text as eap_run_store.store.RUN_KIND_TARGET_CHECK (a migration never imports app code).
_KIND_TARGET_CHECK = (
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
# 0010's check, restored by the downgrade.
_KIND_TARGET_CHECK_0010 = (
    "(kind = 'agent' AND agent_id IS NOT NULL AND (agent_version IS NULL) <> (draft_spec IS NULL)"
    " AND tool_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'tool' AND tool_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND draft_spec IS NULL AND conversation_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND workflow_version IS NOT NULL"
    " AND agent_id IS NULL AND agent_version IS NULL AND draft_spec IS NULL AND tool_id IS NULL"
    " AND conversation_id IS NULL)"
)
# Same texts as eap_run_store.knowledge (inline, no app import).
_KB_DIMENSIONS_CHECK = "dimensions IN (768, 1024, 1536)"
_KB_PENDING_CHECK = (
    "(status = 'ready' AND pending_generation IS NULL AND pending_embedding_model IS NULL"
    " AND pending_dimensions IS NULL)"
    " OR (status IN ('reindexing', 'reindex_failed') AND pending_generation IS NOT NULL"
    " AND pending_generation > active_generation AND pending_embedding_model IS NOT NULL"
    " AND pending_dimensions IN (768, 1024, 1536))"
)
_KB_GENERATIONS_CHECK = (
    "active_generation >= 1 AND active_generation <= last_generation"
    " AND (pending_generation IS NULL OR pending_generation <= last_generation)"
)
_DOC_STATUS_CHECK = "status IN ('pending', 'ingesting', 'ready', 'failed')"
_DOC_VERSION_CHECK = "latest_version >= 1 AND (active_version IS NULL OR active_version <= latest_version)"
_CHUNK_EMBEDDING_CHECK = (
    "(embedding IS NULL AND dimensions IS NULL)"
    " OR (embedding IS NOT NULL AND dimensions IS NOT NULL AND dimensions IN (768, 1024, 1536)"
    " AND public.vector_dims(embedding) = dimensions)"
)
HNSW_DIMENSIONS = (768, 1024, 1536)
MIN_PGVECTOR = (0, 8, 0)


def _ensure_vector_extension() -> None:
    if op.get_context().as_sql:
        # Offline (`alembic upgrade --sql`): no connection to check the installed extension with.
        # The generated script creates it (a superuser must run that line, or create it first) and
        # cannot check the pgvector version (0.8.0 or later): the online upgrade does both.
        op.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
        return
    conn = op.get_bind()
    row = conn.execute(
        sa.text(
            "SELECT e.extversion, n.nspname FROM pg_extension e"
            " JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
        )
    ).first()
    if row is None:
        try:
            with conn.begin_nested():
                conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public"))
        except sa.exc.DBAPIError as exc:
            raise RuntimeError(
                "migration 0011_knowledge_bases needs the pgvector extension, and this role cannot create it. "
                "Create it once as a superuser in this database: CREATE EXTENSION IF NOT EXISTS vector SCHEMA public; "
                "(in dev: scripts/dev-up.sh does it; see packages/db/README.md, pgvector)"
            ) from exc
        row = conn.execute(
            sa.text(
                "SELECT e.extversion, n.nspname FROM pg_extension e"
                " JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
            )
        ).one()
    version, schema = row
    if schema != "public":
        raise RuntimeError(f"the vector extension must live in schema public, found it in {schema!r}")
    parts = tuple(int(p) for p in str(version).split(".")[:3] if p.isdigit())
    if parts < MIN_PGVECTOR:
        raise RuntimeError(
            f"pgvector {version} is too old: 0.8.0 or later is required (iterative index scans); "
            "upgrade the image (pgvector/pgvector:0.8.x-pg17) and run ALTER EXTENSION vector UPDATE as a superuser"
        )


def upgrade() -> None:
    _ensure_vector_extension()
    uuid = postgresql.UUID(as_uuid=False)
    now = sa.text("now()")

    op.create_table(
        "knowledge_bases",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("dimensions", sa.SmallInteger(), nullable=False),
        sa.Column("chunking", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active_generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("pending_generation", sa.Integer(), nullable=True),
        sa.Column("pending_embedding_model", sa.String(), nullable=True),
        sa.Column("pending_dimensions", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'ready'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.PrimaryKeyConstraint("id", name="knowledge_bases_pkey"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_knowledge_bases_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_knowledge_bases_tenant_name"),
        sa.CheckConstraint(_KB_DIMENSIONS_CHECK, name="ck_knowledge_bases_dimensions"),
        sa.CheckConstraint(_KB_PENDING_CHECK, name="ck_knowledge_bases_pending"),
        sa.CheckConstraint(_KB_GENERATIONS_CHECK, name="ck_knowledge_bases_generations"),
    )
    op.create_index(
        "ix_knowledge_bases_tenant_updated", "knowledge_bases", ["tenant_id", "updated_at"], unique=False
    )

    op.create_table(
        "kb_documents",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("kb_id", uuid, nullable=False),
        sa.Column("document_key", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=True),
        sa.Column("latest_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_run_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.PrimaryKeyConstraint("id", name="kb_documents_pkey"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_kb_documents_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "kb_id", "document_key", name="uq_kb_documents_tenant_kb_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "kb_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_kb_documents_tenant_kb",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_DOC_STATUS_CHECK, name="ck_kb_documents_status"),
        sa.CheckConstraint(_DOC_VERSION_CHECK, name="ck_kb_documents_versions"),
    )
    op.create_index(
        "ix_kb_documents_tenant_kb_updated", "kb_documents", ["tenant_id", "kb_id", "updated_at"], unique=False
    )

    op.create_table(
        "kb_chunks",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("kb_id", uuid, nullable=False),
        sa.Column("document_id", uuid, nullable=False),
        sa.Column("doc_version", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("anchor_url", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embed_text", sa.Text(), nullable=False),
        sa.Column("embed_text_hash", sa.String(), nullable=False),
        sa.Column("approx_tokens", sa.Integer(), nullable=False),
        sa.Column("overlap", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("dimensions", sa.SmallInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.PrimaryKeyConstraint("id", name="kb_chunks_pkey"),
        sa.UniqueConstraint(
            "tenant_id", "document_id", "doc_version", "generation", "ordinal", name="uq_kb_chunks_document_slot"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "kb_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_kb_chunks_tenant_kb",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["kb_documents.tenant_id", "kb_documents.id"],
            name="fk_kb_chunks_tenant_document",
            ondelete="CASCADE",
        ),
    )
    # pgvector's SQLAlchemy type renders an unqualified VECTOR, which does not resolve with the
    # migration's single-schema search path: the column and its check are added in SQL.
    op.execute("ALTER TABLE kb_chunks ADD COLUMN embedding public.vector")
    op.create_check_constraint("ck_kb_chunks_embedding", "kb_chunks", _CHUNK_EMBEDDING_CHECK)
    op.create_index(
        "ix_kb_chunks_tenant_kb_generation", "kb_chunks", ["tenant_id", "kb_id", "generation", "document_id"], unique=False
    )
    op.create_index(
        "ix_kb_chunks_tenant_kb_generation_hash",
        "kb_chunks",
        ["tenant_id", "kb_id", "generation", "embed_text_hash"],
        unique=False,
    )
    # The un-embedded chunks of a generation, in batch order (next batch, progress, cache fill).
    op.create_index(
        "ix_kb_chunks_unembedded",
        "kb_chunks",
        ["tenant_id", "kb_id", "generation", "document_id", "doc_version", "ordinal"],
        unique=False,
        postgresql_where=sa.text("embedding IS NULL"),
    )
    for dims in HNSW_DIMENSIONS:
        op.execute(
            f"CREATE INDEX ix_kb_chunks_embedding_{dims} ON kb_chunks USING hnsw "
            f"((embedding::public.vector({dims})) public.vector_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64) WHERE dimensions = {dims}"
        )

    op.add_column("runs", sa.Column("knowledge_base_id", uuid, nullable=True))
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK)
    op.create_index(
        "ix_runs_tenant_kb_created", "runs", ["tenant_id", "knowledge_base_id", "created_at"], unique=False
    )
    op.add_column(
        "run_model_requests",
        sa.Column("operation", sa.String(), server_default=sa.text("'chat'"), nullable=False),
    )
    op.create_check_constraint(
        "ck_run_model_requests_operation", "run_model_requests", "operation IN ('chat', 'embedding')"
    )


def downgrade() -> None:
    # Doomed runs: kb_ingest runs. chr(58) = ':' (no bind-param parsing).
    doomed = "SELECT id, tenant_id FROM runs WHERE kind = 'kb_ingest'"
    op.execute(
        f"DELETE FROM side_effects s USING ({doomed}) r"
        " WHERE s.tenant_id = r.tenant_id AND left(s.key, length(r.id) + 1) = r.id || chr(58)"
    )
    op.execute(f"DELETE FROM run_model_requests WHERE run_id IN (SELECT id FROM ({doomed}) d)")
    # Embedding usage rows of other runs (retrieve, the search tester) stay: they are part of those
    # runs' recorded totals, and read as model requests without the operation column.
    op.execute(f"DELETE FROM execution_events WHERE run_id IN (SELECT id FROM ({doomed}) d)")
    op.execute("DELETE FROM runs WHERE kind = 'kb_ingest'")

    op.drop_constraint("ck_run_model_requests_operation", "run_model_requests", type_="check")
    op.drop_column("run_model_requests", "operation")
    op.drop_index("ix_runs_tenant_kb_created", table_name="runs")
    op.drop_constraint("ck_runs_kind_target", "runs", type_="check")
    op.create_check_constraint("ck_runs_kind_target", "runs", _KIND_TARGET_CHECK_0010)
    op.drop_column("runs", "knowledge_base_id")

    op.drop_table("kb_chunks")  # its indexes, HNSW included, go with it
    op.drop_index("ix_kb_documents_tenant_kb_updated", table_name="kb_documents")
    op.drop_table("kb_documents")
    op.drop_index("ix_knowledge_bases_tenant_updated", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
    # The vector extension stays: it is per database and shared with every other schema.
