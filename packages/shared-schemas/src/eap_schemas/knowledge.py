"""Knowledge base contracts (RAG and memory milestone, task RM.2).

- `KnowledgeBaseRef`: an agent spec's reference to a tenant knowledge base, with retrieval
  settings. Referenced by id (like MCP servers), never snapshotted.
- `ChunkingConfig`: the per-knowledge-base chunking settings `eap_knowledge` (RM.3) consumes and
  `knowledge_bases.chunking` (RM.4) stores.
- Metadata and filter value rules shared by the document upload (RM.7) and the spec filter.
- `approx_tokens`: the platform's `ceil(chars / 4)` token estimate (chunking, history limits,
  cost cap), never 0 for non-empty text.
- Error types, pickle-safe (raised inside DBOS steps).
- RAG P1 (task P1.A1): `ExtractConfig` (`ChunkingConfig.extract`, omitted while empty), chunker
  versions 1 and 2, the reserved chunk metadata key `eap` (`ChunkExtractMetadata`), the `extracted`
  event's data (`ExtractedEventData`) and the extraction error codes with fixed user-safe messages.

No I/O here.
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Annotated, Any, Literal, Union

from pydantic import Field, field_validator, model_validator

from eap_schemas._omit import OmitNoneModel

MAX_KNOWLEDGE_BASES_PER_AGENT = 8
DEFAULT_TOP_K = 8
MAX_TOP_K = 20
MAX_METADATA_KEYS = 16
MAX_METADATA_KEY_LENGTH = 64
MAX_METADATA_STRING_LENGTH = 256
MAX_FILTER_LIST_VALUES = 32
MAX_DOCUMENTS_PER_KNOWLEDGE_BASE = 2_000
MAX_CHUNKS_PER_KNOWLEDGE_BASE = 200_000
# Upper bound of one retrieve tool result (numbered sources plus section text).
MAX_RETRIEVE_RESULT_BYTES = 24 * 1024
METADATA_KEY_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"
# `ChunkingConfig.chunker_version`'s default. It stays 1 so every stored config and every config
# built without a version keeps chunker v1; new knowledge bases opt into v2 explicitly
# (`new_knowledge_base_chunking`, RAG P1 decision "Chunker v2").
CHUNKER_VERSION = 1
CHUNKER_VERSIONS: tuple[int, ...] = (1, 2)
LATEST_CHUNKER_VERSION = 2
# Chunker v2's short-section threshold for new knowledge bases (v1's default is 40).
CHUNKER_V2_MIN_TOKENS = 64

# The reserved key of a chunk's metadata: the platform's extraction record (`ChunkExtractMetadata`),
# an object next to the user's flat scalar metadata. Users can neither set it on a document nor
# filter on it (validate_metadata / validate_filter reject it).
RESERVED_METADATA_KEY = "eap"


def kb_ingest_partition_key(tenant_id: str, knowledge_base_id: str) -> str:
    """The DBOS queue partition key of an `eap.kb_ingest` run (RAG P1 follow-up): its tenant and
    knowledge base, `{tenant_id}:{knowledge_base_id}`. The API enqueues on the partitioned
    `eap-kb-ingest-fair` queue with this key, so a bulk import into one knowledge base no longer
    holds every slot ahead of an upload into another: each knowledge base is its own partition and
    the worker draws from the partitions in random order on every poll."""
    return f"{tenant_id}:{knowledge_base_id}"

_METADATA_KEY_RE = re.compile(METADATA_KEY_PATTERN)

# Extraction error codes (RAG P1) -> fixed, user-safe messages. The worker's sandbox and extractors
# fail with one of these (`eap_knowledge.ExtractionFailed(code)`), the API answers with
# `unsupported_content` (415, magic bytes) and `image_model_required` (422, part 2). They are reasons
# inside an ingest failure, not new `INGEST_FAILURE_CODES`: the run still fails `extraction_failed`
# and the document's error is `extraction_failed: <message>`. Messages never carry document text,
# paths or parser output.
EXTRACTION_ERROR_MESSAGES: dict[str, str] = {
    "extract_failed": "The document could not be extracted.",
    "extract_timeout": "Extraction took too long and was stopped.",
    "extract_memory": "Extraction ran out of memory.",
    "extract_limit": "The document is over an extraction limit (pages, nodes, nesting or image size).",
    "extract_output_too_large": "The extracted text is too large.",
    "document_encrypted": "The PDF is encrypted with a password.",
    "no_extractable_text": "No text could be extracted from the document.",
    # A PDF whose pages are images (scanned) and carry no text: failed, not empty, so a re-extract
    # with image extraction (P1 part 2) selects it.
    "no_text_layer": "Image-only PDF with no text layer: it needs image extraction (re-extract once it is available).",
    "unsupported_content": "The file's content does not match its declared type.",
    "image_model_required": "Images need an image-capable embedding model.",
}
EXTRACTION_ERROR_CODES: tuple[str, ...] = tuple(EXTRACTION_ERROR_MESSAGES)


def extraction_error_message(code: str) -> str:
    """The fixed message for an extraction error code; an unknown code reads as `extract_failed`."""
    return EXTRACTION_ERROR_MESSAGES.get(code, EXTRACTION_ERROR_MESSAGES["extract_failed"])


# Every failure code an ingest or reindex run (eap.kb_ingest) can end with: runs.error_code in
# run_failed, and the prefix of a document's / knowledge base's error "<code>: ...". The first four
# are the decisions table's ("Failure partway"); the rest are the worker's own.
INGEST_FAILURE_CODES: tuple[str, ...] = (
    "embedding_failed",
    "embedding_model_not_allowed",
    "embedding_dimension_mismatch",
    "ingest_budget_exceeded",
    "extraction_failed",  # unsupported MIME type, extraction error, no text, invalid chunking config
    "knowledge_base_limit_exceeded",  # chunk / document caps per knowledge base
    "ingest_cancelled",  # the document or knowledge base deleted, or the reindex cancelled, mid-run
    "ingest_not_converged",  # rounds without progress (generation switches kept racing the ingest)
    "internal_error",  # anything unclassified (a database error after retries, a bug)
)

MetadataScalar = Union[str, bool, int, float]
FilterValue = Union[MetadataScalar, tuple[MetadataScalar, ...]]


def approx_tokens(text: str) -> int:
    """`ceil(len(text) / 4)`, at least 1 for non-empty text and 0 for empty text."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _check_scalar(value: Any, where: str) -> MetadataScalar:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValueError(f"{where}: integer out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{where}: number must be finite")
        return value
    if isinstance(value, str):
        if len(value) > MAX_METADATA_STRING_LENGTH:
            raise ValueError(f"{where}: string longer than {MAX_METADATA_STRING_LENGTH} characters")
        return value
    raise ValueError(f"{where}: value must be a string, number or boolean")


