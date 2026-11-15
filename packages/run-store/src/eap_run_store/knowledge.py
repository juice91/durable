"""Knowledge bases in Postgres with pgvector (RAG and memory milestone, RM.4; migration 0011).

Tables (Alembic owns them; the models here are what `alembic check` compares):

- `knowledge_bases`: tenant-scoped, embedding model and dimensions fixed per generation.
  `active_generation` is what retrieval reads; a reindex fills `pending_generation` with
  `pending_embedding_model` / `pending_dimensions` and switches in one transaction.
- `kb_documents`: the latest uploaded content of `(tenant_id, kb_id, document_key)`.
  `latest_version` counts changing uploads, `active_version` is the version retrieval reads (NULL
  until the first ingest finalises).
- `kb_chunks`: chunk rows per `(document_id, doc_version, generation, ordinal)`, embedding NULL
  until embedded. Partial expression HNSW index per dimension (768, 1024, 1536).

Every method takes the tenant id from trusted input (the principal, the run's deps) and filters by
it in SQL; nothing here accepts a tenant from a model or a request body. The store never embeds:
the worker (RM.6, RM.10, RM.11) passes vectors in. It imports no pydantic_ai, dbos or eap_knowledge:
chunks are any objects with the `eap_knowledge.Chunk` attributes (`ChunkInput` is a plain one).

Concurrency rules. **Lock order: the knowledge base row first, then the document row, then chunk
rows.** No method locks a document and then waits for its knowledge base:
- `put_document`: a re-upload locks only its document (`FOR NO KEY UPDATE`); a new key locks the
  knowledge base `FOR UPDATE` (the 2,000 document cap holds exactly) and then looks for the
  document again. The 200,000 chunk cap is soft (see `upsert_chunks`).
- `upsert_chunks` and `finalize_document` read the document's kb_id without a lock, lock the
  knowledge base `FOR SHARE`, then lock the document (`FOR SHARE` / `FOR NO KEY UPDATE`) and
  re-check it. A version switch therefore never interleaves with a generation switch.
- `start_reindex`, `finalize_reindex` and `cancel_reindex` lock the knowledge base `FOR UPDATE`;
  their chunk inserts take only `FOR KEY SHARE` on documents (foreign keys), which does not
  conflict with `FOR NO KEY UPDATE`.
- `write_embeddings` and `copy_generation_chunks` hold the knowledge base `FOR SHARE`.
- Generation numbers are never reused (`last_generation`), and embedding writes and finalisation
  name the model they embedded with (GenerationModelMismatch otherwise).
- Embedding writes and finalisation append their event (and usage row) in the same transaction
  (`append_event_in_session`), so data, events and cost commit together or not at all.
- Retrieval sets `hnsw.ef_search`, `hnsw.iterative_scan` and `hnsw.max_scan_tuples` with `SET LOCAL`
  semantics (`set_config(..., true)`) inside its own transaction, and re-sorts by exact distance. A
  small knowledge base that the shared index under-fills (fewer than top_k rows) is searched again
  exactly, in the same transaction (EXACT_SEARCH_MAX_CHUNKS).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_schemas import EmbeddingDimensionMismatch, KnowledgeBaseNotFound
from eap_schemas.embedding_policy import ALLOWED_EMBEDDING_DIMENSIONS, parse_embedding_model_id
from eap_schemas.tools import KNOWLEDGE_SEARCH_TOOL_ID
from eap_schemas.knowledge import (
    MAX_CHUNKS_PER_KNOWLEDGE_BASE,
    MAX_DOCUMENTS_PER_KNOWLEDGE_BASE,
    ChunkingConfig,
    validate_filter,
    validate_metadata,
)

from eap_run_store.store import Base, append_event_in_session, set_status_in_session

# Same texts as migration 0011_knowledge_bases.
KB_DIMENSIONS_CHECK = "dimensions IN (768, 1024, 1536)"
KB_PENDING_CHECK = (
    "(status = 'ready' AND pending_generation IS NULL AND pending_embedding_model IS NULL"
    " AND pending_dimensions IS NULL)"
    " OR (status IN ('reindexing', 'reindex_failed') AND pending_generation IS NOT NULL"
    " AND pending_generation > active_generation AND pending_embedding_model IS NOT NULL"
    " AND pending_dimensions IN (768, 1024, 1536))"
)
KB_GENERATIONS_CHECK = (
    "active_generation >= 1 AND active_generation <= last_generation"
    " AND (pending_generation IS NULL OR pending_generation <= last_generation)"
)
DOC_STATUS_CHECK = "status IN ('pending', 'ingesting', 'ready', 'failed')"
DOC_VERSION_CHECK = "latest_version >= 1 AND (active_version IS NULL OR active_version <= latest_version)"
CHUNK_EMBEDDING_CHECK = (
    "(embedding IS NULL AND dimensions IS NULL)"
    " OR (embedding IS NOT NULL AND dimensions IS NOT NULL AND dimensions IN (768, 1024, 1536)"
    " AND public.vector_dims(embedding) = dimensions)"
)

KB_STATUSES = ("ready", "reindexing", "reindex_failed")
DOCUMENT_STATUSES = ("pending", "ingesting", "ready", "failed")
HNSW_DIMENSIONS: tuple[int, ...] = ALLOWED_EMBEDDING_DIMENSIONS  # (768, 1024, 1536)
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64
HNSW_MAX_EF_SEARCH = 1000  # pgvector's upper bound for hnsw.ef_search
MIN_EF_SEARCH = 40
# Iterative HNSW scans stop after this many visited tuples (pgvector's default, set explicitly so the
# bound is ours). The index is shared by every tenant and knowledge base of a dimension, so a small
# knowledge base whose chunks lie behind many nearer foreign rows can exhaust it with fewer than
# top_k hits: the exact-search fallback below covers that case.
HNSW_MAX_SCAN_TUPLES = 20_000
# Exact-search fallback: when the ANN query returns fewer than top_k rows and the attached knowledge
# bases' active generations hold at most this many chunks (and more than were returned), the search
# runs again as an exact scan of those chunks (no HNSW index). Override: EAP_KB_EXACT_SEARCH_MAX_CHUNKS.
EXACT_SEARCH_MAX_CHUNKS = 20_000
EXACT_SEARCH_MAX_CHUNKS_ENV = "EAP_KB_EXACT_SEARCH_MAX_CHUNKS"
MAX_SEARCH_TOP_K = 200
DEFAULT_BATCH_CHUNKS = 64
DEFAULT_BATCH_TOKENS = 100_000
_INSERT_BATCH = 500  # chunk rows per INSERT (17 parameters each, well under psycopg's 65,535)


# ---------------------------------------------------------------------------------------- models


class KnowledgeBaseModel(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    description: Mapped[str | None] = mapped_column("description", Text, nullable=True)
    embedding_model: Mapped[str] = mapped_column("embedding_model", String, nullable=False)
    dimensions: Mapped[int] = mapped_column("dimensions", SmallInteger, nullable=False)
    chunking: Mapped[dict] = mapped_column("chunking", JSONB, nullable=False)
    active_generation: Mapped[int] = mapped_column(
        "active_generation", Integer, nullable=False, server_default=text("1")
    )
    # The highest generation number ever opened: reindexes take last_generation + 1 (never reused).
    last_generation: Mapped[int] = mapped_column("last_generation", Integer, nullable=False, server_default=text("1"))
    pending_generation: Mapped[int | None] = mapped_column("pending_generation", Integer, nullable=True)
    pending_embedding_model: Mapped[str | None] = mapped_column("pending_embedding_model", String, nullable=True)
    pending_dimensions: Mapped[int | None] = mapped_column("pending_dimensions", SmallInteger, nullable=True)
    status: Mapped[str] = mapped_column("status", String, nullable=False, server_default=text("'ready'"))
    error: Mapped[str | None] = mapped_column("error", Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_knowledge_bases_tenant_id_id"),
        # The retrieve tool offers the attached knowledge bases by name.
        UniqueConstraint("tenant_id", "name", name="uq_knowledge_bases_tenant_name"),
        CheckConstraint(KB_DIMENSIONS_CHECK, name="ck_knowledge_bases_dimensions"),
        CheckConstraint(KB_PENDING_CHECK, name="ck_knowledge_bases_pending"),
        CheckConstraint(KB_GENERATIONS_CHECK, name="ck_knowledge_bases_generations"),
        Index("ix_knowledge_bases_tenant_updated", "tenant_id", "updated_at"),
    )


class KbDocumentModel(Base):
    __tablename__ = "kb_documents"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    kb_id: Mapped[str] = mapped_column("kb_id", PG_UUID(as_uuid=False), nullable=False)
    document_key: Mapped[str] = mapped_column("document_key", String, nullable=False)
    title: Mapped[str] = mapped_column("title", Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column("source_url", Text, nullable=True)
    mime_type: Mapped[str] = mapped_column("mime_type", String, nullable=False)
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    content: Mapped[str] = mapped_column("content", Text, nullable=False)
    content_hash: Mapped[str] = mapped_column("content_hash", String, nullable=False)
    active_version: Mapped[int | None] = mapped_column("active_version", Integer, nullable=True)
    latest_version: Mapped[int] = mapped_column("latest_version", Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column("status", String, nullable=False, server_default=text("'pending'"))
    error: Mapped[str | None] = mapped_column("error", Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column("chunk_count", Integer, nullable=False, server_default=text("0"))
    last_run_id: Mapped[str | None] = mapped_column("last_run_id", String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_kb_documents_tenant_id_id"),
        UniqueConstraint("tenant_id", "kb_id", "document_key", name="uq_kb_documents_tenant_kb_key"),
        ForeignKeyConstraint(
            ["tenant_id", "kb_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_kb_documents_tenant_kb",
            ondelete="CASCADE",
        ),
        CheckConstraint(DOC_STATUS_CHECK, name="ck_kb_documents_status"),
        CheckConstraint(DOC_VERSION_CHECK, name="ck_kb_documents_versions"),
        Index("ix_kb_documents_tenant_kb_updated", "tenant_id", "kb_id", "updated_at"),
    )


class KbChunkModel(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[str] = mapped_column(
        "id", PG_UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    kb_id: Mapped[str] = mapped_column("kb_id", PG_UUID(as_uuid=False), nullable=False)
    document_id: Mapped[str] = mapped_column("document_id", PG_UUID(as_uuid=False), nullable=False)
    doc_version: Mapped[int] = mapped_column("doc_version", Integer, nullable=False)
    generation: Mapped[int] = mapped_column("generation", Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column("ordinal", Integer, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(
        "heading_path", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    title: Mapped[str] = mapped_column("title", Text, nullable=False)
    anchor_url: Mapped[str | None] = mapped_column("anchor_url", Text, nullable=True)
    content: Mapped[str] = mapped_column("content", Text, nullable=False)
    embed_text: Mapped[str] = mapped_column("embed_text", Text, nullable=False)
    embed_text_hash: Mapped[str] = mapped_column("embed_text_hash", String, nullable=False)
    approx_tokens: Mapped[int] = mapped_column("approx_tokens", Integer, nullable=False)
    overlap: Mapped[int] = mapped_column("overlap", Integer, nullable=False, server_default=text("0"))
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    dimensions: Mapped[int | None] = mapped_column("dimensions", SmallInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Untyped vector (`public.vector` in the migration): one column for every dimension, with the
    # partial expression HNSW indexes below. Writes cast the text form; the ORM never loads it.
    embedding: Mapped[Any] = mapped_column("embedding", Vector(), nullable=True, deferred=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "document_id", "doc_version", "generation", "ordinal", name="uq_kb_chunks_document_slot"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "kb_id"],
            ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_kb_chunks_tenant_kb",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["kb_documents.tenant_id", "kb_documents.id"],
            name="fk_kb_chunks_tenant_document",
            ondelete="CASCADE",
        ),
        CheckConstraint(CHUNK_EMBEDDING_CHECK, name="ck_kb_chunks_embedding"),
        Index("ix_kb_chunks_tenant_kb_generation", "tenant_id", "kb_id", "generation", "document_id"),
        Index("ix_kb_chunks_tenant_kb_generation_hash", "tenant_id", "kb_id", "generation", "embed_text_hash"),
        # The un-embedded chunks of a generation, in batch order (next batch, progress, cache fill).
        Index(
            "ix_kb_chunks_unembedded",
            "tenant_id", "kb_id", "generation", "document_id", "doc_version", "ordinal",
            postgresql_where=text("embedding IS NULL"),
        ),
        *(
            Index(
                f"ix_kb_chunks_embedding_{dims}",
                literal_column(f"(embedding::public.vector({dims}))").label(f"embedding_{dims}"),
                postgresql_ops={f"embedding_{dims}": "public.vector_cosine_ops"},
                postgresql_using="hnsw",
                postgresql_with={"m": HNSW_M, "ef_construction": HNSW_EF_CONSTRUCTION},
                postgresql_where=text(f"dimensions = {dims}"),
            )
            for dims in HNSW_DIMENSIONS
        ),
    )


def exact_search_max_chunks() -> int:
    """EAP_KB_EXACT_SEARCH_MAX_CHUNKS (default EXACT_SEARCH_MAX_CHUNKS; 0 turns the fallback off)."""
    raw = os.environ.get(EXACT_SEARCH_MAX_CHUNKS_ENV)
    try:
        return max(0, int(raw)) if raw else EXACT_SEARCH_MAX_CHUNKS
    except ValueError:
        return EXACT_SEARCH_MAX_CHUNKS


def check_exact_search_max_chunks() -> None:
    """Startup check (API and worker): EAP_KB_EXACT_SEARCH_MAX_CHUNKS, when set, is an integer
    of 0 or more. ValueError otherwise (searches would silently use the default)."""
    raw = os.environ.get(EXACT_SEARCH_MAX_CHUNKS_ENV)
    if raw is None or not raw.strip():
        return
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{EXACT_SEARCH_MAX_CHUNKS_ENV} must be an integer of 0 or more, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{EXACT_SEARCH_MAX_CHUNKS_ENV} must be an integer of 0 or more, got {raw!r}")


# ---------------------------------------------------------------------------------------- errors


class KnowledgeBaseNameConflict(ValueError):
    """Another knowledge base of the tenant already has this name (API: 409)."""

    code = "knowledge_base_name_conflict"

    def __init__(self, tenant_id: str, name: str) -> None:
        self.tenant_id = tenant_id
        self.name = name
        super().__init__(tenant_id, name)  # pickle-safe

    def __str__(self) -> str:
        return f"a knowledge base named {self.name!r} already exists"


class KnowledgeBaseLimitExceeded(ValueError):
    """A per-knowledge-base cap (2,000 documents, 200,000 chunks) would be exceeded."""

    code = "knowledge_base_limit_exceeded"

    def __init__(self, kb_id: str, what: str, limit: int) -> None:
        self.kb_id = str(kb_id)
        self.what = what
        self.limit = limit
        super().__init__(self.kb_id, what, limit)

    def __str__(self) -> str:
        return f"knowledge base {self.kb_id} would exceed {self.limit} {self.what}"


class DocumentNotFound(LookupError):
    """The document does not exist in the tenant (deleted during an ingest, or never there)."""

    code = "document_not_found"

    def __init__(self, document: str) -> None:
        self.document = str(document)
        super().__init__(self.document)

    def __str__(self) -> str:
        return f"document {self.document} no longer exists"


class IngestRunConflict(RuntimeError):
    """Another request started an ingest run of the document first (compare-and-set lost; API: 409)."""

    code = "ingest_run_conflict"

    def __init__(self, document: str) -> None:
        self.document = str(document)
        super().__init__(self.document)

    def __str__(self) -> str:
        return f"another ingest run of document {self.document} was started concurrently"


class ReindexInProgress(RuntimeError):
    """The knowledge base already has a pending generation (API: 409)."""

    code = "reindex_in_progress"

    def __init__(self, kb_id: str, pending_generation: int) -> None:
        self.kb_id = str(kb_id)
        self.pending_generation = pending_generation
        super().__init__(self.kb_id, pending_generation)

    def __str__(self) -> str:
        return f"knowledge base {self.kb_id} is already reindexing (generation {self.pending_generation})"


class GenerationNotWritable(RuntimeError):
    """`generation` is neither the knowledge base's active nor its pending generation (a reindex was
    switched or cancelled under the caller)."""

    code = "generation_not_writable"

    def __init__(self, kb_id: str, generation: int) -> None:
        self.kb_id = str(kb_id)
        self.generation = generation
        super().__init__(self.kb_id, generation)

    def __str__(self) -> str:
        return f"generation {self.generation} of knowledge base {self.kb_id} is no longer active or pending"


class GenerationModelMismatch(RuntimeError):
    """The generation belongs to another embedding model than the caller embedded with (a run of an
    earlier reindex, or a model change under it): nothing is written."""

    code = "generation_model_mismatch"

    def __init__(self, kb_id: str, generation: int, expected: str, actual: str) -> None:
        self.kb_id = str(kb_id)
        self.generation = generation
        self.expected = expected
        self.actual = actual
        super().__init__(self.kb_id, generation, expected, actual)

    def __str__(self) -> str:
        return (
            f"generation {self.generation} of knowledge base {self.kb_id} is embedded with "
            f"'{self.expected}', not '{self.actual}'"
        )


# ---------------------------------------------------------------------------------- value types


class ChunkLike(Protocol):
    """The attributes of `eap_knowledge.Chunk` the store reads (duck-typed; no import)."""

    ordinal: int
    heading_path: Sequence[str]
    title: str
    text: str
    embed_text: str
    embed_text_hash: str
    approx_tokens: int
    anchor: str | None
    overlap: int


@dataclass(frozen=True)
class ChunkInput:
    """A plain chunk (tests, and callers that do not hold an `eap_knowledge.Chunk`)."""

    ordinal: int
    title: str
    text: str
    embed_text: str
    embed_text_hash: str
    approx_tokens: int
    heading_path: tuple[str, ...] = ()
    anchor: str | None = None
    overlap: int = 0


@dataclass(frozen=True)
class PutDocumentResult:
    document: dict[str, Any]  # the row (without content); document["latest_version"] is what to ingest
    unchanged: bool  # same content, metadata, title, source URL and MIME type: nothing written
    created: bool  # a new document_key


@dataclass(frozen=True)
class UpsertResult:
    total: int  # chunks given
    inserted: int  # rows new in this call (0 on a re-executed step)


@dataclass(frozen=True)
class PendingChunk:
    id: str
    document_id: str
    doc_version: int
    ordinal: int
    embed_text: str
    embed_text_hash: str
    approx_tokens: int


@dataclass(frozen=True)
class EmbeddingProgress:
    total: int
    embedded: int
    pending_tokens: int  # approx_tokens of the chunks still without an embedding

    @property
    def pending(self) -> int:
        return self.total - self.embedded


@dataclass(frozen=True)
class WriteResult:
    written: int  # chunks that got their embedding in this call (0 on a re-executed step)
    event_created: bool | None  # None when no event was asked for


@dataclass(frozen=True)
class FinalizeResult:
    # 'switched' (this call made the version or generation active), 'already_active' (an earlier
    # call did; idempotent), 'incomplete' (chunks still without an embedding, or new ones copied
    # into a reindex generation: embed and finalise again), 'superseded' (a newer version is
    # already active; this version's chunks are dropped), 'stale_generation' (the chunks were
    # written under a generation that is no longer active: chunk again), 'missing' (the document
    # or knowledge base was deleted, or the reindex cancelled).
    status: str
    chunk_count: int = 0
    pending: int = 0


@dataclass(frozen=True)
class ReindexEstimate:
    chunks: int
    approx_tokens: int  # distinct embed texts only (the cache embeds each once)


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    kb_id: str
    document_id: str
    document_key: str
    document_title: str
    doc_version: int
    generation: int
    ordinal: int
    title: str
    heading_path: tuple[str, ...]
    anchor_url: str | None
    content: str
    metadata: dict[str, Any]
    distance: float
    score: float  # 1 - cosine distance


@dataclass(frozen=True)
class SearchResult:
    """`KnowledgeStore.search_snapshot`'s answer: the hits, and `moved`: the requested knowledge bases
    of the tenant that are, in the same snapshot as the search, under ANOTHER embedding model than
    the query's (id -> that model). A knowledge base moves when a reindex switches its model between
    the caller reading its model and the search: the caller re-embeds the query with the new model
    and searches it again."""

    hits: list[SearchHit]
    moved: dict[str, str]


_UNSET: Any = object()


# --------------------------------------------------------------------------------------- helpers


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def content_hash(content: str) -> str:
    """sha256 hex of the UTF-8 content (kb_documents.content_hash)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical_uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _require_uuid(value: Any, what: str) -> str:
    out = _canonical_uuid(value)
    if out is None:
        raise ValueError(f"{what} must be a UUID")
    return out


