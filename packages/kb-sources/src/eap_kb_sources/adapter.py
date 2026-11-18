"""The adapter boundary: everything provider-specific sits behind `SourceAdapter`.

A sync is a series of bounded SEGMENTS (design: native DBOS workflow sequence). The worker's segment
step resolves the connection's secrets, hands the adapter a `SyncSegmentRequest` (opaque cursor,
`page_budget` pages or the targeted parts, `call_budget` provider calls) and a `SourceStoreFacade`, and
gets a `SyncSegmentResult` back: the next cursor, whether the sync is complete and counters, never
raw records. The adapter writes what it fetched through the facade (idempotent upserts, hash-guarded)
and hands document bytes to it inside the same step; bytes never leave the step.

`cursor` is adapter-owned JSON of at most 8 KiB (the daily quota counts live in it, see budget.py).
Secrets: the adapter calls `secrets.resolve("secret:<name>")` inside the segment and never returns,
logs or stores the value (an OAuth token is call-local or a process-local TTL cache replay never
depends on).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import quote

Mode = Literal["targeted", "incremental", "full"]
MODES: tuple[str, ...] = ("targeted", "incremental", "full")
MAX_CURSOR_BYTES = 8 * 1024
MAX_TARGETED_PARTS = 100
MAX_SEGMENT_ERRORS = 50
MAX_DOCUMENT_KEY_CHARS = 256
DEFAULT_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024  # the kb_ingest PDF cap
DEFAULT_DOCUMENT_TYPES = ("datasheet",)
# Relationships with one document per product (the key has no url hash); the others can have several.
SINGLE_DOCUMENT_TYPES = frozenset({"datasheet"})


@dataclass(frozen=True)
class ConnectionSnapshot:
    """A connection as an adapter sees it: its non-secret config and the secret REFS (never values)."""

    tenant_id: str
    connection_id: str
    name: str
    provider: str
    config: Mapping[str, Any] = field(default_factory=dict)
    secret_refs: Mapping[str, str] = field(default_factory=dict)
    base_url: str | None = None


@runtime_checkable
class SecretResolver(Protocol):
    def resolve(self, ref: str) -> str:
        """The tenant's value of `secret:<name>` (a bare name is accepted). SourceError
        `secret_missing` when there is none. Call inside the step that uses the value, nowhere else."""
        ...


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    error_code: str | None = None


@dataclass(frozen=True)
class DocumentPolicy:
    types: tuple[str, ...] = DEFAULT_DOCUMENT_TYPES
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES

    @classmethod
    def from_json(cls, value: Any) -> "DocumentPolicy":
        data = value if isinstance(value, Mapping) else {}
        types = data.get("types")
        kept = tuple(t for t in types if isinstance(t, str) and t) if isinstance(types, (list, tuple)) else ()
        max_bytes = data.get("max_bytes")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            max_bytes = DEFAULT_MAX_DOCUMENT_BYTES
        return cls(types=kept or DEFAULT_DOCUMENT_TYPES, max_bytes=min(max_bytes, DEFAULT_MAX_DOCUMENT_BYTES))


@dataclass(frozen=True)
class SyncSegmentRequest:
    source_id: str
    mode: str
    cursor: dict[str, Any]
    part_numbers: tuple[str, ...] = ()  # targeted only, the segment's slice (<= 20 of <= 100)
    page_budget: int = 10  # pages (10-25) a full or incremental segment walks
    call_budget: int = 200  # provider calls a segment may make
    config: Mapping[str, Any] = field(default_factory=dict)  # the source's non-secret config
    document_policy: DocumentPolicy = field(default_factory=DocumentPolicy)


@dataclass
class SegmentCounters:
    discovered: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    documents_discovered: int = 0
    documents_queued: int = 0
    failed: int = 0

    FIELDS = (
        "discovered", "inserted", "updated", "unchanged", "documents_discovered", "documents_queued", "failed",
    )

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.FIELDS}

    def add(self, other: Mapping[str, int]) -> None:
        for name in self.FIELDS:
            setattr(self, name, getattr(self, name) + int(other.get(name, 0)))


@dataclass
class SyncSegmentResult:
    """`next_cursor`: where the next segment starts (or, when `complete`, where the next sync starts,
    e.g. a changed-since marker); None stores nothing."""

    next_cursor: dict[str, Any] | None
    complete: bool
    counters: SegmentCounters = field(default_factory=SegmentCounters)
    errors: list[dict[str, str]] = field(default_factory=list)  # <= 50 x {external_id?, code, message <= 200}
    calls_used: int = 0
    # A complete FULL walk makes the worker mark every record the walk did not observe `removed` (a record
    # is observed when any upsert of its external id ran during the walk, so an adapter must observe every
    # key its listing names, even when the detail fetch failed). `sweep_ok=False` vetoes that sweep, e.g.
    # when the listing looked truncated.
    sweep_ok: bool = True
    source_revision: str | None = None  # what the walk saw of the provider (stored on a complete walk)
    budget: dict[str, Any] | None = None  # {"used": {bucket: n}, "remaining": {bucket: n}} after the segment


@dataclass(frozen=True)
class DocumentState:
    """What the ledger knows about a document of a product: skip the download when the provider's
    ETag / Last-Modified are unchanged (or, without them, compare the hash after downloading)."""

    content_hash: str | None
    etag: str | None = None
    last_modified: str | None = None
    status: str | None = None
    source_revision: str | None = None  # what the adapter recorded as the provider's version (a git blob sha)


@dataclass(frozen=True)
class LedgerDocument:
    """One ledger row of an entity's documents (for an adapter that must find what disappeared)."""

    key: str
    source_url: str
    source_revision: str | None
    status: str


