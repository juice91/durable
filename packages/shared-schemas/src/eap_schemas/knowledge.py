"""Knowledge base contracts (RAG and memory milestone, task RM.2).

- `KnowledgeBaseRef`: an agent spec's reference to a tenant knowledge base, with retrieval
  settings. Referenced by id (like MCP servers), never snapshotted.
- `ChunkingConfig`: the per-knowledge-base chunking settings `eap_knowledge` (RM.3) consumes and
  `knowledge_bases.chunking` (RM.4) stores.
- Metadata and filter value rules shared by the document upload (RM.7) and the spec filter.
- `approx_tokens`: the platform's `ceil(chars / 4)` token estimate (chunking, history limits,
  cost cap), never 0 for non-empty text.
- Error types, pickle-safe (raised inside DBOS steps).

No I/O here.
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
CHUNKER_VERSION = 1

_METADATA_KEY_RE = re.compile(METADATA_KEY_PATTERN)

# Ingest run failure codes (decisions table, "Failure partway").
INGEST_FAILURE_CODES: tuple[str, ...] = (
    "embedding_failed",
    "embedding_model_not_allowed",
    "embedding_dimension_mismatch",
    "ingest_budget_exceeded",
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


def _check_key(key: Any) -> str:
    if not isinstance(key, str) or not _METADATA_KEY_RE.fullmatch(key):
        raise ValueError(f"metadata key must match {METADATA_KEY_PATTERN}")
    return key


def validate_metadata(value: Any) -> dict[str, MetadataScalar]:
    """A document's metadata: a flat map of at most 16 string, number or boolean values."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    if len(value) > MAX_METADATA_KEYS:
        raise ValueError(f"metadata has more than {MAX_METADATA_KEYS} keys")
    return {_check_key(k): _check_scalar(v, f"metadata.{k}") for k, v in value.items()}


def validate_filter(value: Any) -> dict[str, FilterValue] | None:
    """A metadata filter: key -> scalar (equals) or list of scalars (any of). Empty -> None."""
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
        _check_key(key)
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


class ChunkingConfig(BaseModel):
    """Chunking settings of one knowledge base (stored in `knowledge_bases.chunking`).

    Tokens are `approx_tokens` (chars / 4). `chunker_version` is the algorithm version the chunks
    were produced with; `eap_knowledge` bumps CHUNKER_VERSION when its output changes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tokens: int = Field(default=512, ge=32, le=4096)
    overlap_tokens: int = Field(default=64, ge=0)
    min_tokens: int = Field(default=40, ge=0)
    chunker_version: int = Field(default=CHUNKER_VERSION, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> "ChunkingConfig":
        if self.overlap_tokens > self.max_tokens // 2:
            raise ValueError("overlap_tokens must be at most half of max_tokens")
        if self.min_tokens >= self.max_tokens:
            raise ValueError("min_tokens must be less than max_tokens")
        return self


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
    "CHUNKER_VERSION",
    "ChunkingConfig",
    "DEFAULT_TOP_K",
    "FilterValue",
    "INGEST_FAILURE_CODES",
    "IngestBudgetExceeded",
    "KnowledgeBaseNotFound",
    "KnowledgeBaseRef",
    "MAX_CHUNKS_PER_KNOWLEDGE_BASE",
    "MAX_DOCUMENTS_PER_KNOWLEDGE_BASE",
    "MAX_FILTER_LIST_VALUES",
    "MAX_KNOWLEDGE_BASES_PER_AGENT",
    "MAX_METADATA_KEYS",
    "MAX_METADATA_KEY_LENGTH",
    "MAX_METADATA_STRING_LENGTH",
    "MAX_RETRIEVE_RESULT_BYTES",
    "MAX_TOP_K",
    "METADATA_KEY_PATTERN",
    "MetadataScalar",
    "approx_tokens",
    "check_knowledge_base_refs",
    "validate_filter",
    "validate_metadata",
]
