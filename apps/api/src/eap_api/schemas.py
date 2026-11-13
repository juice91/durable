"""Pydantic schemas for API request/response bodies."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from eap_schemas import AgentSpec, ModelRef
from eap_schemas.knowledge import ChunkingConfig, validate_metadata as validate_kb_metadata
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentCreate(BaseModel):
    """Schema for creating a new agent."""

    model_config = ConfigDict(extra="forbid")

    spec: AgentSpec


class AgentUpdate(BaseModel):
    """Schema for updating an agent's draft spec."""

    model_config = ConfigDict(extra="forbid")

    spec: AgentSpec


class AgentResponse(BaseModel):
    """Schema for agent response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: str
    name: str
    draft_spec: dict | None
    archived: bool
    created_at: datetime
    updated_at: datetime
    latest_version: int | None = None


class AgentListResponse(BaseModel):
    """Schema for agent list response."""

    model_config = ConfigDict(from_attributes=True)

    items: list[AgentResponse]


class AgentVersionResponse(BaseModel):
    """Schema for agent version response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    tenant_id: str
    version: int
    spec: dict
    spec_hash: str
    created_at: datetime
    created_by: str
    restored_from: int | None = None


class VersionRestore(BaseModel):
    """Body of POST /agents/{id}/versions/{v}/restore (optional). `discard_draft` lets a restore
    replace a draft that has unpublished changes; without it such a restore is a 409."""

    model_config = ConfigDict(extra="forbid")

    discard_draft: bool = False


class AgentVersionListResponse(BaseModel):
    """Schema for agent version list response."""

    model_config = ConfigDict(from_attributes=True)

    items: list[AgentVersionResponse]


class HealthzResponse(BaseModel):
    """Schema for health check response."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"


class ErrorDetail(BaseModel):
    """Schema for error detail."""

    model_config = ConfigDict(extra="forbid")

    detail: str


# A run's target: a published version number, "draft" (a draft test run of the agent's current
# draft, validated like a publish and snapshotted on the run), or null for the latest version.
RunVersion = Annotated[int, Field(ge=1)] | Literal["draft"] | None


class RunCreate(BaseModel):
    """Body of POST /agents/{agent_id}/runs. `version` null means the latest published version,
    "draft" a draft test run. `request_context` holds values for the target spec's
    `context_fields` (validated by the API; reserved identity names are refused). `model`
    overrides the spec's model for this run (allowlist and provider rules apply)."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID  # client-generated; retrying with the same run_id is idempotent
    input: str
    version: RunVersion = None
    request_context: dict[str, Any] | None = None
    model: ModelRef | None = None

    @field_validator("input")
    @classmethod
    def validate_input_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 32 * 1024:
            raise ValueError("input must be at most 32 KiB in UTF-8")
        return value


class ConversationCreate(BaseModel):
    """Body of POST /agents/{agent_id}/conversations. `conversation_id` makes a retry idempotent.
    `title` follows the rename rule (trimmed, 1–200 characters, one line); empty or blank is None."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=1000)
    conversation_id: UUID | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        """Same rule as a rename; empty or blank means no title, so the first turn titles it."""
        if value is None or not value.strip():
            return None
        return conversation_title(value)


def conversation_title(value: str) -> str:
    """A conversation title as rename and create accept it: trimmed, 1–200 characters, one line."""
    title = value.strip()
    if not 1 <= len(title) <= 200:
        raise ValueError("title must be 1 to 200 characters")
    if any(not ch.isprintable() for ch in title):
        raise ValueError("title must be a single line without control characters")
    return title


class ConversationRename(BaseModel):
    """Body of PATCH /conversations/{id}: the new title, 1–200 characters after trimming surrounding
    whitespace, on one line (no newline, tab or other control character)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=1000)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return conversation_title(value)


class TurnCreate(RunCreate):
    """Body of POST /conversations/{id}/runs: the next turn, same fields as a run start."""


