"""Pydantic schemas for API request/response bodies."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from eap_schemas import AgentSpec, ModelRef, ModelSettings
from eap_schemas.model_settings_support import drop_recorded_decision
from eap_schemas.evals import DEFAULT_MAX_CONCURRENCY, MAX_MAX_CONCURRENCY, EvaluatorSpec
from eap_schemas.knowledge import (
    ChunkingConfig,
    ExtractConfig,
    new_knowledge_base_chunking,
    validate_metadata as validate_kb_metadata,
)
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictInt, ValidationError, field_validator, model_validator


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


class PublishWarning(BaseModel):
    """Publish-time advice in the publish and restore responses (`warnings`); never blocks a publish.
    `path` names the spec field (`model`, `fallback_models[1]`, `output_processors[0]`)."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["cost_cap_unpriced_model", "redaction_may_break_output_schema"]
    path: str
    message: str


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
    overrides the spec's model for this run (allowlist and provider rules apply). `model_settings`
    overrides the spec's model settings for this run, field by field (a field left out keeps the
    spec's value; ranges and provider rules as at publish)."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID  # client-generated; retrying with the same run_id is idempotent
    input: str
    version: RunVersion = None
    request_context: dict[str, Any] | None = None
    model: ModelRef | None = None
    model_settings: ModelSettings | None = None

    @field_validator("model_settings", mode="before")
    @classmethod
    def drop_recorded_decision(cls, value: Any) -> Any:
        return drop_recorded_decision(value)  # a run's returned settings can be re-posted as they are

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


class ChunkingCreate(BaseModel):
    """The `chunking` of POST /knowledge-bases: every field optional, JSON integers only. The
    knowledge base's config is `new_knowledge_base_chunking(**the fields given)` (RAG P1): chunker
    v2 unless `chunker_version` says otherwise, `min_tokens` 64 for v2 (40 for v1) unless given."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: StrictInt | None = None
    overlap_tokens: StrictInt | None = None
    min_tokens: StrictInt | None = None
    chunker_version: StrictInt | None = None
    extract: ExtractConfig | None = None


class KnowledgeBaseCreate(BaseModel):
    """Body of POST /knowledge-bases. The embedding model must be in EAP_ALLOWED_EMBEDDING_MODELS with
    known dimensions; it is fixed for the knowledge base until a reindex. `chunking` defaults to
    {max_tokens: 512, overlap_tokens: 64, min_tokens: 64, chunker_version: 2} (RAG P1: new knowledge
    bases use chunker v2; `chunker_version: 1` keeps v1 and its min_tokens default of 40)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=1000)
    description: str | None = Field(default=None, max_length=MAX_KB_DESCRIPTION_LENGTH)
    embedding_model: str = Field(max_length=300)
    chunking: ChunkingCreate | None = None
    _config: ChunkingConfig = PrivateAttr()

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return kb_name(value)

    @model_validator(mode="after")
    def _build_chunking(self) -> "KnowledgeBaseCreate":
        given: dict[str, object] = {}
        if self.chunking is not None:
            # Validated values only (StrictInt, ExtractConfig), never the raw request JSON.
            given = {
                k: getattr(self.chunking, k)
                for k in self.chunking.model_fields_set
                if getattr(self.chunking, k) is not None
            }
        try:
            self._config = new_knowledge_base_chunking(**given)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "invalid"}
            where = ".".join(str(p) for p in first.get("loc", ()))
            msg = str(first.get("msg", "invalid")).removeprefix("Value error, ")
            raise ValueError(f"chunking{'.' + where if where else ''}: {msg}") from None
        return self

    def chunking_config(self) -> ChunkingConfig:
        """The new knowledge base's chunking config (`new_knowledge_base_chunking`)."""
        return self._config


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
    """Body of PUT /knowledge-bases/{id}/documents/{document_key}.

    Exactly one of `content` (UTF-8 text: Markdown, HTML, plain text, reStructuredText, YAML, JSON)
    and `content_base64` (standard base64 with padding: binary types, i.e. application/pdf).
    `document_key` in the body is optional and must equal the path's. `title` defaults to the key.
    `mime_type`: a type or alias of the format registry (`eap_knowledge.formats`; parameters such as
    charset and case are ignored); it is stored as the canonical type.
    `metadata`: a flat map of at most 16 string, number or boolean values (agents filter on it); the
    key `eap` is reserved.
    `run_id` (optional, client-generated) makes the ingest run creation idempotent on a retry."""

    model_config = ConfigDict(extra="forbid")

    document_key: str | None = None
    title: str | None = Field(default=None, max_length=MAX_DOCUMENT_TITLE_LENGTH)
    content: str | None = None
    content_base64: str | None = None
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

    @model_validator(mode="after")
    def _one_content(self) -> "DocumentPut":
        if (self.content is None) == (self.content_base64 is None):
            raise ValueError("exactly one of content and content_base64 is required")
        return self


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