def _check_key(key: Any, where: str = "metadata") -> str:
    if not isinstance(key, str) or not _METADATA_KEY_RE.fullmatch(key):
        raise ValueError(f"metadata key must match {METADATA_KEY_PATTERN}")
    if key == RESERVED_METADATA_KEY:
        raise ValueError(f"{where} key {RESERVED_METADATA_KEY!r} is reserved")
    return key


def validate_metadata(value: Any) -> dict[str, MetadataScalar]:
    """A document's metadata: a flat map of at most 16 string, number or boolean values.

    The key `eap` is reserved (RESERVED_METADATA_KEY)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    if len(value) > MAX_METADATA_KEYS:
        raise ValueError(f"metadata has more than {MAX_METADATA_KEYS} keys")
    return {_check_key(k): _check_scalar(v, f"metadata.{k}") for k, v in value.items()}


def validate_filter(value: Any) -> dict[str, FilterValue] | None:
    """A metadata filter: key -> scalar (equals) or list of scalars (any of). Empty -> None.

    The key `eap` is reserved: the platform's extraction record is never a filter target."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("filter must be an object")
    if not value:
        return None
    if len(value) > MAX_METADATA_KEYS:
        raise ValueError(f"filter has more than {MAX_METADATA_KEYS} keys")
    out: dict[str, FilterValue] = {}
    for key, item in value.items():
        _check_key(key, "filter")
        if isinstance(item, (list, tuple)):
            if not 1 <= len(item) <= MAX_FILTER_LIST_VALUES:
                raise ValueError(f"filter.{key}: a list needs 1 to {MAX_FILTER_LIST_VALUES} values")
            out[key] = tuple(_check_scalar(v, f"filter.{key}") for v in item)
        else:
            out[key] = _check_scalar(item, f"filter.{key}")
    return out


def _canonical_uuid(value: Any, what: str) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(uuid.UUID(value))
        except ValueError:
            pass
    raise ValueError(f"{what} must be a UUID")