class RequestContextValidate(BaseModel):
    """Body of POST /agents/{agent_id}/request-context/validate: the context to check against a
    published version's (`version`, null for the latest) or the draft's (`version: "draft"`)
    context fields. Errors come back exactly as on run start."""

    model_config = ConfigDict(extra="forbid")

    version: RunVersion = None
    request_context: dict[str, Any] | None = None


# ------------------------------------------------------------------ knowledge bases (RM.7)

MAX_KB_NAME_LENGTH = 100
MAX_KB_DESCRIPTION_LENGTH = 2000
MAX_DOCUMENT_KEY_LENGTH = 256
MAX_DOCUMENT_TITLE_LENGTH = 500
MAX_SOURCE_URL_LENGTH = 2048
MAX_MIME_TYPE_LENGTH = 100


def kb_name(value: str) -> str:
    """A knowledge base name: trimmed, 1–100 characters, one line (it is shown to the model)."""
    name = value.strip()
    if not 1 <= len(name) <= MAX_KB_NAME_LENGTH:
        raise ValueError(f"name must be 1 to {MAX_KB_NAME_LENGTH} characters")
    if any(not ch.isprintable() for ch in name):
        raise ValueError("name must be a single line without control characters")
    return name


def document_key_problem(key: str) -> str | None:
    """Why `key` is not a valid document key (1–256 characters, no '/', no control character), or None."""
    if not 1 <= len(key) <= MAX_DOCUMENT_KEY_LENGTH:
        return f"document_key must be 1 to {MAX_DOCUMENT_KEY_LENGTH} characters"
    if "/" in key or any(not ch.isprintable() for ch in key) or key != key.strip():
        return "document_key must not contain '/', control characters, or surrounding whitespace"
    return None


def source_url_value(value: str | None) -> str | None:
    """An http(s) URL (it becomes a citation link in Studio, so no javascript: or data: URLs)."""
    if value is None or not value.strip():
        return None
    url = value.strip()
    if len(url) > MAX_SOURCE_URL_LENGTH:
        raise ValueError(f"source_url must be at most {MAX_SOURCE_URL_LENGTH} characters")
    scheme, sep, rest = url.partition("://")
    if not sep or scheme.lower() not in ("http", "https") or not rest or any(ch.isspace() for ch in url):
        raise ValueError("source_url must be an absolute http or https URL")
    return url


class KnowledgeBaseCreate(BaseModel):
    """Body of POST /knowledge-bases. The embedding model must be in EAP_ALLOWED_EMBEDDING_MODELS with
    known dimensions; it is fixed for the knowledge base until a reindex. `chunking` defaults to
    {max_tokens: 512, overlap_tokens: 64, min_tokens: 40}."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=1000)
    description: str | None = Field(default=None, max_length=MAX_KB_DESCRIPTION_LENGTH)
    embedding_model: str = Field(max_length=300)
    chunking: ChunkingConfig | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return kb_name(value)


class KnowledgeBaseUpdate(BaseModel):
    """Body of PATCH /knowledge-bases/{id}: `name` and/or `description` (null clears it). The
    embedding model changes only through POST /knowledge-bases/{id}/reindex."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=MAX_KB_DESCRIPTION_LENGTH)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("name cannot be null")
        return kb_name(value)