def _require_tenant(tenant_id: str) -> None:
    if not tenant_id:
        raise ValueError("tenant_id is required")


def _require_dimensions(dimensions: int) -> int:
    if dimensions not in HNSW_DIMENSIONS:
        raise ValueError(f"dimensions must be one of {', '.join(map(str, HNSW_DIMENSIONS))}")
    return int(dimensions)


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text form `[x,y,...]` of finite floats (cast in SQL as public.vector)."""
    parts = []
    for x in vector:
        f = float(x)
        if not math.isfinite(f):
            raise ValueError("embedding values must be finite")
        parts.append(repr(f))
    return "[" + ",".join(parts) + "]"


def _kb_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "tenant_id": row["tenant_id"],
        "name": row["name"],
        "description": row["description"],
        "embedding_model": row["embedding_model"],
        "dimensions": row["dimensions"],
        "chunking": row["chunking"],
        "active_generation": row["active_generation"],
        "last_generation": row["last_generation"],
        "pending_generation": row["pending_generation"],
        "pending_embedding_model": row["pending_embedding_model"],
        "pending_dimensions": row["pending_dimensions"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        **({"document_count": row["document_count"]} if "document_count" in row else {}),
        **({"chunk_count": row["chunk_count"]} if "chunk_count" in row else {}),
    }


_DOC_COLUMNS = (
    "id, tenant_id, kb_id, document_key, title, source_url, mime_type, metadata, content_hash,"
    " active_version, latest_version, status, error, chunk_count, last_run_id, created_at, updated_at"
)


def _doc_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "id": str(row["id"]),
        "tenant_id": row["tenant_id"],
        "kb_id": str(row["kb_id"]),
        "document_key": row["document_key"],
        "title": row["title"],
        "source_url": row["source_url"],
        "mime_type": row["mime_type"],
        "metadata": row["metadata"] or {},
        "content_hash": row["content_hash"],
        "active_version": row["active_version"],
        "latest_version": row["latest_version"],
        "status": row["status"],
        "error": row["error"],
        "chunk_count": row["chunk_count"],
        "last_run_id": row["last_run_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if "content" in row:
        out["content"] = row["content"]
    return out


def _filter_clauses(metadata_filter: Mapping[str, Any] | None) -> tuple[list[str], dict[str, Any]]:
    """`c.metadata @> ANY(...)` per key: a scalar is equality, a list is any of its values."""
    checked = validate_filter(dict(metadata_filter)) if metadata_filter else None
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for i, (key, value) in enumerate(sorted((checked or {}).items())):
        values = value if isinstance(value, tuple) else (value,)
        params[f"f{i}"] = [json.dumps({key: v}) for v in values]
        clauses.append(f"c.metadata @> ANY(CAST(:f{i} AS jsonb[]))")
    return clauses, params


def _scope_clause(document_id: str | None, doc_version: int | None, alias: str = "") -> tuple[str, dict[str, Any]]:
    if (document_id is None) != (doc_version is None):
        raise ValueError("document_id and doc_version go together")
    if document_id is None:
        return "", {}
    return f" AND {alias}document_id = :document_id AND {alias}doc_version = :doc_version", {
        "document_id": _require_uuid(document_id, "document_id"),
        "doc_version": int(doc_version),
    }


# ------------------------------------------------------------------------------------------ store


def _scrub_search_tester_runs(conn: Any, tenant_id: str, kb_id: str, *, document_id: str | None = None) -> list[str]:
    """Scrub the knowledge base's search-tester runs (P1.C4), in the caller's transaction: tool runs
    of `knowledge.search` with `runs.knowledge_base_id` = `kb_id`, and with `document_id` only those
    whose `tool_result` hits name that document. Like DELETE /conversations/{id} for a turn
    (eap_run_store.memory `_scrub_runs`): `input` and `output` NULL, every event of the run through
    scrub_event_data (no chunk text, query or result survives), every side_effects row of the run
    deleted. run_model_requests rows and run totals are untouched. Returns the run ids.

    Lock order: called after the knowledge base or document DELETE (its rows are locked); the run
    rows are then locked FOR UPDATE, then their events (`_scrub_runs`)."""
    from eap_run_store.memory import _scrub_runs  # eap_run_store.memory imports this module

    sql = (
        "SELECT r.id FROM runs r WHERE r.tenant_id = :t AND r.kind = 'tool' AND r.tool_id = :tool"
        " AND r.knowledge_base_id = :kb"
    )
    params: dict[str, Any] = {"t": tenant_id, "tool": KNOWLEDGE_SEARCH_TOOL_ID, "kb": str(kb_id)}
    if document_id is not None:
        sql += (
            " AND EXISTS (SELECT 1 FROM execution_events e WHERE e.tenant_id = r.tenant_id AND e.run_id = r.id"
            " AND e.type = 'tool_result'"
            " AND e.data -> 'content' -> 'hits' @> CAST(:hit AS jsonb))"
        )
        params["hit"] = json.dumps([{"document_id": str(document_id)}])
    run_ids = [str(r.id) for r in conn.execute(text(sql + " ORDER BY r.id FOR UPDATE OF r"), params)]
    if not run_ids:
        return []
    conn.execute(
        text("UPDATE runs SET input = NULL, output = NULL WHERE tenant_id = :t AND id = ANY(:ids)"),
        {"t": tenant_id, "ids": run_ids},
    )
    _scrub_runs(conn, tenant_id, run_ids, all_side_effects=True)
    return run_ids


class KnowledgeStore:
    """Knowledge bases, documents, chunks and retrieval. Every method is tenant-scoped in SQL."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    # ------------------------------------------------------------------ knowledge bases (RM.7)

    def create_knowledge_base(
        self,
        tenant_id: str,
        *,
        name: str,
        embedding_model: str,
        dimensions: int,
        description: str | None = None,
        chunking: ChunkingConfig | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a knowledge base (generation 1, status 'ready'). The caller has checked the
        embedding model against EmbeddingModelPolicy and takes `dimensions` from it.
        Raises KnowledgeBaseNameConflict for a name the tenant already uses."""
        _require_tenant(tenant_id)
        if not name or not name.strip():
            raise ValueError("name is required")
        parse_embedding_model_id(embedding_model)
        _require_dimensions(dimensions)
        config = chunking if isinstance(chunking, ChunkingConfig) else ChunkingConfig(**dict(chunking or {}))
        with self.Session() as session:
            try:
                row = session.execute(
                    text(
                        "INSERT INTO knowledge_bases (id, tenant_id, name, description, embedding_model, dimensions,"
                        " chunking) VALUES (:id, :t, :name, :description, :model, :dims, CAST(:chunking AS jsonb))"
                        " RETURNING *"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "t": tenant_id,
                        "name": name,
                        "description": description,
                        "model": embedding_model,
                        "dims": int(dimensions),
                        "chunking": json.dumps(config.model_dump()),
                    },
                ).mappings().one()
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if "uq_knowledge_bases_tenant_name" in str(exc.orig):
                    raise KnowledgeBaseNameConflict(tenant_id, name) from None
                raise
        return _kb_dict(row)

    _KB_WITH_COUNTS = (
        "SELECT kb.*, (SELECT count(*) FROM kb_documents d WHERE d.tenant_id = kb.tenant_id AND d.kb_id = kb.id)"
        " AS document_count, (SELECT coalesce(sum(d.chunk_count), 0) FROM kb_documents d"
        " WHERE d.tenant_id = kb.tenant_id AND d.kb_id = kb.id) AS chunk_count FROM knowledge_bases kb"
    )

    def get_knowledge_base(self, tenant_id: str, kb_id: str) -> dict[str, Any] | None:
        """The knowledge base with `document_count` and `chunk_count` (active chunks), or None
        (no such id in this tenant, or not a UUID)."""
        kid = _canonical_uuid(kb_id)
        if kid is None:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                text(self._KB_WITH_COUNTS + " WHERE kb.tenant_id = :t AND kb.id = :id"), {"t": tenant_id, "id": kid}
            ).mappings().first()
        return None if row is None else _kb_dict(row)

    def list_knowledge_bases(self, tenant_id: str) -> list[dict[str, Any]]:
        """The tenant's knowledge bases by name, with counts."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(self._KB_WITH_COUNTS + " WHERE kb.tenant_id = :t ORDER BY kb.name, kb.id"), {"t": tenant_id}
            ).mappings().all()
        return [_kb_dict(r) for r in rows]

    def get_knowledge_bases(self, tenant_id: str, kb_ids: Iterable[str]) -> list[dict[str, Any]]:
        """The existing knowledge bases among `kb_ids` in this tenant (others, and non-UUIDs, are
        left out), in the order given. For publish validation and the retrieve tool's description."""
        ids = [i for i in (_canonical_uuid(k) for k in kb_ids) if i is not None]
        if not ids:
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM knowledge_bases WHERE tenant_id = :t AND id = ANY(CAST(:ids AS uuid[]))"),
                {"t": tenant_id, "ids": ids},
            ).mappings().all()
        by_id = {str(r["id"]): _kb_dict(r) for r in rows}
        return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]

    def existing_knowledge_base_ids(self, tenant_id: str, kb_ids: Iterable[str]) -> set[str]:
        """The ids among `kb_ids` that exist in this tenant (canonical lowercase UUID strings)."""
        return {kb["id"] for kb in self.get_knowledge_bases(tenant_id, kb_ids)}

    def update_knowledge_base(
        self, tenant_id: str, kb_id: str, *, name: str | None = None, description: Any = _UNSET
    ) -> dict[str, Any]:
        """Rename and/or set the description (None clears it). The embedding model changes only
        through a reindex. Raises KnowledgeBaseNotFound, KnowledgeBaseNameConflict."""
        kid = _canonical_uuid(kb_id)
        if kid is None:
            raise KnowledgeBaseNotFound(str(kb_id))
        sets, params = ["updated_at = now()"], {"t": tenant_id, "id": kid}
        if name is not None:
            if not name.strip():
                raise ValueError("name is required")
            sets.append("name = :name")
            params["name"] = name
        if description is not _UNSET:
            sets.append("description = :description")
            params["description"] = description
        with self.Session() as session:
            try:
                row = session.execute(
                    text(f"UPDATE knowledge_bases SET {', '.join(sets)} WHERE tenant_id = :t AND id = :id RETURNING *"),
                    params,
                ).mappings().first()
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if "uq_knowledge_bases_tenant_name" in str(exc.orig):
                    raise KnowledgeBaseNameConflict(tenant_id, name or "") from None
                raise
        if row is None:
            raise KnowledgeBaseNotFound(kid)
        return _kb_dict(row)

    def delete_knowledge_base(self, tenant_id: str, kb_id: str) -> bool:
        """Delete the knowledge base; its documents and chunks cascade in the same statement.
        Runs stay (audit and cost). False when it does not exist in this tenant (idempotent).
        The agent-reference 409 is the API's check (agents live in its metadata).

        In the same transaction (P1.C4), every search-tester run of the knowledge base (`kind`
        'tool', `tool_id` 'knowledge.search', `runs.knowledge_base_id`) is scrubbed like a deleted
        conversation's turn: `input` and `output` NULL, every event's data through
        scrub_event_data, its side_effects rows deleted (_scrub_search_tester_runs). Usage rows
        and run totals stay."""
        kid = _canonical_uuid(kb_id)
        if kid is None:
            return False
        with self.engine.begin() as conn:
            deleted = (
                conn.execute(
                    text("DELETE FROM knowledge_bases WHERE tenant_id = :t AND id = :id"), {"t": tenant_id, "id": kid}
                ).rowcount
                > 0
            )
            if deleted:
                _scrub_search_tester_runs(conn, tenant_id, kid)
            return deleted

    # ------------------------------------------------------------------ documents (RM.7)

    def put_document(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        document_key: str,
        title: str,
        content: str,
        mime_type: str,
        source_url: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        content_sha256: str | None = None,
    ) -> PutDocumentResult:
        """Upload a document. Unchanged (same content hash, metadata, title, source URL and MIME
        type) is a no-op with `unchanged=True`. Otherwise the content is stored, `latest_version`
        increases (1 for a new key), status becomes 'pending' and the error clears: start an ingest
        run of `document["latest_version"]`. Raises KnowledgeBaseNotFound, and
        KnowledgeBaseLimitExceeded at the 2,000 document cap.

        `run_id` (the API generates it before the write): recorded as `last_run_id` in the same
        transaction as a changing upload, so a concurrent unchanged upload always finds the run that
        owns the new version. Not written when the upload is unchanged.

        `content_sha256` (RAG P1): the `content_hash` to store instead of `content_hash(content)`. A
        binary document (a PDF) is stored as base64 text, and its hash is the sha256 of the decoded
        bytes (lower-case hex), so the same file always hashes the same."""
        _require_tenant(tenant_id)
        kid = _canonical_uuid(kb_id)
        if kid is None:
            raise KnowledgeBaseNotFound(str(kb_id))
        if not document_key:
            raise ValueError("document_key is required")
        meta = validate_metadata(dict(metadata) if metadata is not None else None)
        if content_sha256 is not None and not _SHA256_HEX.fullmatch(content_sha256):
            raise ValueError("content_sha256 must be 64 lower-case hex characters")
        digest = content_sha256 if content_sha256 is not None else content_hash(content)
        select_doc = text(
            f"SELECT {_DOC_COLUMNS} FROM kb_documents"
            " WHERE tenant_id = :t AND kb_id = :kb AND document_key = :key FOR NO KEY UPDATE"
        )
        doc_params = {"t": tenant_id, "kb": kid, "key": document_key}
        with self.Session() as session:
            # Lock order (module docstring): a re-upload locks only its document; a new key locks
            # the knowledge base FOR UPDATE (the document cap) and then looks again.
            existing = session.execute(select_doc, doc_params).mappings().first()
            if existing is None:
                kb = session.execute(
                    text("SELECT id FROM knowledge_bases WHERE tenant_id = :t AND id = :id FOR UPDATE"),
                    {"t": tenant_id, "id": kid},
                ).first()
                if kb is None:
                    session.rollback()
                    raise KnowledgeBaseNotFound(kid)
                existing = session.execute(select_doc, doc_params).mappings().first()
            values = {
                "t": tenant_id,
                "kb": kid,
                "key": document_key,
                "title": title,
                "content": content,
                "hash": digest,
                "mime": mime_type,
                "url": source_url,
                "meta": json.dumps(meta),
                "run": None if run_id is None else str(run_id),
            }
            if existing is not None:
                if (
                    existing["content_hash"] == digest
                    and (existing["metadata"] or {}) == meta
                    and existing["title"] == title
                    and existing["source_url"] == source_url
                    and existing["mime_type"] == mime_type
                ):
                    session.commit()
                    return PutDocumentResult(_doc_dict(existing), unchanged=True, created=False)
                row = session.execute(
                    text(
                        "UPDATE kb_documents SET title = :title, content = :content, content_hash = :hash,"
                        " mime_type = :mime, source_url = :url, metadata = CAST(:meta AS jsonb),"
                        " latest_version = latest_version + 1, status = 'pending', error = NULL, updated_at = now(),"
                        " last_run_id = coalesce(CAST(:run AS varchar), last_run_id)"
                        f" WHERE tenant_id = :t AND id = :id RETURNING {_DOC_COLUMNS}"
                    ),
                    {**values, "id": existing["id"]},
                ).mappings().one()
                created = False
            else:
                count = session.execute(
                    text("SELECT count(*) FROM kb_documents WHERE tenant_id = :t AND kb_id = :kb"),
                    {"t": tenant_id, "kb": kid},
                ).scalar_one()
                if count >= MAX_DOCUMENTS_PER_KNOWLEDGE_BASE:
                    session.rollback()
                    raise KnowledgeBaseLimitExceeded(kid, "documents", MAX_DOCUMENTS_PER_KNOWLEDGE_BASE)
                row = session.execute(
                    text(
                        "INSERT INTO kb_documents (id, tenant_id, kb_id, document_key, title, source_url, mime_type,"
                        " metadata, content, content_hash, last_run_id) VALUES (:id, :t, :kb, :key, :title, :url, :mime,"
                        f" CAST(:meta AS jsonb), :content, :hash, CAST(:run AS varchar)) RETURNING {_DOC_COLUMNS}"
                    ),
                    {**values, "id": str(uuid.uuid4())},
                ).mappings().one()
                created = True
            session.commit()
        return PutDocumentResult(_doc_dict(row), unchanged=False, created=created)

    def get_document(
        self, tenant_id: str, kb_id: str, document_key: str, *, include_content: bool = False
    ) -> dict[str, Any] | None:
        kid = _canonical_uuid(kb_id)
        if kid is None:
            return None
        cols = _DOC_COLUMNS + (", content" if include_content else "")
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {cols} FROM kb_documents WHERE tenant_id = :t AND kb_id = :kb AND document_key = :key"),
                {"t": tenant_id, "kb": kid, "key": document_key},
            ).mappings().first()
        return None if row is None else _doc_dict(row)

    def get_document_by_id(
        self, tenant_id: str, document_id: str, *, include_content: bool = False
    ) -> dict[str, Any] | None:
        did = _canonical_uuid(document_id)
        if did is None:
            return None
        cols = _DOC_COLUMNS + (", content" if include_content else "")
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {cols} FROM kb_documents WHERE tenant_id = :t AND id = :id"), {"t": tenant_id, "id": did}
            ).mappings().first()
        return None if row is None else _doc_dict(row)

    def list_documents(
        self, tenant_id: str, kb_id: str, *, limit: int | None = None, after: str | None = None
    ) -> list[dict[str, Any]]:
        """The knowledge base's documents (without content), by document_key.

        Keyset pagination: with `after`, only documents whose key sorts strictly after it; with
        `limit`, at most that many. Served by uq_kb_documents_tenant_kb_key; the ORDER BY and the
        `>` use the column's (deterministic) collation, so they agree and no key is skipped or
        repeated between pages. Without both, every document (as before)."""
        kid = _canonical_uuid(kb_id)
        if kid is None:
            return []
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        params: dict[str, Any] = {"t": tenant_id, "kb": kid}
        sql = f"SELECT {_DOC_COLUMNS} FROM kb_documents WHERE tenant_id = :t AND kb_id = :kb"
        if after is not None:
            sql += " AND document_key > :after"
            params["after"] = after
        sql += " ORDER BY document_key"
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = int(limit)
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [_doc_dict(r) for r in rows]

    def write_search_result(
        self,
        tenant_id: str,
        kb_id: str,
        document_ids: Iterable[str],
        write: Callable[[Any, frozenset[str] | None], Any],
    ) -> Any:
        """One write of a `knowledge.search` tester run's result (its `tool_result` event, its `end`
        event, or its finish), serialised with the knowledge base and document deletes (P1.C4): in
        ONE transaction the knowledge base row and the rows of `document_ids` (the hits' documents)
        are locked FOR SHARE, then `write(session, live)` runs with `live` = the ids among
        `document_ids` that still exist, or None when the knowledge base is gone, and the
        transaction commits. A delete (FOR UPDATE / DELETE of those rows) therefore runs either
        before this write, which then sees the rows gone and leaves their hits out, or after it, and
        then scrubs what it wrote (_scrub_search_tester_runs). `write` uses append_event_in_session
        and set_status_in_session with the session it is given."""
        _require_tenant(tenant_id)
        kid = _canonical_uuid(kb_id)
        ids = sorted({d for d in (_canonical_uuid(str(i)) for i in document_ids) if d is not None})
        with self.Session() as session:
            try:
                live: frozenset[str] | None = None
                kb = None
                if kid is not None:
                    kb = session.execute(
                        text("SELECT id FROM knowledge_bases WHERE tenant_id = :t AND id = :id FOR SHARE"),
                        {"t": tenant_id, "id": kid},
                    ).first()
                if kb is not None:
                    live = frozenset()
                    if ids:
                        rows = session.execute(
                            text(
                                "SELECT id FROM kb_documents WHERE tenant_id = :t AND kb_id = :kb"
                                " AND id = ANY(CAST(:ids AS uuid[])) ORDER BY id FOR SHARE"
                            ),
                            {"t": tenant_id, "kb": kid, "ids": ids},
                        ).all()
                        live = frozenset(str(r.id) for r in rows)
                result = write(session, live)
            except Exception:
                session.rollback()
                raise
            session.commit()
            return result

    def delete_document(self, tenant_id: str, kb_id: str, document_key: str) -> bool:
        """Delete the document in one statement; its chunks cascade. An in-flight ingest then finds
        it missing (DocumentNotFound, FinalizeResult 'missing'). False when absent (idempotent).

        In the same transaction (P1.C4), the knowledge base's search-tester runs whose
        `tool_result` hits name the document's id are scrubbed (_scrub_search_tester_runs)."""
        kid = _canonical_uuid(kb_id)
        if kid is None:
            return False
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    "DELETE FROM kb_documents WHERE tenant_id = :t AND kb_id = :kb AND document_key = :key"
                    " RETURNING id"
                ),
                {"t": tenant_id, "kb": kid, "key": document_key},
            ).first()
            if row is None:
                return False
            _scrub_search_tester_runs(conn, tenant_id, kid, document_id=str(row.id))
            return True

    def request_reingest(
        self,
        tenant_id: str,
        kb_id: str,
        document_key: str,
        *,
        run_id: str | None = None,
        expected_last_run_id: Any = _UNSET,
        doc_version: int | None = None,
    ) -> dict[str, Any]:
        """Retry: status 'pending' with the error cleared, same `latest_version` (cached embeddings
        are reused). Start an ingest run of `latest_version`. Raises DocumentNotFound.

        `run_id` is recorded as `last_run_id` in the same statement. With `expected_last_run_id`
        (None included) and/or `doc_version`, the update applies only while the document still has
        that `last_run_id` / `latest_version`, so of two concurrent reingests one wins; the loser
        gets IngestRunConflict and must not start a run."""
        kid = _canonical_uuid(kb_id)
        conditions, params = "", {"t": tenant_id, "kb": kid, "key": document_key, "run": run_id and str(run_id)}
        if expected_last_run_id is not _UNSET:
            conditions += " AND last_run_id IS NOT DISTINCT FROM CAST(:prev AS varchar)"
            params["prev"] = None if expected_last_run_id is None else str(expected_last_run_id)
        if doc_version is not None:
            conditions += " AND latest_version = :v"
            params["v"] = int(doc_version)
        with self.engine.begin() as conn:
            row = None
            if kid is not None:
                row = conn.execute(
                    text(
                        "UPDATE kb_documents SET status = 'pending', error = NULL, updated_at = now(),"
                        " last_run_id = coalesce(CAST(:run AS varchar), last_run_id)"
                        f" WHERE tenant_id = :t AND kb_id = :kb AND document_key = :key{conditions}"
                        f" RETURNING {_DOC_COLUMNS}"
                    ),
                    params,
                ).mappings().first()
                if row is None and conditions:
                    exists = conn.execute(
                        text("SELECT 1 FROM kb_documents WHERE tenant_id = :t AND kb_id = :kb AND document_key = :key"),
                        params,
                    ).first()
                    if exists is not None:
                        raise IngestRunConflict(document_key)
        if row is None:
            raise DocumentNotFound(document_key)
        return _doc_dict(row)

    def attach_ingest_run(
        self,
        tenant_id: str,
        document_id: str,
        run_id: str,
        *,
        doc_version: int | None = None,
        expected_last_run_id: Any = _UNSET,
        expected_updated_at: datetime | None = None,
    ) -> bool:
        """Record the ingest run started for the document (`last_run_id`). With `doc_version` it
        applies only while that is the latest version; with `expected_last_run_id` (None included)
        only while `last_run_id` is still that value; with `expected_updated_at` only while
        `updated_at` is still the value the caller read (any write since, a lease renewal included,
        makes it lose). Compare-and-set: of concurrent callers one wins. False when it did not
        apply: the caller must not start the run."""
        did = _canonical_uuid(document_id)
        if did is None:
            return False
        conditions, params = "", {"t": tenant_id, "id": did, "run": str(run_id)}
        if doc_version is not None:
            conditions += " AND latest_version = :v"
            params["v"] = int(doc_version)
        if expected_last_run_id is not _UNSET:
            conditions += " AND last_run_id IS NOT DISTINCT FROM CAST(:prev AS varchar)"
            params["prev"] = None if expected_last_run_id is None else str(expected_last_run_id)
        if expected_updated_at is not None:
            conditions += " AND updated_at = :seen"
            params["seen"] = expected_updated_at
        with self.engine.begin() as conn:
            return (
                conn.execute(
                    text(
                        "UPDATE kb_documents SET last_run_id = :run, updated_at = now()"
                        f" WHERE tenant_id = :t AND id = :id{conditions}"
                    ),
                    params,
                ).rowcount
                > 0
            )

    # ------------------------------------------------------------ re-extraction (RAG P1.A4)

    def new_document_version(
        self,
        tenant_id: str,
        kb_id: str,
        document_key: str,
        *,
        run_id: str,
        expected_latest_version: int | None = None,
    ) -> dict[str, Any]:
        """A new version of the document with the SAME content (re-extraction): `latest_version`
        + 1, status 'pending', error cleared and `last_run_id` = `run_id`, in one statement.
        Content, hash, title, metadata, source URL and MIME type are untouched; start an ingest run
        of the returned `latest_version`. Raises DocumentNotFound.

        Compare-and-set, so of concurrent callers only the winner bumps: with
        `expected_latest_version` it applies only while that is still the latest version, and it
        never applies when `last_run_id` is already `run_id` (that run id has claimed a version:
        a retry or a concurrent duplicate of the same request must not bump again). Otherwise it
        raises IngestRunConflict and writes nothing; the caller re-reads the document (its
        `last_run_id` equal to `run_id` means "replay that run")."""
        kid = _canonical_uuid(kb_id)
        params: dict[str, Any] = {"t": tenant_id, "kb": kid, "key": document_key, "run": str(run_id)}
        condition = " AND last_run_id IS DISTINCT FROM CAST(:run AS varchar)"
        if expected_latest_version is not None:
            condition += " AND latest_version = :v"
            params["v"] = int(expected_latest_version)
        with self.engine.begin() as conn:
            row = None
            if kid is not None:
                row = conn.execute(
                    text(
                        "UPDATE kb_documents SET latest_version = latest_version + 1, status = 'pending',"
                        " error = NULL, updated_at = now(), last_run_id = :run"
                        f" WHERE tenant_id = :t AND kb_id = :kb AND document_key = :key{condition}"
                        f" RETURNING {_DOC_COLUMNS}"
                    ),
                    params,
                ).mappings().first()
                if row is None:
                    exists = conn.execute(
                        text("SELECT 1 FROM kb_documents WHERE tenant_id = :t AND kb_id = :kb AND document_key = :key"),
                        params,
                    ).first()
                    if exists is not None:
                        raise IngestRunConflict(document_key)
        if row is None:
            raise DocumentNotFound(document_key)
        return _doc_dict(row)

    def start_reextract(
        self,
        tenant_id: str,
        kb_id: str,
        *,
        document_ids: Sequence[str] | None,
        chunker_version: int | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        """Bulk re-extraction, in ONE transaction: lock the knowledge base `FOR UPDATE` (the lock
        order: knowledge base, then documents), optionally move it to `chunker_version` (only the
        `chunking.chunker_version` key changes; the result must be a valid ChunkingConfig), then
        give each document a new version with the same content (as `new_document_version`) and a
        fresh `last_run_id` (a random UUID: the id of the ingest run the caller creates next).

        `document_ids` None means every document of the knowledge base (a new key cannot be added
        meanwhile: its upload locks the knowledge base too); ids that are gone are skipped.

        With `chunker_version`, the bump is part of the switch: when the knowledge base is already
        on that version once the lock is held (a concurrent duplicate request switched it first),
        nothing is written and no document is returned, so duplicates make one version each.

        Returns (the knowledge base, the bumped documents by key, whether the chunker version
        changed). Raises KnowledgeBaseNotFound."""
        _require_tenant(tenant_id)
        kid = _canonical_uuid(kb_id)
        if kid is None:
            raise KnowledgeBaseNotFound(str(kb_id))
        ids: list[str] | None = None
        if document_ids is not None:
            ids = [d for d in (_canonical_uuid(x) for x in document_ids) if d is not None]
        with self.engine.begin() as conn:
            kb = conn.execute(
                text("SELECT * FROM knowledge_bases WHERE tenant_id = :t AND id = :id FOR UPDATE"),
                {"t": tenant_id, "id": kid},
            ).mappings().first()
            if kb is None:
                raise KnowledgeBaseNotFound(kid)
            changed = False
            if chunker_version is not None and (kb["chunking"] or {}).get("chunker_version") != int(chunker_version):
                ChunkingConfig(**{**dict(kb["chunking"] or {}), "chunker_version": int(chunker_version)})
                kb = conn.execute(
                    text(
                        "UPDATE knowledge_bases SET chunking = jsonb_set(chunking, '{chunker_version}', to_jsonb(:v)),"
                        " updated_at = now() WHERE tenant_id = :t AND id = :id RETURNING *"
                    ),
                    {"t": tenant_id, "id": kid, "v": int(chunker_version)},
                ).mappings().one()
                changed = True
            rows: list[Any] = []
            if chunker_version is not None and not changed:
                pass  # the switch already happened: its own request bumped the documents
            elif ids is None or ids:
                scope = "" if ids is None else " AND id = ANY(CAST(:ids AS uuid[]))"
                rows = conn.execute(
                    text(
                        "UPDATE kb_documents SET latest_version = latest_version + 1, status = 'pending',"
                        " error = NULL, updated_at = now(), last_run_id = CAST(gen_random_uuid() AS varchar)"
                        f" WHERE tenant_id = :t AND kb_id = :kb{scope} RETURNING {_DOC_COLUMNS}"
                    ),
                    {"t": tenant_id, "kb": kid, **({} if ids is None else {"ids": ids})},
                ).mappings().all()
        docs = sorted((_doc_dict(r) for r in rows), key=lambda d: d["document_key"])
        return _kb_dict(kb), docs, changed

    def outdated_extraction_documents(
        self,
        tenant_id: str,
        kb_id: str,
        current: Mapping[str, tuple[str | None, int]],
        *,
        orphan_after_seconds: float = 60.0,
    ) -> list[dict[str, Any]]:
        """The documents (without content, by key) whose extraction is outdated.

        `current` maps a base MIME type (lower-case, no parameters; aliases listed separately) to
        the worker extractor that handles it in this knowledge base and that extractor's current
        version (`eap_knowledge.EXTRACTOR_VERSIONS`); the caller builds it from the format
        registry, since this store imports no eap_knowledge. A document of one of those types is
        outdated when its active version has no chunk, at the active generation, whose
        `metadata.eap` records that extractor at that version or later: an older extractor version,
        a different extractor (the knowledge base's Markdown flavour changed), the API path (no
        record), or no active version at all (never ingested, or every ingest failed).

        An extractor of None means the API extracts that type in this knowledge base (its version
        is ignored): such a document is outdated when its active chunks DO carry a worker
        extractor record (`metadata.eap.extractor`), e.g. Markdown extracted as Doxygen before the
        knowledge base's flavour went back to plain. One with no active version is not selected
        (an upload or reingest starts its run; there is no stale extraction to replace).

        A document whose latest version is in flight is not selected: status `pending` or
        `ingesting` with that version not active (never ingested, or a newer version over the
        active one) AND a live run. That covers a re-extract started by an earlier call (so a double
        click or a CLI retry starts nothing instead of superseding those runs) and a first ingest
        still queued, e.g. behind a bulk import (it extracts with the current extractor anyway;
        browser check finding A). Once its run ends the document is judged on its active chunks
        again; a never-ingested document whose every ingest failed is selectable.

        The run is its `last_run_id` row. It is live while it is not terminal (`queued`, however
        long it has waited: a long queue is not a lost run, and DBOS still holds it). It is dead,
        and the pending document selectable (so bulk re-extract can revive it), when the row is
        terminal (an enqueue that failed: `failed` / `enqueue_failed`; or a run that ended without
        moving the document), or when there is no row and the document has been pending longer than
        `orphan_after_seconds` (the API's ORPHAN_AFTER_SECONDS: its request died between the version
        write and the run insert). A younger document with no row is a request still in progress."""
        kid = _canonical_uuid(kb_id)
        if kid is None or not current:
            return []
        mimes = sorted(current)
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {', '.join('d.' + c.strip() for c in _DOC_COLUMNS.split(','))}"
                    " FROM kb_documents d"
                    " JOIN knowledge_bases kb ON kb.tenant_id = d.tenant_id AND kb.id = d.kb_id"
                    " JOIN unnest(CAST(:mimes AS text[]), CAST(:exts AS text[]), CAST(:vers AS int[]))"
                    "   AS cur(mime, extractor, version)"
                    "   ON cur.mime = lower(btrim(split_part(d.mime_type, ';', 1)))"
                    " WHERE d.tenant_id = :t AND d.kb_id = :kb"
                    " AND NOT (d.status IN ('pending', 'ingesting')"
                    "   AND (d.active_version IS NULL OR d.latest_version > d.active_version)"
                    "   AND (EXISTS (SELECT 1 FROM runs r WHERE r.tenant_id = d.tenant_id AND r.id = d.last_run_id"
                    "       AND r.status NOT IN ('completed', 'failed', 'needs_attention'))"
                    "     OR (NOT EXISTS (SELECT 1 FROM runs r WHERE r.tenant_id = d.tenant_id AND r.id = d.last_run_id)"
                    "       AND d.updated_at > now() - make_interval(secs => :orphan))))"
                    " AND CASE WHEN cur.extractor IS NULL THEN EXISTS (SELECT 1 FROM kb_chunks c"
                    "   WHERE c.tenant_id = d.tenant_id AND c.document_id = d.id"
                    "   AND c.doc_version = d.active_version AND c.generation = kb.active_generation"
                    "   AND c.metadata -> 'eap' ->> 'extractor' IS NOT NULL)"
                    " ELSE NOT EXISTS (SELECT 1 FROM kb_chunks c"
                    "   WHERE c.tenant_id = d.tenant_id AND c.document_id = d.id"
                    "   AND c.doc_version = d.active_version AND c.generation = kb.active_generation"
                    "   AND c.metadata -> 'eap' ->> 'extractor' = cur.extractor"
                    "   AND CASE WHEN jsonb_typeof(c.metadata -> 'eap' -> 'extractor_version') = 'number'"
                    "     THEN CAST(c.metadata -> 'eap' ->> 'extractor_version' AS numeric) >= cur.version"
                    "     ELSE false END) END"
                    " ORDER BY d.document_key"
                ),
                {
                    "t": tenant_id,
                    "kb": kid,
                    "mimes": mimes,
                    "exts": [current[m][0] for m in mimes],
                    "vers": [int(current[m][1]) for m in mimes],
                    "orphan": float(orphan_after_seconds),
                },
            ).mappings().all()
        return [_doc_dict(r) for r in rows]

    def latest_extraction(self, tenant_id: str, kb_id: str, document_id: str) -> dict[str, Any] | None:
        """The extraction of the document's ACTIVE version (what retrieval serves): the newest
        `extracted` event (RAG P1, written by eap.kb_extract_chunk, key `extract:{doc_version}`)
        among its kb_ingest runs of that version, as `{**data, "doc_version", "run_id"}`. A newer
        version that is pending or failed does not replace it. None when there is no active
        version, or no worker extraction of it (an API-extracted document). Counts only: the event
        never holds document text."""
        kid, did = _canonical_uuid(kb_id), _canonical_uuid(document_id)
        if kid is None or did is None:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT e.run_id, e.event_key, e.data FROM kb_documents d"
                    " JOIN runs r ON r.tenant_id = d.tenant_id AND r.knowledge_base_id = d.kb_id"
                    " JOIN execution_events e ON e.tenant_id = r.tenant_id AND e.run_id = r.id"
                    " WHERE d.tenant_id = :t AND d.kb_id = :kb AND d.id = CAST(:doc_id AS uuid) AND d.active_version IS NOT NULL"
                    " AND r.kind = 'kb_ingest' AND r.input LIKE :like"
                    " AND CAST(r.input AS jsonb) ->> 'document_id' = :doc"
                    " AND e.type = 'extracted' AND e.event_key = 'extract:' || d.active_version"
                    " ORDER BY e.ts DESC, e.seq DESC LIMIT 1"
                ),
                {"t": tenant_id, "kb": kid, "doc": did, "doc_id": did, "like": f'%"{did}"%'},
            ).mappings().first()
        if row is None or not isinstance(row["data"], dict):
            return None
        _, _, version = str(row["event_key"]).partition(":")
        return {
            **row["data"],
            "doc_version": int(version) if version.isdigit() else None,
            "run_id": str(row["run_id"]),
        }

    # ------------------------------------------------------------------ ingestion (RM.10)

    def get_ingest_source(self, tenant_id: str, document_id: str, doc_version: int) -> dict[str, Any] | None:
        """What `eap.start_ingest` needs: `{"document": {... with content}, "knowledge_base": {...},
        "generation": <active generation>, "embedding_model", "dimensions", "chunking",
        "already_active": doc_version is the active version, "superseded": a newer version is
        uploaded}`. None when the document is gone (end the run cancelled)."""
        doc = self.get_document_by_id(tenant_id, document_id, include_content=True)
        if doc is None:
            return None
        kb = self.get_knowledge_base(tenant_id, doc["kb_id"])
        if kb is None:
            return None
        return {
            "document": doc,
            "knowledge_base": kb,
            "generation": kb["active_generation"],
            "embedding_model": kb["embedding_model"],
            "dimensions": kb["dimensions"],
            "chunking": kb["chunking"],
            "already_active": doc["active_version"] == doc_version,
            "superseded": doc["latest_version"] > doc_version,
        }

    def mark_document_ingesting(self, tenant_id: str, document_id: str, doc_version: int, run_id: str) -> bool:
        """Status 'ingesting' and `last_run_id`, only while `doc_version` is the latest and not yet
        active. False otherwise (a newer upload, already active, or deleted)."""
        did = _require_uuid(document_id, "document_id")
        with self.engine.begin() as conn:
            return (
                conn.execute(
                    text(
                        "UPDATE kb_documents SET status = 'ingesting', last_run_id = :run, error = NULL,"
                        " updated_at = now() WHERE tenant_id = :t AND id = :id AND latest_version = :v"
                        " AND active_version IS DISTINCT FROM :v"
                    ),
                    {"t": tenant_id, "id": did, "v": int(doc_version), "run": str(run_id)},
                ).rowcount
                > 0
            )

    def mark_document_failed(self, tenant_id: str, document_id: str, doc_version: int, error: str) -> bool:
        """Status 'failed' with `error` (e.g. "embedding_failed: 412 of 980 sections embedded"), only
        while `doc_version` is the latest. The active version stays searchable."""
        did = _require_uuid(document_id, "document_id")
        with self.engine.begin() as conn:
            return (
                conn.execute(
                    text(
                        "UPDATE kb_documents SET status = 'failed', error = :error, updated_at = now()"
                        " WHERE tenant_id = :t AND id = :id AND latest_version = :v"
                    ),
                    {"t": tenant_id, "id": did, "v": int(doc_version), "error": error},
                ).rowcount
                > 0
            )

    def _writable_generation(
        self,
        session,
        tenant_id: str,
        kb_id: str,
        generation: int,
        lock: str = "",
        embedding_model: str | None = None,
    ) -> tuple[str, int]:
        """(embedding model, dimensions) of the active or pending `generation`; with
        `embedding_model`, raises GenerationModelMismatch when the generation has another model."""
        kb = session.execute(
            text(
                "SELECT embedding_model, dimensions, active_generation, pending_generation, pending_embedding_model,"
                f" pending_dimensions FROM knowledge_bases WHERE tenant_id = :t AND id = :id {lock}"
            ),
            {"t": tenant_id, "id": kb_id},
        ).mappings().first()
        if kb is None:
            raise KnowledgeBaseNotFound(kb_id)
        if generation == kb["active_generation"]:
            model, dims = kb["embedding_model"], kb["dimensions"]
        elif generation == kb["pending_generation"]:
            model, dims = kb["pending_embedding_model"], kb["pending_dimensions"]
        else:
            raise GenerationNotWritable(kb_id, generation)
        if embedding_model is not None and embedding_model != model:
            raise GenerationModelMismatch(kb_id, generation, model, embedding_model)
        return model, dims

    @staticmethod
    def _document_kb(session, tenant_id: str, document_id: str) -> str:
        """The document's knowledge base id, read without a lock (the first step of the lock order)."""
        kb_id = session.execute(
            text("SELECT kb_id FROM kb_documents WHERE tenant_id = :t AND id = :id"),
            {"t": tenant_id, "id": document_id},
        ).scalar()
        if kb_id is None:
            raise DocumentNotFound(document_id)
        return str(kb_id)

    def upsert_chunks(
        self,
        tenant_id: str,
        document_id: str,
        doc_version: int,
        generation: int,
        chunks: Sequence[ChunkLike],
        *,
        metadata: Mapping[str, Any],
        extract_metadata: Sequence[Mapping[str, Any] | None] | None = None,
    ) -> UpsertResult:
        """Insert the version's chunk rows without embeddings, idempotent by `(document_id,
        doc_version, generation, ordinal)` (ON CONFLICT DO NOTHING: a re-executed step inserts 0).
        `metadata` is the document metadata of the version being ingested (the `get_ingest_source`
        snapshot), copied onto every chunk for the retrieval filter.
        `extract_metadata` (RAG P1, eap.kb_extract_chunk): one platform extraction record per chunk
        (`eap_schemas.knowledge.ChunkExtractMetadata` as JSON, or None), stored under the reserved
        key `eap` AFTER the user metadata is validated (validate_metadata rejects that key). In the
        same transaction, before the insert, the version's rows in this generation recorded with
        another extractor, an OLDER extractor version, or no `eap` record are deleted, so a retry
        after an extractor upgrade (or a Markdown flavour switch) never leaves rows mixed. Rows of
        the same extractor with a NEWER version are never deleted: during a rolling deploy an old
        worker's insert then finds them and inserts nothing (ON CONFLICT DO NOTHING).
        Without `extract_metadata` (eap.kb_chunk), the version's rows in this generation that DO
        carry an `eap` record (a worker extraction, e.g. Doxygen Markdown before its knowledge base
        went back to plain) are deleted first, for the same reason.
        Raises DocumentNotFound (deleted), KnowledgeBaseNotFound, GenerationNotWritable, and
        KnowledgeBaseLimitExceeded at the 200,000 chunk cap of the generation. The cap is soft:
        it is checked under a shared lock, so concurrent ingests of one knowledge base can pass it
        together by at most their own chunk counts."""
        _require_tenant(tenant_id)
        did = _require_uuid(document_id, "document_id")
        meta = validate_metadata(dict(metadata))
        if extract_metadata is not None and len(extract_metadata) != len(chunks):
            raise ValueError("extract_metadata must have one entry per chunk")
        with self.Session() as session:
            try:
                # Lock order: the knowledge base FOR SHARE, then the document, then re-check.
                kid = self._document_kb(session, tenant_id, did)
                self._writable_generation(session, tenant_id, kid, int(generation), "FOR SHARE")
                doc = session.execute(
                    text("SELECT kb_id FROM kb_documents WHERE tenant_id = :t AND id = :id FOR SHARE"),
                    {"t": tenant_id, "id": did},
                ).first()
                if doc is None or str(doc.kb_id) != kid:
                    raise DocumentNotFound(did)
            except Exception:
                session.rollback()
                raise
            slot = {"t": tenant_id, "id": did, "v": int(doc_version), "g": int(generation)}
            slot_sql = (
                "DELETE FROM kb_chunks WHERE tenant_id = :t AND document_id = :id AND doc_version = :v"
                " AND generation = :g"
            )
            stamps = {(str(r["extractor"]), int(r["extractor_version"])) for r in extract_metadata or () if r}
            if len(stamps) == 1:
                ((extractor, version),) = stamps
                session.execute(
                    text(
                        slot_sql
                        + " AND (metadata -> 'eap' ->> 'extractor' IS DISTINCT FROM :ex"
                        " OR jsonb_typeof(metadata -> 'eap' -> 'extractor_version') IS DISTINCT FROM 'number'"
                        " OR (metadata -> 'eap' ->> 'extractor_version')::numeric < :ver)"
                    ),
                    {**slot, "ex": extractor, "ver": version},
                )
            elif extract_metadata is None:
                session.execute(text(slot_sql + " AND metadata ? 'eap'"), slot)
            others = session.execute(
                text(
                    "SELECT count(*) FROM kb_chunks WHERE tenant_id = :t AND kb_id = :kb AND generation = :g"
                    " AND document_id <> :id"
                ),
                {"t": tenant_id, "kb": kid, "g": int(generation), "id": did},
            ).scalar_one()
            if others + len(chunks) > MAX_CHUNKS_PER_KNOWLEDGE_BASE:
                session.rollback()
                raise KnowledgeBaseLimitExceeded(kid, "chunks", MAX_CHUNKS_PER_KNOWLEDGE_BASE)
            rows = [
                {
                    "tenant_id": tenant_id,
                    "kb_id": kid,
                    "document_id": did,
                    "doc_version": int(doc_version),
                    "generation": int(generation),
                    "ordinal": int(c.ordinal),
                    "heading_path": list(c.heading_path),
                    "title": c.title,
                    "anchor_url": c.anchor,
                    "content": c.text,
                    "embed_text": c.embed_text,
                    "embed_text_hash": c.embed_text_hash,
                    "approx_tokens": int(c.approx_tokens),
                    "overlap": int(getattr(c, "overlap", 0) or 0),
                    "metadata": meta,
                }
                for c in chunks
            ]
            if extract_metadata is not None:
                from eap_schemas.knowledge import RESERVED_METADATA_KEY

                for row, record in zip(rows, extract_metadata, strict=True):
                    if record:
                        row["metadata"] = {**meta, RESERVED_METADATA_KEY: dict(record)}
            inserted = 0
            for start in range(0, len(rows), _INSERT_BATCH):
                result = session.execute(
                    insert(KbChunkModel.__table__)
                    .values(rows[start : start + _INSERT_BATCH])
                    .on_conflict_do_nothing(constraint="uq_kb_chunks_document_slot")
                    .returning(KbChunkModel.__table__.c.id)
                )
                inserted += len(result.all())
            session.commit()
        return UpsertResult(total=len(chunks), inserted=inserted)

    def apply_cached_embeddings(
        self,
        tenant_id: str,
        kb_id: str,
        generation: int,
        *,
        document_id: str | None = None,
        doc_version: int | None = None,
    ) -> int:
        """Copy an existing embedding onto every un-embedded chunk with the same `embed_text_hash`
        **in the same knowledge base and generation** (so the same model and dimensions). Scoped to
        one document version when given. Returns the chunks filled."""
        kid = _require_uuid(kb_id, "kb_id")
        scope, scope_params = _scope_clause(document_id, doc_version, "p.")
        with self.Session() as session:
            _, dims = self._writable_generation(session, tenant_id, kid, int(generation))
            # One indexed lookup per pending chunk (ix_kb_chunks_tenant_kb_generation_hash), not a
            # scan of the whole generation.
            filled = session.execute(
                text(
                    "UPDATE kb_chunks c SET embedding = hit.embedding, dimensions = hit.dimensions"
                    " FROM (SELECT p.id, src.embedding, src.dimensions FROM kb_chunks p"
                    " CROSS JOIN LATERAL (SELECT x.embedding, x.dimensions FROM kb_chunks x"
                    " WHERE x.tenant_id = p.tenant_id AND x.kb_id = p.kb_id AND x.generation = p.generation"
                    " AND x.embed_text_hash = p.embed_text_hash AND x.embedding IS NOT NULL"
                    " AND x.dimensions = :dims LIMIT 1) src"
                    " WHERE p.tenant_id = :t AND p.kb_id = :kb AND p.generation = :g AND p.embedding IS NULL"
                    f"{scope}) hit WHERE c.id = hit.id AND c.embedding IS NULL"
                ),
                {"t": tenant_id, "kb": kid, "g": int(generation), "dims": dims, **scope_params},
            ).rowcount
            session.commit()
        return filled

    def embedding_progress(
        self,
        tenant_id: str,
        kb_id: str,
        generation: int,
        *,
        document_id: str | None = None,
        doc_version: int | None = None,
    ) -> EmbeddingProgress:
        """Chunk counts of the generation (or one document version in it). `pending_tokens` is the
        cost cap's estimate after `apply_cached_embeddings`: the approximate tokens of the chunks still
        without an embedding, **counted once per distinct embed text** (max(approx_tokens) per
        `embed_text_hash`), because the worker embeds each distinct text once and fills duplicates
        from the cache. Same rule as `estimate_reindex`, so the dry run and the worker's cap agree."""
        kid = _require_uuid(kb_id, "kb_id")
        scope, scope_params = _scope_clause(document_id, doc_version)
        params = {"t": tenant_id, "kb": kid, "g": int(generation), **scope_params}
        where = f"tenant_id = :t AND kb_id = :kb AND generation = :g{scope}"
        with self.engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT count(*) AS total, count(embedding) AS embedded FROM kb_chunks WHERE {where}"),
                params,
            ).mappings().one()
            pending_tokens = conn.execute(
                text(
                    "SELECT coalesce(sum(t), 0) FROM (SELECT max(approx_tokens) AS t FROM kb_chunks"
                    f" WHERE {where} AND embedding IS NULL GROUP BY embed_text_hash) h"
                ),
                params,
            ).scalar_one()
        return EmbeddingProgress(int(row["total"]), int(row["embedded"]), int(pending_tokens))

    def fill_cached_embeddings(
        self, tenant_id: str, kb_id: str, generation: int, chunk_ids: Sequence[str]
    ) -> set[str]:
        """`apply_cached_embeddings` for the given chunk ids only (a batch the worker selected): copy
        an existing embedding of the same knowledge base, generation and `embed_text_hash` onto each
        of them still without one. Returns the ids filled."""
        kid = _require_uuid(kb_id, "kb_id")
        ids = [_require_uuid(c, "chunk id") for c in chunk_ids]
        if not ids:
            return set()
        with self.Session() as session:
            _, dims = self._writable_generation(session, tenant_id, kid, int(generation))
            filled = session.execute(
                text(
                    "UPDATE kb_chunks c SET embedding = hit.embedding, dimensions = hit.dimensions"
                    " FROM (SELECT p.id, src.embedding, src.dimensions FROM kb_chunks p"
                    " CROSS JOIN LATERAL (SELECT x.embedding, x.dimensions FROM kb_chunks x"
                    " WHERE x.tenant_id = p.tenant_id AND x.kb_id = p.kb_id AND x.generation = p.generation"
                    " AND x.embed_text_hash = p.embed_text_hash AND x.embedding IS NOT NULL"
                    " AND x.dimensions = :dims LIMIT 1) src"
                    " WHERE p.tenant_id = :t AND p.kb_id = :kb AND p.generation = :g AND p.embedding IS NULL"
                    " AND p.id = ANY(CAST(:ids AS uuid[]))) hit WHERE c.id = hit.id AND c.embedding IS NULL"
                    " RETURNING c.id"
                ),
                {"t": tenant_id, "kb": kid, "g": int(generation), "dims": dims, "ids": ids},
            ).scalars().all()
            session.commit()
        return {str(i) for i in filled}

    def next_unembedded_batch(
        self,
        tenant_id: str,
        kb_id: str,
        generation: int,
        *,
        document_id: str | None = None,
        doc_version: int | None = None,
        max_chunks: int = DEFAULT_BATCH_CHUNKS,
        max_tokens: int = DEFAULT_BATCH_TOKENS,
    ) -> list[PendingChunk]:
        """Up to `max_chunks` chunks still without an embedding, in (document, version, ordinal)
        order, cut at `max_tokens` approximate tokens (never fewer than one chunk). Empty when
        everything is embedded (a re-executed batch step then makes no provider call)."""
        kid = _require_uuid(kb_id, "kb_id")
        if max_chunks < 1:
            raise ValueError("max_chunks must be at least 1")
        scope, scope_params = _scope_clause(document_id, doc_version)
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, document_id, doc_version, ordinal, embed_text, embed_text_hash, approx_tokens"
                    " FROM kb_chunks WHERE tenant_id = :t AND kb_id = :kb AND generation = :g AND embedding IS NULL"
                    f"{scope} ORDER BY document_id, doc_version, ordinal LIMIT :n"
                ),
                {"t": tenant_id, "kb": kid, "g": int(generation), "n": int(max_chunks), **scope_params},
            ).mappings().all()
        batch: list[PendingChunk] = []
        tokens = 0
        for r in rows:
            if batch and tokens + r["approx_tokens"] > max_tokens:
                break
            tokens += r["approx_tokens"]
            batch.append(
                PendingChunk(
                    id=str(r["id"]),
                    document_id=str(r["document_id"]),
                    doc_version=r["doc_version"],
                    ordinal=r["ordinal"],
                    embed_text=r["embed_text"],
                    embed_text_hash=r["embed_text_hash"],
                    approx_tokens=r["approx_tokens"],
                )
            )
        return batch

    def write_embeddings(
        self,
        tenant_id: str,
        kb_id: str,
        generation: int,
        vectors: Mapping[str, Sequence[float]],
        *,
        embedding_model: str,
        run_id: str | None = None,
        event_key: str | None = None,
        event_type: str = "ingest_progress",
        event_data: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> WriteResult:
        """Write the batch's vectors (chunk id -> vector) and, in the same transaction, the run's
        event and usage row (`usage` as for EventLog.append, e.g. request_key `embed:batch:{i}`,
        operation 'embedding'). Only chunks still without an embedding are written, so a
        re-executed step writes nothing twice; the event key and usage request key are idempotent.

        `embedding_model` is the model that produced the vectors; it must be the generation's.

        Raises EmbeddingDimensionMismatch (nothing written) when a vector's length is not the
        generation's dimensions, GenerationModelMismatch when the generation belongs to another
        model, KnowledgeBaseNotFound (deleted: end the run cancelled), GenerationNotWritable, and
        RunNotFound for another tenant's run."""
        kid = _require_uuid(kb_id, "kb_id")
        if usage is not None and event_key is None:
            raise ValueError("usage is recorded with its event: pass event_key")
        if event_key is not None and run_id is None:
            raise ValueError("event_key needs run_id")
        ids: list[str] = []
        literals: list[str] = []
        with self.Session() as session:
            try:
                model, dims = self._writable_generation(
                    session, tenant_id, kid, int(generation), "FOR SHARE", embedding_model
                )
                for chunk_id, vector in vectors.items():
                    if len(vector) != dims:
                        raise EmbeddingDimensionMismatch(model, dims, len(vector))
                    ids.append(_require_uuid(chunk_id, "chunk id"))
                    literals.append(vector_literal(vector))
                written = 0
                if ids:
                    written = session.execute(
                        text(
                            "UPDATE kb_chunks c SET embedding = CAST(v.e AS public.vector), dimensions = :dims"
                            " FROM unnest(CAST(:ids AS uuid[]), CAST(:embs AS text[])) AS v(id, e)"
                            " WHERE c.id = v.id AND c.tenant_id = :t AND c.kb_id = :kb AND c.generation = :g"
                            " AND c.embedding IS NULL"
                        ),
                        {"t": tenant_id, "kb": kid, "g": int(generation), "dims": dims, "ids": ids, "embs": literals},
                    ).rowcount
                created = None
                if event_key is not None:
                    _, created = append_event_in_session(
                        session,
                        tenant_id,
                        str(run_id),
                        event_key,
                        event_type,
                        dict(event_data or {}),
                        None if usage is None else {"operation": "embedding", **dict(usage)},
                    )
                session.commit()
            except BaseException:
                session.rollback()
                raise
        return WriteResult(written=written, event_created=created)

    def finalize_document(
        self,
        tenant_id: str,
        document_id: str,
        doc_version: int,
        generation: int,
        *,
        embedding_model: str,
        run_id: str | None = None,
        event_key: str | None = None,
        event_type: str = "ingest_progress",
        event_data: Mapping[str, Any] | None = None,
    ) -> FinalizeResult:
        """The atomic version switch, in one transaction: when every chunk of `doc_version` in the
        active `generation` has an embedding, set `active_version`, `chunk_count`, status 'ready'
        (when it is the latest version) and delete the document's older versions' chunks (every
        generation). Optionally appends the run's event in the same transaction (also on
        'already_active', idempotent by key). See FinalizeResult for the statuses.
        Raises GenerationModelMismatch when the active `generation` belongs to another model than
        `embedding_model` (the model the run embedded with)."""
        did = _require_uuid(document_id, "document_id")
        v = int(doc_version)
        with self.Session() as session:
            try:
                # Lock order: the knowledge base FOR SHARE, then the document, then re-check.
                try:
                    kid = self._document_kb(session, tenant_id, did)
                except DocumentNotFound:
                    session.rollback()
                    return FinalizeResult("missing")
                kb = session.execute(
                    text(
                        "SELECT active_generation, embedding_model FROM knowledge_bases"
                        " WHERE tenant_id = :t AND id = :kb FOR SHARE"
                    ),
                    {"t": tenant_id, "kb": kid},
                ).mappings().first()
                if kb is None:
                    session.rollback()
                    return FinalizeResult("missing")
                doc = session.execute(
                    text(
                        "SELECT kb_id, active_version, latest_version FROM kb_documents"
                        " WHERE tenant_id = :t AND id = :id FOR NO KEY UPDATE"
                    ),
                    {"t": tenant_id, "id": did},
                ).mappings().first()
                if doc is None or str(doc["kb_id"]) != kid:
                    session.rollback()
                    return FinalizeResult("missing")
                if kb["active_generation"] == int(generation) and kb["embedding_model"] != embedding_model:
                    raise GenerationModelMismatch(kid, int(generation), kb["embedding_model"], embedding_model)
                params = {"t": tenant_id, "id": did, "v": v, "g": int(generation)}
                if doc["active_version"] == v:
                    count = session.execute(
                        text("SELECT chunk_count FROM kb_documents WHERE tenant_id = :t AND id = :id"), params
                    ).scalar_one()
                    if doc["latest_version"] == v:
                        session.execute(
                            text(
                                "UPDATE kb_documents SET status = 'ready', error = NULL, updated_at = now()"
                                " WHERE tenant_id = :t AND id = :id AND status <> 'ready'"
                            ),
                            params,
                        )
                    result = FinalizeResult("already_active", chunk_count=count)
                elif doc["active_version"] is not None and doc["active_version"] > v:
                    session.execute(
                        text("DELETE FROM kb_chunks WHERE tenant_id = :t AND document_id = :id AND doc_version = :v"),
                        params,
                    )
                    result = FinalizeResult("superseded")
                elif kb["active_generation"] != int(generation):
                    result = FinalizeResult("stale_generation")
                else:
                    total, embedded = session.execute(
                        text(
                            "SELECT count(*), count(embedding) FROM kb_chunks WHERE tenant_id = :t"
                            " AND document_id = :id AND doc_version = :v AND generation = :g"
                        ),
                        params,
                    ).one()
                    if embedded < total:
                        result = FinalizeResult("incomplete", chunk_count=total, pending=total - embedded)
                    else:
                        session.execute(
                            text(
                                "UPDATE kb_documents SET active_version = :v, chunk_count = :n, updated_at = now(),"
                                " status = CASE WHEN latest_version = :v THEN 'ready' ELSE status END,"
                                " error = CASE WHEN latest_version = :v THEN NULL ELSE error END"
                                " WHERE tenant_id = :t AND id = :id"
                            ),
                            {**params, "n": total},
                        )
                        session.execute(
                            text("DELETE FROM kb_chunks WHERE tenant_id = :t AND document_id = :id AND doc_version < :v"),
                            params,
                        )
                        result = FinalizeResult("switched", chunk_count=total)
                if event_key is not None and result.status in ("switched", "already_active"):
                    if run_id is None:
                        raise ValueError("event_key needs run_id")
                    append_event_in_session(
                        session, tenant_id, str(run_id), event_key, event_type, dict(event_data or {}), None
                    )
                session.commit()
            except BaseException:
                session.rollback()
                raise
        return result

    # ------------------------------------------------------------------ reindex (RM.7, RM.10)

    def start_reindex(self, tenant_id: str, kb_id: str, *, embedding_model: str, dimensions: int) -> dict[str, Any]:
        """Open generation `last_generation + 1` for `embedding_model` (allowlisted by the caller),
        status 'reindexing'. Generation numbers are never reused (`last_generation` only grows), so
        a batch of a cancelled reindex can never write into a later one. Retrieval keeps the active generation until `finalize_reindex`.
        Raises KnowledgeBaseNotFound, ReindexInProgress (one pending generation at a time)."""
        kid = _canonical_uuid(kb_id)
        if kid is None:
            raise KnowledgeBaseNotFound(str(kb_id))
        parse_embedding_model_id(embedding_model)
        _require_dimensions(dimensions)
        with self.Session() as session:
            kb = session.execute(
                text(
                    "SELECT active_generation, pending_generation, last_generation FROM knowledge_bases"
                    " WHERE tenant_id = :t AND id = :id FOR UPDATE"
                ),
                {"t": tenant_id, "id": kid},
            ).mappings().first()
            if kb is None:
                session.rollback()
                raise KnowledgeBaseNotFound(kid)
            if kb["pending_generation"] is not None:
                session.rollback()
                raise ReindexInProgress(kid, kb["pending_generation"])
            pending = kb["last_generation"] + 1
            # Defensive: no rows can carry a never-opened generation number.
            session.execute(
                text("DELETE FROM kb_chunks WHERE tenant_id = :t AND kb_id = :id AND generation >= :g"),
                {"t": tenant_id, "id": kid, "g": pending},
            )
            row = session.execute(
                text(
                    "UPDATE knowledge_bases SET pending_generation = :g, last_generation = :g,"
                    " pending_embedding_model = :model,"
                    " pending_dimensions = :dims, status = 'reindexing', error = NULL, updated_at = now()"
                    " WHERE tenant_id = :t AND id = :id RETURNING *"
                ),
                {"t": tenant_id, "id": kid, "g": pending, "model": embedding_model, "dims": int(dimensions)},
            ).mappings().one()
            session.commit()
        return _kb_dict(row)

    _COPY_ACTIVE_CHUNKS = (
        "INSERT INTO kb_chunks (tenant_id, kb_id, document_id, doc_version, generation, ordinal, heading_path, title,"
        " anchor_url, content, embed_text, embed_text_hash, approx_tokens, overlap, metadata)"
        " SELECT c.tenant_id, c.kb_id, c.document_id, c.doc_version, :to_g, c.ordinal, c.heading_path, c.title,"
        " c.anchor_url, c.content, c.embed_text, c.embed_text_hash, c.approx_tokens, c.overlap, c.metadata"
        " FROM kb_chunks c JOIN kb_documents d ON d.tenant_id = c.tenant_id AND d.id = c.document_id"
        " AND d.active_version = c.doc_version"
        " WHERE c.tenant_id = :t AND c.kb_id = :kb AND c.generation = :from_g"
        " ON CONFLICT ON CONSTRAINT uq_kb_chunks_document_slot DO NOTHING"
    )

    def copy_generation_chunks(self, tenant_id: str, kb_id: str, generation: int) -> int:
        """Copy the active generation's chunks of every active document version into the pending
        `generation`, without embeddings (no re-chunking). Idempotent; returns the rows inserted.
        Raises KnowledgeBaseNotFound, GenerationNotWritable (not the pending generation)."""
        kid = _require_uuid(kb_id, "kb_id")
        with self.Session() as session:
            kb = session.execute(
                text(
                    "SELECT active_generation, pending_generation FROM knowledge_bases"
                    " WHERE tenant_id = :t AND id = :id FOR SHARE"
                ),
                {"t": tenant_id, "id": kid},
            ).mappings().first()
            if kb is None:
                session.rollback()
                raise KnowledgeBaseNotFound(kid)
            if kb["pending_generation"] != int(generation):
                session.rollback()
                raise GenerationNotWritable(kid, int(generation))
            inserted = session.execute(
                text(self._COPY_ACTIVE_CHUNKS),
                {"t": tenant_id, "kb": kid, "from_g": kb["active_generation"], "to_g": int(generation)},
            ).rowcount
            session.commit()
        return inserted

    def finalize_reindex(
        self,
        tenant_id: str,
        kb_id: str,
        generation: int,
        *,
        embedding_model: str,
        run_id: str | None = None,
        event_key: str | None = None,
        event_type: str = "ingest_progress",
        event_data: Mapping[str, Any] | None = None,
    ) -> FinalizeResult:
        """The atomic generation switch, in one transaction holding the knowledge base row: first
        copies chunks of document versions activated since the copy (status 'incomplete' if any),
        then, when every chunk of `generation` has an embedding, sets `embedding_model`,
        `dimensions` and `active_generation`, clears the pending fields (status 'ready') and
        deletes every other generation's chunks. 'missing' when the knowledge base is gone or the
        reindex was cancelled; 'already_active' when an earlier call switched. Raises
        GenerationModelMismatch when `generation` belongs to another model than `embedding_model`
        (the model the reindex run embedded with)."""
        kid = _require_uuid(kb_id, "kb_id")
        g = int(generation)
        with self.Session() as session:
            try:
                kb = session.execute(
                    text("SELECT * FROM knowledge_bases WHERE tenant_id = :t AND id = :id FOR UPDATE"),
                    {"t": tenant_id, "id": kid},
                ).mappings().first()
                params = {"t": tenant_id, "kb": kid, "g": g}
                if kb is None:
                    session.rollback()
                    return FinalizeResult("missing")
                if kb["active_generation"] == g:
                    if kb["embedding_model"] != embedding_model:
                        raise GenerationModelMismatch(kid, g, kb["embedding_model"], embedding_model)
                    result = FinalizeResult("already_active")
                elif kb["pending_generation"] != g:
                    session.rollback()
                    return FinalizeResult("missing")
                elif kb["pending_embedding_model"] != embedding_model:
                    raise GenerationModelMismatch(kid, g, kb["pending_embedding_model"], embedding_model)
                else:
                    copied = session.execute(
                        text(self._COPY_ACTIVE_CHUNKS),
                        {"t": tenant_id, "kb": kid, "from_g": kb["active_generation"], "to_g": g},
                    ).rowcount
                    # Chunks of versions that are no longer active (replaced during the reindex).
                    session.execute(
                        text(
                            "DELETE FROM kb_chunks c USING kb_documents d WHERE c.tenant_id = :t AND c.kb_id = :kb"
                            " AND c.generation = :g AND d.tenant_id = c.tenant_id AND d.id = c.document_id"
                            " AND d.active_version IS DISTINCT FROM c.doc_version"
                        ),
                        params,
                    )
                    total, embedded = session.execute(
                        text(
                            "SELECT count(*), count(embedding) FROM kb_chunks"
                            " WHERE tenant_id = :t AND kb_id = :kb AND generation = :g"
                        ),
                        params,
                    ).one()
                    if copied or embedded < total:
                        session.commit()  # keep the copied rows: the next batches embed them
                        return FinalizeResult("incomplete", chunk_count=total, pending=total - embedded)
                    session.execute(
                        text(
                            "UPDATE knowledge_bases SET embedding_model = pending_embedding_model,"
                            " dimensions = pending_dimensions, active_generation = pending_generation,"
                            " pending_generation = NULL, pending_embedding_model = NULL, pending_dimensions = NULL,"
                            " status = 'ready', error = NULL, updated_at = now() WHERE tenant_id = :t AND id = :kb"
                        ),
                        params,
                    )
                    session.execute(
                        text("DELETE FROM kb_chunks WHERE tenant_id = :t AND kb_id = :kb AND generation <> :g"), params
                    )
                    result = FinalizeResult("switched", chunk_count=total)
                if event_key is not None:
                    if run_id is None:
                        raise ValueError("event_key needs run_id")
                    append_event_in_session(
                        session, tenant_id, str(run_id), event_key, event_type, dict(event_data or {}), None
                    )
                session.commit()
            except BaseException:
                session.rollback()
                raise
        return result

    def mark_reindex_failed(self, tenant_id: str, kb_id: str, generation: int, error: str) -> bool:
        """Status 'reindex_failed' with `error`; the pending generation and its embedded chunks stay
        for a retry (a new kb_ingest run of the same generation). Retrieval is unaffected."""
        kid = _require_uuid(kb_id, "kb_id")
        with self.engine.begin() as conn:
            return (
                conn.execute(
                    text(
                        "UPDATE knowledge_bases SET status = 'reindex_failed', error = :error, updated_at = now()"
                        " WHERE tenant_id = :t AND id = :id AND pending_generation = :g"
                    ),
                    {"t": tenant_id, "id": kid, "g": int(generation), "error": error},
                ).rowcount
                > 0
            )

    def claim_reindex_retry(self, tenant_id: str, kb_id: str, generation: int) -> bool:
        """Compare-and-set 'reindex_failed' -> 'reindexing' for pending `generation` (error cleared):
        of concurrent retries exactly one gets True and starts the retry run. False when the
        generation is not pending in the failed state (another retry won, or it was cancelled)."""
        kid = _canonical_uuid(kb_id)
        if kid is None:
            return False
        with self.engine.begin() as conn:
            return (
                conn.execute(
                    text(
                        "UPDATE knowledge_bases SET status = 'reindexing', error = NULL, updated_at = now()"
                        " WHERE tenant_id = :t AND id = :id AND pending_generation = :g AND status = 'reindex_failed'"
                    ),
                    {"t": tenant_id, "id": kid, "g": int(generation)},
                ).rowcount
                > 0
            )

    def cancel_reindex(self, tenant_id: str, kb_id: str) -> bool:
        """Drop the pending generation and its chunks (status 'ready'). False when none is pending."""
        kid = _canonical_uuid(kb_id)
        if kid is None:
            return False
        with self.engine.begin() as conn:
            kb = conn.execute(
                text("SELECT pending_generation FROM knowledge_bases WHERE tenant_id = :t AND id = :id FOR UPDATE"),
                {"t": tenant_id, "id": kid},
            ).first()
            if kb is None or kb.pending_generation is None:
                return False
            conn.execute(
                text("DELETE FROM kb_chunks WHERE tenant_id = :t AND kb_id = :id AND generation = :g"),
                {"t": tenant_id, "id": kid, "g": kb.pending_generation},
            )
            conn.execute(
                text(
                    "UPDATE knowledge_bases SET pending_generation = NULL, pending_embedding_model = NULL,"
                    " pending_dimensions = NULL, status = 'ready', error = NULL, updated_at = now()"
                    " WHERE tenant_id = :t AND id = :id"
                ),
                {"t": tenant_id, "id": kid},
            )
        return True

    def estimate_reindex(self, tenant_id: str, kb_id: str) -> ReindexEstimate:
        """The active chunks a reindex would embed, and their approximate tokens counted once per
        distinct embed text (the cache embeds duplicates once). The API's dry run prices it."""
        kid = _canonical_uuid(kb_id)
        if kid is None:
            raise KnowledgeBaseNotFound(str(kb_id))
        with self.engine.connect() as conn:
            kb = conn.execute(
                text("SELECT active_generation FROM knowledge_bases WHERE tenant_id = :t AND id = :id"),
                {"t": tenant_id, "id": kid},
            ).first()
            if kb is None:
                raise KnowledgeBaseNotFound(kid)
            row = conn.execute(
                text(
                    "SELECT count(*) AS chunks, coalesce(sum(t), 0) AS tokens FROM ("
                    " SELECT max(c.approx_tokens) AS t, count(*) AS n FROM kb_chunks c JOIN kb_documents d"
                    " ON d.tenant_id = c.tenant_id AND d.id = c.document_id AND d.active_version = c.doc_version"
                    " WHERE c.tenant_id = :t AND c.kb_id = :kb AND c.generation = :g GROUP BY c.embed_text_hash) h"
                ),
                {"t": tenant_id, "kb": kid, "g": kb.active_generation},
            ).mappings().one()
            chunks = conn.execute(
                text(
                    "SELECT count(*) FROM kb_chunks c JOIN kb_documents d ON d.tenant_id = c.tenant_id"
                    " AND d.id = c.document_id AND d.active_version = c.doc_version"
                    " WHERE c.tenant_id = :t AND c.kb_id = :kb AND c.generation = :g"
                ),
                {"t": tenant_id, "kb": kid, "g": kb.active_generation},
            ).scalar_one()
        return ReindexEstimate(chunks=int(chunks), approx_tokens=int(row["tokens"]))

    # ------------------------------------------------------------------ retrieval (RM.11)

    @staticmethod
    def _search_sql(dims: int, filter_clauses: Sequence[str], with_min_score: bool, exact: bool = False) -> str:
        # `dims` is one of HNSW_DIMENSIONS (checked), inlined so that the partial index predicate
        # and the cast match the index definition exactly. `exact`: ORDER BY `distance + 0`, an
        # expression no HNSW index matches, so the chunks are scanned (by the tenant/kb btree) and
        # sorted by exact distance.
        where = "".join(f" AND {c}" for c in filter_clauses)
        order_open, order_close = ("(", ") + 0") if exact else ("", "")
        cut = " WHERE hits.distance <= :max_distance" if with_min_score else ""
        return (
            "SELECT * FROM ("
            " SELECT c.id, c.kb_id, c.document_id, d.document_key, d.title AS document_title, c.doc_version,"
            " c.generation, c.ordinal, c.title, c.heading_path, c.anchor_url, c.content, c.metadata,"
            f" (c.embedding::public.vector({dims})) OPERATOR(public.<=>) CAST(:q AS public.vector({dims})) AS distance"
            " FROM kb_chunks c"
            " JOIN knowledge_bases kb ON kb.tenant_id = c.tenant_id AND kb.id = c.kb_id"
            " AND kb.active_generation = c.generation"
            " JOIN kb_documents d ON d.tenant_id = c.tenant_id AND d.id = c.document_id"
            " AND d.active_version = c.doc_version"
            f" WHERE c.tenant_id = :t AND c.kb_id = ANY(CAST(:kb_ids AS uuid[])) AND c.dimensions = {dims}"
            f" AND kb.tenant_id = :t AND kb.embedding_model = :model AND kb.dimensions = {dims}{where}"
            f" ORDER BY {order_open}(c.embedding::public.vector({dims})) OPERATOR(public.<=>) CAST(:q AS public.vector({dims})){order_close}"
            " LIMIT :k"
            f") hits{cut} ORDER BY hits.distance, hits.id"
        )

    def _prepare_search(
        self,
        session,
        tenant_id: str,
        knowledge_base_ids: Iterable[str],
        query_embedding: Sequence[float],
        embedding_model: str,
        top_k: int,
        min_score: float | None,
        metadata_filter: Mapping[str, Any] | None,
    ) -> tuple[tuple[str, str, dict[str, Any]] | None, dict[str, str]]:
        """((ANN SQL, exact SQL, params) with this transaction's HNSW settings applied, or None when
        no knowledge base is eligible; the requested knowledge bases of the tenant under another
        embedding model -> their model)."""
        _require_tenant(tenant_id)
        dims = len(query_embedding)
        if dims not in HNSW_DIMENSIONS:
            raise ValueError(f"query embedding has {dims} dimensions; expected one of {HNSW_DIMENSIONS}")
        if not 1 <= int(top_k) <= MAX_SEARCH_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_SEARCH_TOP_K}")
        if min_score is not None and not 0.0 <= float(min_score) <= 1.0:
            raise ValueError("min_score must be between 0 and 1")
        ids = sorted({i for i in (_canonical_uuid(k) for k in knowledge_base_ids) if i is not None})
        if not ids:
            return None, {}
        # The tenant's knowledge bases among the ids: another tenant's id simply matches nothing.
        kbs = session.execute(
            text(
                "SELECT id, embedding_model, dimensions FROM knowledge_bases"
                " WHERE tenant_id = :t AND id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"t": tenant_id, "ids": ids},
        ).mappings().all()
        for kb in kbs:
            if kb["embedding_model"] == embedding_model and kb["dimensions"] != dims:
                raise EmbeddingDimensionMismatch(embedding_model, kb["dimensions"], dims)
        eligible = [str(kb["id"]) for kb in kbs if kb["embedding_model"] == embedding_model]
        moved = {str(kb["id"]): str(kb["embedding_model"]) for kb in kbs if kb["embedding_model"] != embedding_model}
        if not eligible:
            return None, moved
        clauses, params = _filter_clauses(metadata_filter)
        ef_search = min(HNSW_MAX_EF_SEARCH, max(MIN_EF_SEARCH, 4 * int(top_k)))
        # SET LOCAL equivalents (bindable): reset when this transaction ends.
        session.execute(text("SELECT set_config('hnsw.ef_search', :v, true)"), {"v": str(ef_search)})
        session.execute(text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"))
        session.execute(
            text("SELECT set_config('hnsw.max_scan_tuples', :v, true)"), {"v": str(int(HNSW_MAX_SCAN_TUPLES))}
        )
        params.update(
            {
                "t": tenant_id,
                "kb_ids": eligible,
                "model": embedding_model,
                "q": vector_literal(query_embedding),
                "k": int(top_k),
            }
        )
        if min_score is not None:
            params["max_distance"] = 1.0 - float(min_score)
        with_min = min_score is not None
        return (
            (self._search_sql(dims, clauses, with_min), self._search_sql(dims, clauses, with_min, exact=True), params),
            moved,
        )

    @staticmethod
    def _active_chunk_count(session, params: Mapping[str, Any], limit: int) -> int:
        """The embedded chunks of the eligible knowledge bases' active generations (every document
        version: an upper bound), counted up to `limit + 1` only."""
        return int(
            session.execute(
                text(
                    "SELECT count(*) FROM (SELECT 1 FROM kb_chunks c JOIN knowledge_bases kb"
                    " ON kb.tenant_id = c.tenant_id AND kb.id = c.kb_id AND kb.active_generation = c.generation"
                    " WHERE c.tenant_id = :t AND c.kb_id = ANY(CAST(:kb_ids AS uuid[])) AND kb.tenant_id = :t"
                    " AND c.embedding IS NOT NULL LIMIT :n) x"
                ),
                {"t": params["t"], "kb_ids": params["kb_ids"], "n": int(limit) + 1},
            ).scalar_one()
        )

    def search(
        self,
        tenant_id: str,
        knowledge_base_ids: Iterable[str],
        query_embedding: Sequence[float],
        *,
        embedding_model: str,
        top_k: int,
        min_score: float | None = None,
        metadata_filter: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        """Nearest active chunks by cosine distance, one SQL query: `tenant_id` from trusted deps
        (never a model argument), `kb_id = ANY(ids ∩ the tenant's knowledge bases embedded with
        embedding_model)`, the active generation and document version only, the metadata filter
        (`metadata @> …`: key = value, a list = any of), the dimension predicate and cast of the
        partial HNSW index, `ORDER BY distance LIMIT top_k`, then the `min_score` cut
        (score = 1 - distance), re-sorted by exact distance. `hnsw.ef_search = max(40, 4 × top_k)`,
        `hnsw.iterative_scan = relaxed_order` and `hnsw.max_scan_tuples = HNSW_MAX_SCAN_TUPLES` are
        set for this transaction only.

        Exact-search fallback: the HNSW index is shared by every tenant, so a small knowledge base
        behind many nearer foreign rows can exhaust the scan with fewer than top_k rows. Then, when
        those knowledge bases' active chunks number at most EXACT_SEARCH_MAX_CHUNKS
        (EAP_KB_EXACT_SEARCH_MAX_CHUNKS) and more than were returned, the same query runs again,
        exactly (no index), in the same transaction. Larger knowledge bases keep the ANN result.

        Knowledge bases of another tenant, deleted ones, and ones under another embedding model
        give no rows (use existing_knowledge_base_ids for the "no longer exists" error). Raises
        EmbeddingDimensionMismatch when a knowledge base of `embedding_model` has other dimensions
        than the query vector, ValueError for unsupported dimensions or top_k."""
        return self.search_snapshot(
            tenant_id, knowledge_base_ids, query_embedding, embedding_model=embedding_model, top_k=top_k,
            min_score=min_score, metadata_filter=metadata_filter,
        ).hits

    def search_snapshot(
        self,
        tenant_id: str,
        knowledge_base_ids: Iterable[str],
        query_embedding: Sequence[float],
        *,
        embedding_model: str,
        top_k: int,
        min_score: float | None = None,
        metadata_filter: Mapping[str, Any] | None = None,
    ) -> SearchResult:
        """`search`, plus which requested knowledge bases are under another embedding model
        (SearchResult.moved), all read in ONE snapshot: the transaction is REPEATABLE READ and read
        only, so the knowledge base models, the active generations and the chunks it sees are
        consistent. A reindex that switches a knowledge base's model while the caller embedded the
        query is therefore always reported in `moved` (never silently an empty result), and the
        caller can re-embed with the new model and search again (eap_worker.platform_tools)."""
        with self.Session() as session:
            # First statement of the transaction: every later statement reads the same snapshot.
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            prepared, moved = self._prepare_search(
                session, tenant_id, knowledge_base_ids, query_embedding, embedding_model, top_k, min_score,
                metadata_filter,
            )
            if prepared is None:
                session.rollback()
                return SearchResult([], moved)
            sql, exact_sql, params = prepared
            rows = session.execute(text(sql), params).mappings().all()
            if len(rows) < int(top_k):
                limit = exact_search_max_chunks()
                if limit > 0 and len(rows) < self._active_chunk_count(session, params, limit) <= limit:
                    rows = session.execute(text(exact_sql), params).mappings().all()
            session.commit()
        return SearchResult([
            SearchHit(
                chunk_id=str(r["id"]),
                kb_id=str(r["kb_id"]),
                document_id=str(r["document_id"]),
                document_key=r["document_key"],
                document_title=r["document_title"],
                doc_version=r["doc_version"],
                generation=r["generation"],
                ordinal=r["ordinal"],
                title=r["title"],
                heading_path=tuple(r["heading_path"] or ()),
                anchor_url=r["anchor_url"],
                content=r["content"],
                metadata=r["metadata"] or {},
                distance=float(r["distance"]),
                score=1.0 - float(r["distance"]),
            )
            for r in rows
        ], moved)

    def explain_search(
        self,
        tenant_id: str,
        knowledge_base_ids: Iterable[str],
        query_embedding: Sequence[float],
        *,
        embedding_model: str,
        top_k: int,
        min_score: float | None = None,
        metadata_filter: Mapping[str, Any] | None = None,
        disable_seqscan: bool = True,
        exact: bool = False,
    ) -> str:
        """EXPLAIN of exactly the SQL `search` runs (`exact`: its exact-search fallback), with the
        same settings (diagnostics and tests: a small table makes the planner prefer a sequential
        scan, hence `disable_seqscan`). Empty when the search would not query (no eligible
        knowledge base)."""
        with self.Session() as session:
            prepared, _moved = self._prepare_search(
                session, tenant_id, knowledge_base_ids, query_embedding, embedding_model, top_k, min_score,
                metadata_filter,
            )
            if prepared is None:
                session.rollback()
                return ""
            ann_sql, exact_sql, params = prepared
            sql = exact_sql if exact else ann_sql
            if disable_seqscan:
                session.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(r[0] for r in session.execute(text("EXPLAIN " + sql), params))
            session.rollback()
        return plan


__all__ = [
    "ChunkInput",
    "ChunkLike",
    "DocumentNotFound",
    "DOCUMENT_STATUSES",
    "EmbeddingProgress",
    "FinalizeResult",
    "GenerationModelMismatch",
    "GenerationNotWritable",
    "HNSW_DIMENSIONS",
    "KB_STATUSES",
    "KbChunkModel",
    "KbDocumentModel",
    "KnowledgeBaseLimitExceeded",
    "KnowledgeBaseModel",
    "KnowledgeBaseNameConflict",
    "KnowledgeStore",
    "PendingChunk",
    "PutDocumentResult",
    "ReindexEstimate",
    "ReindexInProgress",
    "SearchHit",
    "SearchResult",
    "UpsertResult",
    "WriteResult",
    "content_hash",
    "vector_literal",
]