class KnowledgeBaseRef(OmitNoneModel):
    """One knowledge base an agent retrieves from. `min_score` and `filter` are dropped while None."""

    id: str = Field(description="Knowledge base id (UUID) in the agent's tenant")
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K, description="Sections returned per search")
    min_score: float | None = Field(default=None, ge=0.0, le=1.0, description="Cosine score cut (1 - distance)")
    filter: dict[str, FilterValue] | None = Field(
        default=None, description="Metadata filter: key -> value (equals) or list (any of)"
    )

    @field_validator("id", mode="before")
    @classmethod
    def _uuid(cls, v: Any) -> str:
        return _canonical_uuid(v, "knowledge base id")

    @field_validator("filter", mode="before")
    @classmethod
    def _filter(cls, v: Any) -> Any:
        return validate_filter(v)


def check_knowledge_base_refs(refs: tuple[KnowledgeBaseRef, ...]) -> None:
    """At most 8, unique ids (AgentSpec validator)."""
    if len(refs) > MAX_KNOWLEDGE_BASES_PER_AGENT:
        raise ValueError(f"at most {MAX_KNOWLEDGE_BASES_PER_AGENT} knowledge bases per agent")
    seen: set[str] = set()
    for ref in refs:
        if ref.id in seen:
            raise ValueError(f"duplicate knowledge base: {ref.id}")
        seen.add(ref.id)


MAX_RST_CONFIG_KEYS = 64
MAX_RST_TAGS = 64
RST_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
_RST_NAME_RE = re.compile(RST_NAME_PATTERN)

class FrozenScalarMap(dict):
    """A read-only, hashable dict of scalars (`ExtractConfig.rst_config`), so a frozen
    `ExtractConfig`/`ChunkingConfig` stays hashable. It is still a `dict`: it compares equal to a
    plain dict and dumps (pydantic and `json.dumps`) as the same flat object. Mutation raises
    TypeError; pickling and copying rebuild it from its items."""

    __slots__ = ()

    def _readonly(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("rst_config is read-only")

    __setitem__ = __delitem__ = _readonly  # type: ignore[assignment]
    clear = pop = popitem = setdefault = update = _readonly  # type: ignore[assignment]
    __ior__ = _readonly  # type: ignore[assignment]

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(frozenset(self.items()))

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (dict(self),))


PdfHeadings = Literal["outline", "font", "pages"]
PdfImages = Literal["off", "embedded", "pages"]
MarkdownFlavor = Literal["plain", "doxygen"]


class ExtractConfig(OmitNoneModel):
    """Per-knowledge-base extraction options (`ChunkingConfig.extract`). Every field is omitted from
    dumps while None, and an empty `ExtractConfig` is dropped from its `ChunkingConfig`, so knowledge
    bases that set nothing keep their stored JSON.

    - `rst_config`: names for `ifconfig` conditions (`CONFIG_part_variant: "AM62X"`), a flat map of
      string, number or boolean values; names are Python-identifier-like.
    - `rst_tags`: tags for `only` conditions.
    - `pdf_headings`: `outline` (default; falls back to `font`), `font` or `pages`.
    - `pdf_images`: `off`, `embedded` or `pages` (part 2, P1.B2).
    - `markdown_flavor`: `doxygen` sends Markdown through the worker's Doxygen pre-pass; `plain` (or
      unset) keeps the API-extracted Markdown path.
    """

    rst_config: dict[str, MetadataScalar] | None = None
    rst_tags: tuple[str, ...] | None = None
    pdf_headings: PdfHeadings | None = None
    pdf_images: PdfImages | None = None
    markdown_flavor: MarkdownFlavor | None = None

    @field_validator("rst_config", mode="before")
    @classmethod
    def _rst_config(cls, v: Any) -> Any:
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("rst_config must be an object")
        if len(v) > MAX_RST_CONFIG_KEYS:
            raise ValueError(f"rst_config has more than {MAX_RST_CONFIG_KEYS} keys")
        out: dict[str, MetadataScalar] = {}
        for key, item in v.items():
            if not isinstance(key, str) or not _RST_NAME_RE.fullmatch(key):
                raise ValueError(f"rst_config key must match {RST_NAME_PATTERN}")
            out[key] = _check_scalar(item, f"rst_config.{key}")
        return out

    @field_validator("rst_config", mode="after")
    @classmethod
    def _rst_config_frozen(cls, v: dict[str, MetadataScalar] | None) -> FrozenScalarMap | None:
        return None if v is None else FrozenScalarMap(v)

    @field_validator("rst_tags", mode="before")
    @classmethod
    def _rst_tags(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (str, bytes)) or not isinstance(v, (list, tuple)):
            raise ValueError("rst_tags must be a list of names")
        if len(v) > MAX_RST_TAGS:
            raise ValueError(f"rst_tags has more than {MAX_RST_TAGS} tags")
        for tag in v:
            if not isinstance(tag, str) or not _RST_NAME_RE.fullmatch(tag):
                raise ValueError(f"rst_tags entries must match {RST_NAME_PATTERN}")
        return tuple(v)

    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in type(self).model_fields)