class DocumentPut(BaseModel):
    """Body of PUT /knowledge-bases/{id}/documents/{document_key}: the document as UTF-8 text.

    `document_key` in the body is optional and must equal the path's. `title` defaults to the key.
    `mime_type` is text/markdown, text/html or text/plain (parameters such as charset are ignored).
    `metadata`: a flat map of at most 16 string, number or boolean values (agents filter on it).
    `run_id` (optional, client-generated) makes the ingest run creation idempotent on a retry."""

    model_config = ConfigDict(extra="forbid")

    document_key: str | None = None
    title: str | None = Field(default=None, max_length=MAX_DOCUMENT_TITLE_LENGTH)
    content: str
    mime_type: str = Field(max_length=MAX_MIME_TYPE_LENGTH)
    source_url: str | None = None
    metadata: dict[str, Any] | None = None
    run_id: UUID | None = None

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        return source_url_value(value)

    @field_validator("metadata")
    @classmethod
    def validate_metadata_map(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return None if value is None else validate_kb_metadata(value)


class IngestRunCreate(BaseModel):
    """Body of POST …/documents/{document_key}/reingest (optional): `run_id` for an idempotent retry."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID | None = None


class ReindexCreate(BaseModel):
    """Body of POST /knowledge-bases/{id}/reindex. `dry_run` returns the estimate and starts nothing."""

    model_config = ConfigDict(extra="forbid")

    embedding_model: str = Field(max_length=300)
    dry_run: bool = False
    run_id: UUID | None = None


class KnowledgeBaseResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None
    embedding_model: str
    dimensions: int
    chunking: dict[str, Any]
    active_generation: int
    last_generation: int
    pending_generation: int | None
    pending_embedding_model: str | None
    pending_dimensions: int | None
    status: Literal["ready", "reindexing", "reindex_failed"]
    error: str | None
    created_at: datetime
    updated_at: datetime
    document_count: int | None = None
    chunk_count: int | None = None


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseResponse]


class KnowledgeDocumentResponse(BaseModel):
    """A document without its content (GET with ?include_content=true adds `content`)."""

    id: str
    tenant_id: str
    kb_id: str
    document_key: str
    title: str
    source_url: str | None
    mime_type: str
    metadata: dict[str, Any]
    content_hash: str
    active_version: int | None
    latest_version: int
    status: Literal["pending", "ingesting", "ready", "failed"]
    error: str | None
    chunk_count: int
    last_run_id: str | None
    created_at: datetime
    updated_at: datetime
    content: str | None = None


class KnowledgeDocumentListResponse(BaseModel):
    items: list[KnowledgeDocumentResponse]


class DocumentPutResponse(BaseModel):
    """`unchanged` true: nothing written, no run (`run_id` null). Otherwise `run_id` is the ingest
    run of `document.latest_version` (watch it on GET /runs/{run_id}/stream)."""

    unchanged: bool
    created: bool
    document: KnowledgeDocumentResponse
    run_id: str | None
    status: str | None = None
    display_status: str | None = None
    trace_id: str | None = None


class IngestRunResponse(BaseModel):
    """A started (or replayed) kb_ingest run. A reingest of a document whose latest version is
    already active answers 200 with `run_id` null and `unchanged` true."""

    run_id: str | None
    kind: Literal["kb_ingest"] = "kb_ingest"
    knowledge_base_id: str
    status: str | None = None
    display_status: str | None = None
    trace_id: str | None = None
    unchanged: bool = False
    document: KnowledgeDocumentResponse | None = None
    knowledge_base: KnowledgeBaseResponse | None = None


class ReindexEstimateResponse(BaseModel):
    """The dry run of a reindex: what it would embed and, when the price is known to the API, cost."""

    dry_run: Literal[True] = True
    knowledge_base_id: str
    embedding_model: str
    dimensions: int
    chunks: int
    approx_tokens: int
    max_ingest_tokens: int
    exceeds_budget: bool
    estimated_cost_usd: str | None
    price_source: str | None
    cost_note: str | None
    reindex_pending: bool


class EmbeddingModelItem(BaseModel):
    id: str
    provider: str
    name: str
    dimensions: int
    wildcard: bool


class EmbeddingModelListResponse(BaseModel):
    items: list[EmbeddingModelItem]


class KnowledgeBaseReference(BaseModel):
    agent_id: str
    agent_name: str
    version: int
    archived: bool


class EnqueueFailedResponse(BaseModel):
    """503: the run is recorded (marked failed) but could not be enqueued. `detail` says how to retry."""

    detail: str
    run_id: str


class KnowledgeBaseInUse(BaseModel):
    """409 of DELETE /knowledge-bases/{id} without force: the agents whose latest published version
    attaches the knowledge base."""

    detail: str
    agents: list[KnowledgeBaseReference]