@dataclass(frozen=True)
class EntityRef:
    """A stored entity of the source (a product): what document calls name."""

    entity_id: str
    external_id: str
    entity_type: str = "product"


@runtime_checkable
class SourceStoreFacade(Protocol):
    """The adapter's writes: idempotent, tenant- and source-scoped by construction."""

    def upsert_record(
        self, external_id: str, record_type: str, payload: dict[str, Any], *, source_updated_at: datetime | None = None
    ) -> Literal["inserted", "updated", "unchanged"]:
        """The provider's raw JSON, by hash (`record_type`: product, quality, parametrics, ...)."""
        ...

    def upsert_entity(
        self,
        external_id: str,
        *,
        entity_type: str = "product",
        lifecycle_status: str | None = None,
        attributes: dict[str, Any] | None = None,
        identifiers: Sequence[tuple[str, str, bool]] = (),
        parameters: Sequence[dict[str, Any]] = (),
    ) -> EntityRef:
        """The normalised entity (`canonical_name` = `external_id`), its `(scheme, value, is_primary)`
        identifiers and its parameter rows (`kb_entity_parameters` fields), replaced as given."""
        ...

    def record_unchanged(self, external_id: str, record_type: str, payload: dict[str, Any]) -> bool:
        """Read-only: an ACTIVE record with exactly this payload (by hash) is already stored. Lets an
        incremental walk skip the detail calls of an item whose listing entry did not change."""
        ...

    def document_state(
        self, entity: EntityRef, relationship: str, url: str, *, key: str | None = None
    ) -> DocumentState | None:
        """`key`: the adapter's own document key (<= 256 characters) instead of the derived one, for a
        provider whose documents are named by a stable path while their URL changes with every revision."""
        ...

    def submit_document(
        self,
        entity: EntityRef,
        relationship: str,
        url: str,
        data: bytes,
        *,
        mime_type: str | None = None,
        title: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        source_revision: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        key: str | None = None,
    ) -> Literal["queued", "unchanged"]:
        """Store the document in the knowledge base and enqueue its ingest through the shared
        hand-off. `metadata`: flat scalar keys merged under the platform's own stamps (which win); with
        the stamps at most 16 keys. `key`: see `document_state`. `unchanged`: the same bytes and metadata
        are already there (no new version, no run).
        Raises SourceError (`response_too_large`, `invalid_document`, `kb_capacity`, `enqueue_failed`)."""
        ...


@runtime_checkable
class SourceAdapter(Protocol):
    provider: str  # "acme_catalog", "fake"
    kind: str  # "product_catalog"

    def validate_config(self, config: Mapping[str, Any]) -> None:
        """Raise ValueError (a short, secret-free message) for a config the adapter cannot use."""
        ...

    async def check_connection(self, connection: ConnectionSnapshot, secrets: SecretResolver) -> ConnectionCheck: ...

    async def sync_segment(
        self,
        request: SyncSegmentRequest,
        connection: ConnectionSnapshot,
        secrets: SecretResolver,
        store: SourceStoreFacade,
    ) -> SyncSegmentResult: ...


def secret_name(ref: str) -> str:
    """`secret:<name>` -> `<name>`; a bare name stays as it is."""
    return ref[len("secret:"):] if ref.startswith("secret:") else ref


_SAFE = re.compile(r"[^A-Za-z0-9._~-]")


def document_key(source_id: str, entity_type: str, external_id: str, relationship: str, url: str | None = None) -> str:
    """`source:<source_id>:<entity_type>:<external_id>:<relationship>`; a relationship that can have
    several documents (not a datasheet) appends `:` + sha1(url)[:8]. One URL path segment (the id is
    percent-encoded), at most 256 characters (a long id is cut and gets its own hash)."""
    ext = quote(external_id, safe="")
    ext = _SAFE.sub(lambda m: "%%%02X" % ord(m.group()), ext)
    tail = ""
    if relationship not in SINGLE_DOCUMENT_TYPES:
        tail = ":" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:8]
    head = f"source:{source_id}:{entity_type}:"
    suffix = f":{relationship}{tail}"
    room = MAX_DOCUMENT_KEY_CHARS - len(head) - len(suffix)
    if len(ext) > room:
        digest = hashlib.sha1(external_id.encode("utf-8")).hexdigest()[:8]
        ext = ext[: max(1, room - 9)] + "~" + digest
    return head + ext + suffix
