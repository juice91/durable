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
    # The owner the conversation acts for (RM.9): set once, never changed. Omitted means the
    # principal's user id. An integration acting for its own end users passes their key.
    resource_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:@-]{1,128}$",
        description="1-128 characters of A-Z, a-z, 0-9, '_', '.', ':', '@' or '-'; default: the caller's user id",
    )

    @field_validator("resource_id")
    @classmethod
    def validate_resource_id(cls, value: str | None) -> str | None:
        """The store's rule (eap_run_store.validate_resource_id): also no dot-only ids (`.`, `..`)."""
        from eap_run_store import validate_resource_id

        return validate_resource_id(value)

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
    """Why `key` is not a valid document key, or None. A key is 1–256 characters with no '/' or
    '\\', no control character, no surrounding whitespace, and not only dots: the key is a URL
    path segment, and a client (or proxy) normalizes `.` and `..` segments away, so a DELETE of
    document `..` would otherwise reach the knowledge base itself (checkpoint review, 2026-11-14)."""
    if not 1 <= len(key) <= MAX_DOCUMENT_KEY_LENGTH:
        return f"document_key must be 1 to {MAX_DOCUMENT_KEY_LENGTH} characters"
    if "/" in key or "\\" in key or any(not ch.isprintable() for ch in key) or key != key.strip():
        return "document_key must not contain '/', '\\', control characters, or surrounding whitespace"
    if set(key) == {"."}:
        return "document_key must not be only dots ('.' and '..' are path segments)"
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


class KnowledgeBaseDetailResponse(KnowledgeBaseResponse):
    """GET /knowledge-bases/{id}: the knowledge base plus what its `kb_ingest` runs (ingests and
    reindexes, finished or in flight) have spent so far, summed over their model requests:
    `ingest_tokens` (input + output tokens), `ingest_cost_usd` (a decimal string, null when any of
    those requests had no price, "0.0000000000" when there were none) and `ingest_requests`.
    Search tester runs are not counted."""

    ingest_tokens: int
    ingest_cost_usd: str | None
    ingest_requests: int


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
    """A page of documents by document_key. `next_after` is an opaque cursor when a full page came
    back (pass it back verbatim as `after`), else null."""

    items: list[KnowledgeDocumentResponse]
    next_after: str | None = None


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
    """An agent attaching the knowledge base: through its latest published version (`version`), or
    only through its current draft (`draft` true, `version` null)."""

    agent_id: str
    agent_name: str
    version: int | None
    archived: bool
    draft: bool = False


class EnqueueFailedResponse(BaseModel):
    """503: the run is recorded (marked failed) but could not be enqueued. `detail` says how to retry."""

    detail: str
    run_id: str


class KnowledgeBaseInUse(BaseModel):
    """409 of DELETE /knowledge-bases/{id} without force: the agents whose latest published version
    or current draft attaches the knowledge base."""

    detail: str
    agents: list[KnowledgeBaseReference]


# ------------------------------------------------------------------------------ memory (RM.9)


class WorkingMemoryPut(BaseModel):
    """Body of PUT .../memory/working: the whole note, replacing it. A string in template mode
    (Markdown, at most 16 KiB), an object in schema mode (validated against the agent's latest
    published `memory.working.json_schema`). Send `If-Match: <version>` (0: the note must not exist
    yet) to fail with 409 instead of overwriting a newer edit."""

    model_config = ConfigDict(extra="forbid")

    content: str | dict[str, Any]


class WorkingMemoryResponse(BaseModel):
    """One working memory note. `scope_key` is the conversation id (scope `conversation`) or the
    resource id (scope `resource`). `content` is a string (format `markdown`) or an object (`json`).
    The response's ETag header is the quoted version."""

    agent_id: str
    scope: Literal["conversation", "resource"]
    scope_key: str
    format: Literal["markdown", "json"]
    content: str | dict[str, Any]
    version: int
    updated_by_run_id: str | None
    created_at: datetime
    updated_at: datetime


class WorkingMemoryListResponse(BaseModel):
    items: list[WorkingMemoryResponse]


class ObservationResponse(BaseModel):
    id: str
    agent_id: str
    conversation_id: str
    kind: Literal["observation", "reflection"]
    covers_from_turn: int
    covers_to_turn: int
    content: str
    approx_tokens: int
    run_id: str | None
    superseded_by: str | None
    created_at: datetime
    # A retention marker (RM.15): its text aged out (`content` "", `approx_tokens` 0), but its turns
    # stay covered so they are not observed again. Never shown to the model.
    expired: bool = False


class ObservationListResponse(BaseModel):
    """`covered_to_turn`: the highest turn the active rows cover, expired markers included (0:
    none); history after it is what the model sees verbatim."""

    items: list[ObservationResponse]
    covered_to_turn: int


class ObservationsDeleted(BaseModel):
    deleted: int


class ForgetResourceResponse(BaseModel):
    """What a forget removed. `agent_id` null: every agent of the tenant. `conversations_deleted`
    (include_conversations) lists the conversations deleted; `busy_conversations` the ones skipped
    because a turn started between the memory delete and the conversation delete (retry them)."""

    resource_id: str
    agent_id: str | None
    working_memories_deleted: int
    message_embeddings_deleted: int
    conversations_deleted: list[str]
    busy_conversations: list[str]


class BusyDetail(BaseModel):
    """409 while a turn is queued or running; nothing was deleted. Retry after it finishes."""

    detail: str
    busy_conversations: list[str] = []


class ValidationIssue(BaseModel):
    """One request validation problem: where and what, never the rejected value."""

    loc: list[int | str]
    msg: str
    type: str


class UnprocessableDetail(BaseModel):
    """422: a message (a rule the request broke, e.g. invalid working memory content, which names a
    path, never a value) or the request validation problems."""

    detail: str | list[ValidationIssue]