class ReextractCreate(BaseModel):
    """Body of POST /knowledge-bases/{id}/reextract (RAG P1).

    - `formats`: registry format names (`markdown`, `html`, `text`, `rst`, `pdf`, `yaml`, `json`) to
      limit the selection to; omitted means every format.
    - `only_outdated` (default true): only documents whose recorded extraction is not the current
      one: a worker-extracted type whose active chunks do not record its current extractor at its
      current version (`metadata.eap`), or an API-extracted type whose active chunks still record a
      worker extractor (e.g. Markdown extracted as Doxygen, after `markdown_flavor` went back to
      plain). Other API-extracted documents are never outdated: re-extract them with
      `only_outdated: false`. A document whose re-extract from an earlier call is still pending or
      ingesting is not selected, so repeating the call before those runs end starts nothing
      (`only_outdated: false` and `chunker_version` select regardless; a repeated chunker switch
      is itself a no-op).
    - `chunker_version: 2`: move the knowledge base to chunker v2 and re-extract EVERY document
      (`formats` must be omitted; `only_outdated` is ignored). On a knowledge base already on v2
      it changes nothing and the call selects as without it.
    - `dry_run`: answer the selection (200) and start nothing."""

    model_config = ConfigDict(extra="forbid")

    formats: list[str] | None = Field(default=None, min_length=1, max_length=16)
    only_outdated: bool = True
    chunker_version: Literal[2] | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def _chunker_takes_all(self) -> "ReextractCreate":
        if self.chunker_version is not None and self.formats is not None:
            raise ValueError("chunker_version re-extracts every document; leave formats out")
        return self


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
    # RAG P1. "base64" for a binary document (a PDF): it is stored as base64, and `content` is
    # always null in responses (the bytes are never returned). Null for text documents.
    content_encoding: Literal["base64"] | None = None
    # From `error` ("<code>: <message>"): the ingest failure code (INGEST_FAILURE_CODES) and, for an
    # extraction failure with a fixed message, its extraction code (e.g. "document_encrypted").
    error_code: str | None = None
    error_reason: str | None = None
    # GET of one document only: the worker extraction of its active version (null for API-extracted
    # documents, or while no version is active).
    extraction: "DocumentExtraction | None" = None


class DocumentExtraction(BaseModel):
    """The `extracted` event of the document's active version (counts only, never text), plus `doc_version`,
    `run_id` and `current_version` (the extractor's current version; lower `extractor_version`
    means a re-extract would use a newer extractor). `empty_reason` marks an empty document."""

    model_config = ConfigDict(extra="ignore")

    extractor: str
    extractor_version: int
    current_version: int | None = None
    doc_version: int | None = None
    run_id: str | None = None
    sections: int | None = None
    chars: int | None = None
    pages: int | None = None
    image_only_pages: list[int] | None = None
    conditions: int | None = None
    warnings: int | None = None
    # "no_extractable_text" when the active version extracted no text: ready with 0 chunks, not failed.
    empty_reason: str | None = None


KnowledgeDocumentResponse.model_rebuild()


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


class ReextractDocumentRef(BaseModel):
    """A document selected by a bulk re-extract, with the ingest run of its new version (null in a
    dry run, or when the run could not be started: see `detail`)."""

    document_id: str
    document_key: str
    mime_type: str
    doc_version: int
    run_id: str | None = None
    status: str | None = None
    detail: str | None = None