class ChunkingConfig(OmitNoneModel):
    """Chunking settings of one knowledge base (stored in `knowledge_bases.chunking`).

    Tokens are `approx_tokens` (chars / 4). `chunker_version` is the algorithm version the chunks
    were produced with (1 or 2, CHUNKER_VERSIONS); the default stays CHUNKER_VERSION (1), so a
    stored or partial config never moves to a new chunker silently. `extract` holds the extraction
    options (RAG P1); it is omitted while None and an empty one becomes None, so every config
    stored before it dumps byte for byte the same.
    """

    max_tokens: int = Field(default=512, ge=32, le=4096)
    overlap_tokens: int = Field(default=64, ge=0)
    min_tokens: int = Field(default=40, ge=0)
    chunker_version: int = Field(default=CHUNKER_VERSION, ge=min(CHUNKER_VERSIONS), le=max(CHUNKER_VERSIONS))
    extract: ExtractConfig | None = None

    @field_validator("extract", mode="after")
    @classmethod
    def _empty_extract(cls, v: ExtractConfig | None) -> ExtractConfig | None:
        return None if v is None or v.is_empty() else v

    @model_validator(mode="after")
    def _consistent(self) -> "ChunkingConfig":
        if self.overlap_tokens > self.max_tokens // 2:
            raise ValueError("overlap_tokens must be at most half of max_tokens")
        if self.min_tokens >= self.max_tokens:
            raise ValueError("min_tokens must be less than max_tokens")
        return self


def new_knowledge_base_chunking(**overrides: Any) -> ChunkingConfig:
    """The chunking config of a NEW knowledge base. `overrides` are the create request's own fields
    and always win; existing knowledge bases keep their stored config.

    - `chunker_version` defaults to LATEST_CHUNKER_VERSION (2).
    - `min_tokens`, when not given: for v2, `min(64, max_tokens - 1)` (64 unless `max_tokens` is 64
      or less, so a small `max_tokens` stays valid); for v1, v1's own default (40).
    """
    values: dict[str, Any] = {"chunker_version": LATEST_CHUNKER_VERSION, **overrides}
    if "min_tokens" not in overrides and values["chunker_version"] == LATEST_CHUNKER_VERSION:
        max_tokens = values.get("max_tokens", ChunkingConfig.model_fields["max_tokens"].default)
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
            values["min_tokens"] = min(CHUNKER_V2_MIN_TOKENS, max_tokens - 1)
    return ChunkingConfig(**values)


# Extractor ids, as recorded in `metadata.eap.extractor` and the `extracted` event. Their current
# versions are `eap_knowledge.EXTRACTOR_VERSIONS`.
ExtractorId = Literal["rst", "pdf", "yaml", "json", "markdown-doxygen", "code"]
EXTRACTOR_IDS: tuple[str, ...] = ("rst", "pdf", "yaml", "json", "markdown-doxygen", "code")
MAX_CONDITION_CHARS = 200
MAX_IMAGE_ONLY_PAGES_LISTED = 200


class ChunkExtractMetadata(OmitNoneModel):
    """A worker-extracted chunk's `metadata["eap"]` (RESERVED_METADATA_KEY). None fields are omitted.

    `page`/`page_end` are 1-based PDF pages; `condition` is an unevaluated rst `ifconfig`/`only`
    condition (at most 200 characters)."""

    extractor: ExtractorId
    extractor_version: int = Field(ge=1)
    page: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    condition: str | None = Field(default=None, max_length=MAX_CONDITION_CHARS)
    # Source code (extractor `code`): where the chunk sits in its file.
    language: str | None = Field(default=None, max_length=32)
    path: str | None = Field(default=None, max_length=1024)
    symbol: str | None = Field(default=None, max_length=256)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    kind: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _pages(self) -> "ChunkExtractMetadata":
        if self.page_end is not None and (self.page is None or self.page_end < self.page):
            raise ValueError("page_end needs page and must not be before it")
        return self