class ReextractResponse(BaseModel):
    """POST /knowledge-bases/{id}/reextract. 202 when runs were started, 200 for a dry run or when
    nothing was selected. `selected` documents matched; `started` got a queued run; `failed` lists
    those whose run could not be enqueued (each is `pending` with its run marked failed: retry it
    with POST …/documents/{key}/reingest). `chunker_version_changed`: the call moved the knowledge
    base to chunker v2."""

    knowledge_base_id: str
    dry_run: bool = False
    chunker_version: int
    chunker_version_changed: bool = False
    selected: int
    started: int
    failed: int
    documents: list[ReextractDocumentRef]
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


# ---------------------------------------------------------------------------- evals (AC.12)
# Bodies of the test suite routes (routes/evals.py). Evaluators and cases are validated with the
# eap_schemas.evals contracts: the field types here give FastAPI's 422 list for a malformed body;
# the store re-checks the rules that span fields (evaluator names unique across a suite's and a
# case's evaluators, the 500-case cap) and answers a 422 with a message.


class EvalSuiteCreate(BaseModel):
    """Body of POST /agents/{agent_id}/eval-suites. `suite_id` lets a client choose the id."""

    model_config = ConfigDict(extra="forbid")

    suite_id: UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    evaluators: list[EvaluatorSpec] = Field(default_factory=list, max_length=10)


class EvalSuiteUpdate(BaseModel):
    """Body of PATCH /eval-suites/{suite_id}: only the fields present change. `archived` true
    archives the suite (it can then not be edited or run), false restores it."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    evaluators: list[EvaluatorSpec] | None = Field(default=None, max_length=10)
    archived: bool | None = None


class EvalCaseCreate(BaseModel):
    """Body of POST /eval-suites/{suite_id}/cases (an EvalCase, plus an optional id and position;
    the default position is after the last case)."""

    model_config = ConfigDict(extra="forbid")

    case_id: UUID | None = None
    name: str
    input: str
    expected_output: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    evaluators: list[EvaluatorSpec] = Field(default_factory=list, max_length=5)
    position: int | None = Field(default=None, ge=0)


class EvalCaseUpdate(BaseModel):
    """Body of PATCH /eval-suites/{suite_id}/cases/{case_id}: only the fields present change
    (`expected_output: null` clears it); the merged case is validated as a whole."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    input: str | None = None
    expected_output: Any = None
    metadata: dict[str, Any] | None = None
    evaluators: list[EvaluatorSpec] | None = Field(default=None, max_length=5)
    position: int | None = Field(default=None, ge=0)


class EvalRunCreate(BaseModel):
    """Body of POST /agents/{agent_id}/evals/runs. `run_id` is client-generated: a retry with the
    same body is idempotent. `version`: a published version, "draft", or null for the latest
    version. `max_concurrency` (1..8, default 4) is frozen into the run's suite snapshot."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    suite_id: UUID
    version: RunVersion = None
    max_concurrency: int = Field(default=DEFAULT_MAX_CONCURRENCY, ge=1, le=MAX_MAX_CONCURRENCY)


# --------------------------------------------------------------------------- run cancellation (loops D12)


class RunCancelResponse(BaseModel):
    """202 answer of POST /runs/{run_id}/cancel (a workflow run). `status` is the run's status
    after the cancel, always equal to what its `end` event fixes: `failed` (or `needs_attention`
    when one of its side effects was still reserved) with `error_code` `run_cancelled`, or the
    run's own outcome when it had already ended by itself before the terminal write (a retry).
    `cancelled` is True when the run's `end` is the cancel's (`error_code` `run_cancelled`).
    `approvals_closed` counts the open approval inbox rows this call closed (0 on a retry)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    kind: Literal["workflow", "kb_source_sync"]
    status: str
    display_status: str
    error_code: str | None
    cancelled: bool
    requested_by: str | None
    approvals_closed: int


class CancelNotSupportedDetail(BaseModel):
    """409 answer of POST /runs/{run_id}/cancel for a run that is not a workflow run."""

    model_config = ConfigDict(extra="forbid")

    detail: str
    code: Literal["cancel_not_supported"]