class ExtractedEventData(OmitNoneModel):
    """The `extracted` run event's data (key `extract:{doc_version}`): counts and ids, never text.

    `image_only_pages` lists at most 200 1-based page numbers; `conditions` counts kept, unevaluated
    rst conditions; `warnings` counts dropped `include`/`literalinclude` directives and the like.
    `empty_reason` (`no_extractable_text`) marks a version of a non-PDF format that extracted no
    text (a toctree- or include-only rst page, a YAML file with only a licence comment, an empty
    JSON or Doxygen page) and is active with 0 chunks: an empty document, not a failure. A PDF
    never gets it: one with no text fails (`no_text_layer` when it has image-only pages, else
    `no_extractable_text`)."""

    extractor: ExtractorId
    extractor_version: int = Field(ge=1)
    sections: int = Field(ge=0)
    chars: int = Field(ge=0)
    pages: int | None = Field(default=None, ge=0)
    # The producer truncates to the first MAX_IMAGE_ONLY_PAGES_LISTED pages.
    image_only_pages: tuple[Annotated[int, Field(ge=1)], ...] | None = Field(
        default=None, max_length=MAX_IMAGE_ONLY_PAGES_LISTED
    )
    conditions: int | None = Field(default=None, ge=0)
    warnings: int | None = Field(default=None, ge=0)
    empty_reason: Literal["no_extractable_text"] | None = None


class KnowledgeBaseNotFound(LookupError):
    """A knowledge base id does not exist in the tenant (deleted, or never there)."""

    code = "knowledge_base_not_found"

    def __init__(self, kb_id: str) -> None:
        self.kb_id = str(kb_id)
        super().__init__(self.kb_id)

    def __str__(self) -> str:
        return f"knowledge base {self.kb_id} no longer exists"


class IngestBudgetExceeded(RuntimeError):
    """An ingest run's un-cached tokens exceed EAP_KB_MAX_INGEST_TOKENS; no provider call is made."""

    code = "ingest_budget_exceeded"

    def __init__(self, estimated_tokens: int, limit: int) -> None:
        self.estimated_tokens = estimated_tokens
        self.limit = limit
        super().__init__(estimated_tokens, limit)

    def __str__(self) -> str:
        return (
            f"ingest needs about {self.estimated_tokens} embedding tokens, over the limit of {self.limit} "
            "(EAP_KB_MAX_INGEST_TOKENS)"
        )


__all__ = [
    "CHUNKER_V2_MIN_TOKENS",
    "CHUNKER_VERSION",
    "CHUNKER_VERSIONS",
    "ChunkExtractMetadata",
    "ChunkingConfig",
    "DEFAULT_TOP_K",
    "EXTRACTION_ERROR_CODES",
    "EXTRACTION_ERROR_MESSAGES",
    "EXTRACTOR_IDS",
    "ExtractConfig",
    "ExtractedEventData",
    "ExtractorId",
    "FilterValue",
    "FrozenScalarMap",
    "INGEST_FAILURE_CODES",
    "IngestBudgetExceeded",
    "KnowledgeBaseNotFound",
    "KnowledgeBaseRef",
    "LATEST_CHUNKER_VERSION",
    "MAX_CHUNKS_PER_KNOWLEDGE_BASE",
    "MAX_CONDITION_CHARS",
    "MAX_IMAGE_ONLY_PAGES_LISTED",
    "MAX_DOCUMENTS_PER_KNOWLEDGE_BASE",
    "MAX_FILTER_LIST_VALUES",
    "MAX_KNOWLEDGE_BASES_PER_AGENT",
    "MAX_METADATA_KEYS",
    "MAX_METADATA_KEY_LENGTH",
    "MAX_METADATA_STRING_LENGTH",
    "MAX_RETRIEVE_RESULT_BYTES",
    "MAX_RST_CONFIG_KEYS",
    "MAX_RST_TAGS",
    "MAX_TOP_K",
    "METADATA_KEY_PATTERN",
    "MarkdownFlavor",
    "MetadataScalar",
    "PdfHeadings",
    "PdfImages",
    "RESERVED_METADATA_KEY",
    "RST_NAME_PATTERN",
    "approx_tokens",
    "check_knowledge_base_refs",
    "extraction_error_message",
    "new_knowledge_base_chunking",
    "validate_filter",
    "validate_metadata",
]
