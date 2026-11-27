"""Knowledge bases, their documents, uploads, ingest and reindex runs: the business logic behind
`eap_api.routes.knowledge` (RAG and memory milestone, RM.7).

Transport-independent: a method takes plain values and the principal's tenant id, returns plain data and
refuses with the existing `DefinitionError` family (so every body stays what the routes always answered:
`{"detail", ...}` with no `code` unless one was set) or a `ServiceError`. Collaborators (the knowledge store,
the run store and the run launcher) come through the constructor."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from eap_api.ports import RunLauncher
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.schemas import DocumentPut, document_key_problem
from eap_api.services.refusals import refusal
from eap_knowledge import ExtractionFailed, ExtractLimits, UnsupportedMimeType, extract
from eap_knowledge.formats import (
    EXTRACTOR_VERSIONS,
    FORMAT_SPECS,
    PDF_SIGNATURE_WINDOW,
    FormatSpec,
    detect_binary_type,
    extractor_for,
    format_by_name,
    format_for_mime,
    has_pdf_signature,
)
from eap_observability.context import current_trace_context
from eap_observability.redaction import log_failure
from eap_pricing import (
    COST_QUANTUM,
    OVERRIDE_ONLY_PROVIDERS,
    PRICES_FILE_ENV,
    SOURCE_OVERRIDE,
    InvalidPriceFile,
    Usage,
    format_cost,
    load_overrides,
    override_cost,
    override_period,
)
from eap_run_store import ENQUEUE_FAILED_ERROR, TERMINAL_RUN_STATUSES, RunIdConflict, RunStore
from eap_run_store import kb_ingest_handoff as handoff
from eap_run_store.cursor import MAX_CURSOR_LENGTH  # noqa: F401  (re-exported for the route)
from eap_run_store.definition_errors import DefinitionError
from eap_run_store.kb_ingest_handoff import (  # noqa: F401  (re-exported: tests and routes use these names)
    EMBED_SPLIT_KEY,
    ENQUEUE_FAILED_DETAIL,
    ORPHAN_AFTER_SECONDS,
    ingest_input,
    reindex_input,
)
from eap_run_store.knowledge import (
    DocumentNotFound,
    IngestRunConflict,
    content_hash,
    KnowledgeBaseLimitExceeded,
    KnowledgeBaseNameConflict,
    KnowledgeStore,
    ReindexInProgress,
)
from eap_schemas import KnowledgeBaseNotFound
from eap_schemas.knowledge import (
    EXTRACTION_ERROR_MESSAGES,
    INGEST_FAILURE_CODES,
    ChunkingConfig,
    ExtractConfig,
)
from eap_schemas.embedding_policy import EmbeddingModel, EmbeddingModelNotAllowed, EmbeddingModelPolicy
from eap_schemas.tools import KNOWLEDGE_SEARCH_TOOL_ID, KnowledgeSearchInput

log = logging.getLogger("eap_api")

KB_MAX_DOCUMENT_BYTES_ENV = "EAP_KB_MAX_DOCUMENT_BYTES"
DEFAULT_KB_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
KB_MAX_INGEST_TOKENS_ENV = "EAP_KB_MAX_INGEST_TOKENS"
DEFAULT_KB_MAX_INGEST_TOKENS = 2_000_000
DEADLOCK_SQLSTATE = "40P01"

KB_NOT_FOUND = "Knowledge base not found"
DOCUMENT_NOT_FOUND = "Document not found"


def too_large_detail(limit: int) -> str:
    return f"request body too large (max {limit} bytes)"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def max_document_bytes() -> int:
    """EAP_KB_MAX_DOCUMENT_BYTES: the upload route's request body cap (default 5 MiB)."""
    return _env_int(KB_MAX_DOCUMENT_BYTES_ENV, DEFAULT_KB_MAX_DOCUMENT_BYTES)


def max_ingest_tokens() -> int:
    """EAP_KB_MAX_INGEST_TOKENS: the per-run embedding budget the worker enforces (default 2,000,000)."""
    return _env_int(KB_MAX_INGEST_TOKENS_ENV, DEFAULT_KB_MAX_INGEST_TOKENS)


BINARY_UPLOAD_CONCURRENCY_ENV = "EAP_KB_BINARY_UPLOAD_CONCURRENCY"
DEFAULT_BINARY_UPLOAD_CONCURRENCY = 4
MAX_BINARY_UPLOAD_CONCURRENCY = 64
# How long a binary upload waits for a slot before answering 503.
BINARY_UPLOAD_WAIT_SECONDS = 30.0


def binary_upload_concurrency() -> int:
    """EAP_KB_BINARY_UPLOAD_CONCURRENCY: binary (base64) uploads this API process decodes and
    stores at once (default 4, 1..64). Each holds its decoded bytes (up to EAP_KB_MAX_BINARY_BYTES)
    while it is checked and hashed, so this bounds that memory per process."""
    value = _env_int(BINARY_UPLOAD_CONCURRENCY_ENV, DEFAULT_BINARY_UPLOAD_CONCURRENCY)
    if value > MAX_BINARY_UPLOAD_CONCURRENCY:
        raise ValueError(
            f"{BINARY_UPLOAD_CONCURRENCY_ENV} must be an integer from 1 to {MAX_BINARY_UPLOAD_CONCURRENCY}, got {value}"
        )
    return value


_binary_slots_lock = threading.Lock()
_binary_slots: tuple[int, threading.BoundedSemaphore] | None = None


def _binary_upload_slots() -> threading.BoundedSemaphore:
    """The process-wide semaphore for binary uploads, rebuilt when the configured size changes."""
    global _binary_slots
    try:
        size = binary_upload_concurrency()
    except ValueError:  # checked at startup; the environment changed under the process
        size = DEFAULT_BINARY_UPLOAD_CONCURRENCY
    with _binary_slots_lock:
        if _binary_slots is None or _binary_slots[0] != size:
            _binary_slots = (size, threading.BoundedSemaphore(size))
        return _binary_slots[1]


@contextmanager
def binary_upload_slot(spec: FormatSpec | None) -> Iterator[None]:
    """Hold one binary-upload slot for a binary format (no-op otherwise). 503 after
    BINARY_UPLOAD_WAIT_SECONDS without one."""
    if spec is None or not spec.binary:
        yield
        return
    slots = _binary_upload_slots()
    if not slots.acquire(timeout=BINARY_UPLOAD_WAIT_SECONDS):
        raise refusal(503, "too many binary uploads in progress; retry shortly",
        )
    try:
        yield
    finally:
        slots.release()


def extract_limits() -> ExtractLimits:
    """The RAG P1 upload caps (EAP_KB_MAX_BINARY_BYTES, EAP_KB_MAX_STRUCTURED_BYTES, ...), read per
    request like EAP_KB_MAX_DOCUMENT_BYTES. ValueError for a malformed value (checked at startup)."""
    return ExtractLimits.from_env(os.environ)


def upload_guard_bytes() -> int:
    """The upload route's request-body guard: `max(text cap, ceil(binary cap × 4/3) + 64 KiB)`, so a
    base64 PDF at the binary cap fits. Only a body with `content_base64` may stream past the text
    cap (EAP_KB_MAX_DOCUMENT_BYTES, `_is_base64_upload`); any other is refused there, before it is
    buffered further, and the route checks the text cap on the body again. Every format's
    decoded-size cap is checked after decoding."""
    try:
        limits = extract_limits()
    except ValueError:  # checked at startup; the environment changed under the process
        limits = ExtractLimits()
    return limits.upload_guard_bytes(max_document_bytes())


def check_knowledge_configuration() -> None:
    """Fail startup on a malformed EAP_ALLOWED_EMBEDDING_MODELS, EAP_EMBEDDING_QUERY_INSTRUCTIONS,
    EAP_KB_MAX_DOCUMENT_BYTES, EAP_KB_MAX_INGEST_TOKENS, EAP_KB_EXACT_SEARCH_MAX_CHUNKS or the RAG P1
    extraction limits (EAP_KB_MAX_BINARY_BYTES and the others of ExtractLimits) (ValueError), not
    at the first request."""
    from eap_run_store.knowledge import check_exact_search_max_chunks

    EmbeddingModelPolicy.from_env()
    max_document_bytes()
    max_ingest_tokens()
    check_exact_search_max_chunks()
    extract_limits()
    binary_upload_concurrency()


def _policy() -> EmbeddingModelPolicy:
    try:
        return EmbeddingModelPolicy.from_env()
    except ValueError as exc:  # checked at startup; the environment changed under the process
        log_failure(log, "embedding model allowlist invalid", exc, code="embedding_policy_invalid", level=logging.ERROR)
        raise refusal(503, "the embedding model allowlist (EAP_ALLOWED_EMBEDDING_MODELS) is misconfigured",
        )


def _resolve_model(model_id: str) -> EmbeddingModel:
    """The allowed embedding model, or 422 with the policy's message."""
    try:
        return _policy().resolve(model_id)
    except EmbeddingModelNotAllowed as exc:
        raise refusal(422, str(exc))


def _kb_or_404(store: KnowledgeStore, tenant_id: str, kb_id: str) -> dict[str, Any]:
    kb = store.get_knowledge_base(tenant_id, kb_id)
    if kb is None:
        raise refusal(404, KB_NOT_FOUND)
    return kb


def _check_document_key(document_key: str) -> None:
    problem = document_key_problem(document_key)
    if problem is not None:
        raise refusal(422, problem)


_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.S)


def _has_text(document: Any) -> bool:
    """Whether the extracted document has any text besides HTML comments. eap_knowledge drops
    script, style and nav, but keeps an HTML comment in Markdown as section text (RM.3)."""
    if document.extracted_chars == 0:
        return False
    return any(
        _HTML_COMMENT.sub("", f"{section.heading}\n{section.text}").strip() for section in document.sections
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_same_ingest_input = handoff.same_ingest_input

# ------------------------------------------------------------------ formats and uploads (RAG P1.A4)


class UploadRejected(Exception):
    """An upload refused before anything is written: the route answers `status_code` with `body`."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(status_code, body)

    def as_refusal(self) -> DefinitionError:
        return refusal(self.status_code, self.body.get("detail"), **{k: v for k, v in self.body.items() if k != "detail"})


def _reject(status_code: int, detail: str, **extra: Any) -> UploadRejected:
    return UploadRejected(status_code, {"detail": detail, **extra})


def _unsupported_content(declared: str, detected: str | None, reason: str | None = None) -> UploadRejected:
    """415 `unsupported_content`: the bytes are not what `declared` says. `detected_type` names the
    binary signature found, when one was."""
    base = EXTRACTION_ERROR_MESSAGES["unsupported_content"]
    if detected is not None:
        detail = f"{base} It looks like {detected}, not {declared}."
    else:
        detail = f"{base} {reason or ''}".strip()
    return _reject(
        415, detail, code="unsupported_content", detected_type=detected
    )


def _text_signature(content: str) -> str | None:
    """The binary type a TEXT upload really is (signatures at offset 0), or None. The prefix is
    checked as UTF-8 and, when every character is below U+0100, as the bytes those code points name
    (a file read as Latin-1 keeps its signature bytes that way)."""
    head = content[:16]
    detected = detect_binary_type(head.encode("utf-8"))
    if detected is None and all(ord(ch) < 256 for ch in head):
        detected = detect_binary_type(head.encode("latin-1"))
    return detected


def _decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise _reject(
            422,
            "content_base64 is not valid base64 (the standard alphabet with padding, no line breaks)",
        ) from None


def _format_or_422(mime_type: str) -> FormatSpec:
    try:
        return format_for_mime(mime_type)
    except UnsupportedMimeType as exc:
        raise _reject(422, str(exc)) from None


def _limits_or_503() -> ExtractLimits:
    try:
        return extract_limits()
    except ValueError as exc:
        log_failure(log, "extraction limits invalid", exc, code="extract_limits_invalid", level=logging.ERROR)
        raise refusal(503, "the knowledge base upload limits (EAP_KB_MAX_*) are misconfigured",
        ) from None


class PreparedUpload:
    """What the upload stores: `content` (UTF-8 text, or canonical base64 for a binary type), the
    canonical `mime_type`, and `digest` (sha256 of the decoded bytes for a binary type, else None:
    the store hashes the text)."""

    __slots__ = ("content", "digest", "mime_type", "spec")

    def __init__(self, spec: FormatSpec, content: str, digest: str | None) -> None:
        self.spec = spec
        self.mime_type = spec.mime_type
        self.content = content
        self.digest = digest

    @property
    def content_hash(self) -> str:
        return self.digest if self.digest is not None else content_hash(self.content)


def prepare_upload(body: DocumentPut, body_size: int) -> PreparedUpload:
    """Check an upload against the format registry, its caps and its magic bytes, before anything is
    written. Raises UploadRejected:

    - 422: an unsupported MIME type; `content` for a binary type, or `content_base64` for a text
      type (unless its bytes are a known binary type: then 415); invalid or empty base64; empty text.
    - 413: a text upload whose request body is over EAP_KB_MAX_DOCUMENT_BYTES (the guard's own
      message); YAML or JSON over EAP_KB_MAX_STRUCTURED_BYTES; a decoded binary over
      EAP_KB_MAX_BINARY_BYTES.
    - 415 (`code: unsupported_content`, `detected_type`): a PDF without `%PDF-` in its first 1,024
      bytes, or text that starts with a known binary signature."""
    spec = _format_or_422(body.mime_type)
    limits = _limits_or_503()
    text_cap = max_document_bytes()
    declared = spec.mime_type
    if spec.binary:
        if body.content_base64 is None:
            raise _reject(
                422,
                f"unsupported document mime type {body.mime_type!r} for text content: {declared} documents are"
                " uploaded as content_base64",
            )
        data = _decode_base64(body.content_base64)
        if not data:
            raise _reject(422, "content_base64 is empty")
        cap = limits.max_bytes_for(spec.cap_class, text_cap)
        if len(data) > cap:
            raise _reject(
                413,
                f"the document is too large ({len(data)} bytes; max {cap} bytes for {declared})",
            )
        if spec.format == "pdf" and not has_pdf_signature(data):
            detected = detect_binary_type(data)
            raise _unsupported_content(
                declared,
                detected,
                f"It has no %PDF- signature in its first {PDF_SIGNATURE_WINDOW:,} bytes, so it is not a PDF.",
            )
        # Stored as sent (validated strictly above, so it decodes to exactly `data`): no second
        # encoded copy of up to ~33 MiB.
        digest = hashlib.sha256(data).hexdigest()
        del data
        return PreparedUpload(spec, body.content_base64, digest)

    if body_size > text_cap:  # text keeps its cap on the request body, as before RAG P1
        raise _reject(413, too_large_detail(text_cap))
    if body.content_base64 is not None:
        data = _decode_base64(body.content_base64)
        detected = detect_binary_type(data[:16])
        if detected is not None:
            raise _unsupported_content(declared, detected)
        raise _reject(
            422,
            f"{declared} documents are uploaded as content (UTF-8 text), not content_base64",
        )
    content = body.content or ""
    if not content.strip():
        raise _reject(422, "content is empty")
    cap = limits.max_bytes_for(spec.cap_class, text_cap)
    size = len(content.encode("utf-8"))
    if size > cap:
        raise _reject(
            413, f"the document is too large ({size} bytes; max {cap} bytes for {declared})"
        )
    detected = _text_signature(content)
    if detected is not None:
        raise _unsupported_content(declared, detected)
    return PreparedUpload(spec, content, None)


def kb_extract_config(kb: dict[str, Any]) -> ExtractConfig | None:
    """The knowledge base's `chunking.extract` (None when unset or unreadable)."""
    raw = kb.get("chunking")
    if not isinstance(raw, dict) or not raw.get("extract"):
        return None
    try:
        return ChunkingConfig.model_validate(raw).extract
    except ValidationError:
        return None


def _spec_or_none(mime_type: str | None) -> FormatSpec | None:
    try:
        return format_for_mime(mime_type or "")
    except UnsupportedMimeType:
        return None


_EXTRACTION_CODE_BY_MESSAGE = {message: code for code, message in EXTRACTION_ERROR_MESSAGES.items()}


def document_error_codes(error: str | None) -> tuple[str | None, str | None]:
    """(error_code, error_reason) of a document's `error` ("<code>: <message>"): the ingest failure
    code when it is one of INGEST_FAILURE_CODES, and the extraction code whose fixed message the
    rest is (the worker writes "extraction_failed: <EXTRACTION_ERROR_MESSAGES[code]>")."""
    if not error:
        return None, None
    code, sep, rest = error.partition(":")
    code = code.strip()
    if not sep or code not in INGEST_FAILURE_CODES:
        return None, None
    return code, _EXTRACTION_CODE_BY_MESSAGE.get(rest.strip())


def document_view(document: dict[str, Any], extraction: dict[str, Any] | None = None) -> dict[str, Any]:
    """A document as the API returns it (RAG P1): `content_encoding` "base64" and `content` null
    for a binary document (the stored bytes never leave), `error_code` and `error_reason` from the
    error, and `extraction` when given."""
    out = dict(document)
    spec = _spec_or_none(out.get("mime_type"))
    binary = spec is not None and spec.binary
    out["content_encoding"] = "base64" if binary else None
    if binary:
        out["content"] = None
    out["error_code"], out["error_reason"] = document_error_codes(out.get("error"))
    if extraction is not None:
        out["extraction"] = {**extraction, "current_version": EXTRACTOR_VERSIONS.get(str(extraction.get("extractor")))}
    return out


class InvalidDocumentCursor(ValueError):
    """An `after` cursor this API did not issue."""


def encode_document_cursor(document_key: str) -> str:
    """The opaque cursor positioned at `document_key` (URL-safe base64 of {"k": key}, unpadded)."""
    raw = json.dumps({"k": document_key}, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_document_cursor(token: str) -> str:
    """The document key a cursor names; InvalidDocumentCursor for anything else."""
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8"))
    except (binascii.Error, ValueError) as exc:
        raise InvalidDocumentCursor("cursor is not valid") from exc
    if not isinstance(data, dict) or set(data) != {"k"} or not isinstance(data["k"], str) or not data["k"]:
        raise InvalidDocumentCursor("cursor is not valid")
    return data["k"]


def ingest_totals(store: KnowledgeStore, tenant_id: str, kb_id: str) -> dict[str, Any]:
    """What the knowledge base's kb_ingest runs have spent: one aggregate over their model requests
    (ix_runs_tenant_kb_created, then run_model_requests by primary key). Requests of runs still in
    flight count too. Cost follows the run rule: NULL when any request is unpriced."""
    with store.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT count(q.run_id) AS requests,"
                " coalesce(sum(q.input_tokens + q.output_tokens), 0) AS tokens,"
                " CASE WHEN count(q.run_id) = count(q.cost_usd) THEN coalesce(sum(q.cost_usd), 0) END AS cost"
                " FROM runs r JOIN run_model_requests q ON q.run_id = r.id AND q.tenant_id = r.tenant_id"
                " WHERE r.tenant_id = :t AND r.knowledge_base_id = CAST(:kb AS uuid) AND r.kind = 'kb_ingest'"
            ),
            {"t": tenant_id, "kb": kb_id},
        ).mappings().one()
    cost = row["cost"]
    if cost is not None:
        cost = format_cost(Decimal(cost).quantize(COST_QUANTUM))
    return {"ingest_requests": int(row["requests"]), "ingest_tokens": int(row["tokens"]), "ingest_cost_usd": cost}


T = TypeVar("T")


def _retry_deadlock_once(operation: Callable[[], T]) -> T:
    """Run a delete, and once more if Postgres chose it as a deadlock victim (SQLSTATE 40P01)."""
    try:
        return operation()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != DEADLOCK_SQLSTATE:
            raise
        log.warning("knowledge delete was a deadlock victim; retrying once")
        return operation()


REINDEX_ENQUEUE_FAILED_DETAIL = (
    "Reindex could not be started and was cancelled; retry the request (without run_id, or with a new one)"
)

class HandOffFailed(DefinitionError):
    """The enqueue of a recorded run failed: 503 {"detail", "run_id"}."""

    status_code = 503

    def __init__(self, run_id: str, detail: str = ENQUEUE_FAILED_DETAIL) -> None:
        super().__init__(detail, run_id=run_id)
        self.run_id = run_id


def _run_in_use(exc: handoff.RunIdInUse) -> DefinitionError:
    return refusal(409, exc.detail)


def _create_ingest_run(
    run_store: RunStore, tenant_id: str, kb_id: str, input_json: str, run_id: uuid.UUID | str,
    trace: tuple[str | None, str | None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create (or find, for a retry with the same run_id) the kb_ingest run. 409 when `run_id` is
    another tenant's or another request's run (callers resolve run_id before writing; this is the
    last guard against a race)."""
    trace_id, traceparent = trace if trace is not None else current_trace_context()
    try:
        return handoff.create_ingest_run(
            run_store, tenant_id, kb_id, input_json, run_id, trace_id=trace_id, traceparent=traceparent
        )
    except handoff.RunIdInUse as exc:
        raise _run_in_use(exc) from None


_is_run_of = handoff.is_run_of
_run_input = handoff.run_input
_is_document_run = handoff.is_document_run
_enqueue_failed = handoff.enqueue_failed
_run_view = handoff.run_view
_stale_queued = handoff.stale_queued
_renew_claim = handoff.renew_claim
_latest_run = handoff.latest_run
_orphaned = handoff.orphaned


def _hand_off(
    run_store: RunStore,
    launcher: RunLauncher,
    tenant_id: str,
    run: dict[str, Any],
    created: bool,
    failed_detail: str = ENQUEUE_FAILED_DETAIL,
) -> dict[str, Any]:
    """Enqueue `eap.kb_ingest` for the stored run (eap_run_store.kb_ingest_handoff.hand_off, shared
    with the worker's source sync); a failed enqueue raises HandOffFailed (503 with the run_id)."""
    try:
        return handoff.hand_off(run_store, launcher.launch_kb_ingest, tenant_id, run, created, failed_detail)
    except handoff.HandOffFailed as exc:
        raise HandOffFailed(exc.run_id, exc.detail) from None


def _replay(run_store: RunStore, launcher: RunLauncher, tenant_id: str, run: dict[str, Any]) -> dict[str, Any]:
    """A retry of the request that created `run`: hand it off again unless it has finished."""
    try:
        return handoff.replay(run_store, launcher.launch_kb_ingest, tenant_id, run)
    except handoff.HandOffFailed as exc:
        raise HandOffFailed(exc.run_id, exc.detail) from None


def _resolve_client_run_id(
    store: KnowledgeStore, run_store: RunStore, tenant_id: str, run_id: uuid.UUID | None
) -> dict[str, Any] | None:
    """Resolve a client run_id BEFORE anything is written: None when unused, the tenant's run when
    it exists, 409 when another tenant holds it."""
    try:
        return handoff.resolve_client_run_id(store, run_store, tenant_id, run_id)
    except handoff.RunIdInUse as exc:
        raise _run_in_use(exc) from None


# ----------------------------------------------------------------------- references (409 on delete)


def referencing_agents(store: KnowledgeStore, tenant_id: str, kb_id: str) -> list[dict[str, Any]]:
    """The tenant's agents whose latest published version OR current draft attaches `kb_id`
    (spec.knowledge_bases), archived agents included, one entry per agent: `version` is the latest
    published version when it attaches the knowledge base, else null; `draft` is true when only the
    draft attaches it (a draft test run of such an agent would get the retrieve tool's "no longer
    exist" error after a forced delete, like a published version). One query: the latest version per
    agent through the unique (agent_id, version) index, then JSONB containment tests on both specs."""
    needle = json.dumps([{"id": str(kb_id)}])
    with store.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, name, archived, version, in_published FROM ("
                " SELECT a.id, a.name, a.archived, v.version,"
                "  coalesce(v.spec -> 'knowledge_bases' @> CAST(:needle AS jsonb), false) AS in_published,"
                "  coalesce(a.draft_spec -> 'knowledge_bases' @> CAST(:needle AS jsonb), false) AS in_draft"
                " FROM agents a"
                " LEFT JOIN LATERAL (SELECT av.version, av.spec FROM agent_versions av"
                "   WHERE av.agent_id = a.id AND av.tenant_id = a.tenant_id ORDER BY av.version DESC LIMIT 1) v"
                " ON true"
                " WHERE a.tenant_id = :t) refs"
                " WHERE in_published OR in_draft"
                " ORDER BY name, id"
            ),
            {"t": tenant_id, "needle": needle},
        ).mappings().all()
    return [
        {
            "agent_id": str(r["id"]),
            "agent_name": r["name"],
            "version": r["version"] if r["in_published"] else None,
            "archived": bool(r["archived"]),
            "draft": not r["in_published"],
        }
        for r in rows
    ]


# ------------------------------------------------------------------------------- price estimate


def estimate_embedding_cost(model_id: str, tokens: int, today: date | None = None) -> tuple[str | None, str | None, str | None]:
    """(cost_usd as a decimal string, price_source, note) for `tokens` input tokens of `model_id`.

    Reads the operator override file with the worker's own loader and lookup rules (eap_pricing: an
    exact `provider:name` key wins over `provider:*`; the period with the latest `effective_from`
    not after today applies; a file the worker rejects prices nothing here either). A provider the
    worker prices through genai-prices gets no estimate here yet (eap_pricing.price_request could
    give one; not switched in this batch)."""
    provider = model_id.partition(":")[0]
    overrides = load_overrides()
    if isinstance(overrides, InvalidPriceFile):
        if overrides.reason.startswith("file not found"):
            return None, None, f"the price file named by {PRICES_FILE_ENV} does not exist"
        return None, None, "the model price override file is unreadable or invalid"
    period = override_period(overrides, model_id, today or datetime.now(timezone.utc).date())
    if period is not None:
        cost = override_cost(period, Usage(input_tokens=tokens)).quantize(COST_QUANTUM)
        return format_cost(cost), SOURCE_OVERRIDE, None
    if provider in OVERRIDE_ONLY_PROVIDERS:
        return None, None, f"no price for '{model_id}' in the model price override file"
    return None, None, (
        f"'{provider}' models are priced by genai-prices in the worker and are not estimated here; "
        "the cost is recorded on the reindex run"
    )


# ------------------------------------------------------------------------- re-extract helpers

REEXTRACT_ENQUEUE_FAILED_DETAIL = (
    "the ingest run was recorded but could not be started; retry it with POST "
    "/knowledge-bases/{id}/documents/{key}/reingest"
)


def _reextract_replay(
    run_store: RunStore, launcher: RunLauncher, tenant_id: str, run: dict[str, Any], *, created: bool
) -> dict[str, Any]:
    """Hand off a re-extract run this request created. A run it found (a retry or a concurrent
    duplicate with the same run_id) is answered as recorded, with no second dispatch, unless its
    enqueue failed or it is still `queued` past ORPHAN_AFTER_SECONDS (its request may have died
    before enqueuing): then it is handed off again (idempotent in DBOS)."""
    if created or _enqueue_failed(run) or _stale_queued(run):
        return _hand_off(run_store, launcher, tenant_id, run, created)
    return _run_view(run)


def outdated_extractor_map(
    kb: dict[str, Any], formats: set[str] | None = None
) -> dict[str, tuple[str | None, int]]:
    """Base MIME type (every alias too) -> (the worker extractor that handles it in this knowledge
    base, its current version), for KnowledgeStore.outdated_extraction_documents. An API-extracted
    type maps to (None, 0): its document is outdated only when its active chunks still carry a
    worker extractor's record (a flavour or extractor change, e.g. Markdown back from Doxygen)."""
    extract_config = kb_extract_config(kb)
    out: dict[str, tuple[str | None, int]] = {}
    for spec in FORMAT_SPECS:
        if formats is not None and spec.format not in formats:
            continue
        extractor = extractor_for(spec.mime_type, extract_config)
        for mime in spec.mime_types:
            out[mime.lower()] = (extractor, EXTRACTOR_VERSIONS[extractor]) if extractor is not None else (None, 0)
    return out


def _document_ref(document: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "document_id": document["id"],
        "document_key": document["document_key"],
        "mime_type": document["mime_type"],
        "doc_version": document["latest_version"],
        **extra,
    }


def safe_errors(exc: ValidationError, prefix: list[Any]) -> list[dict[str, Any]]:
    """loc/msg/type only: never the rejected values (a query can hold anything a user typed)."""
    return [
        {"loc": [*prefix, *err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


def search_view(run: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """(finished, body): the result (or the failure) once the tool run ended, else the run id to
    follow through GET /runs/{id} (finished False)."""
    view: dict[str, Any] = {
        "run_id": str(run["id"]),
        "kind": "tool",
        "tool_id": KNOWLEDGE_SEARCH_TOOL_ID,
        "knowledge_base_id": str(run["knowledge_base_id"]) if run.get("knowledge_base_id") else None,
        "status": run["status"],
        "display_status": run["display_status"],
        "trace_id": run.get("trace_id"),
    }
    if run["status"] == "completed":
        try:
            view["result"] = json.loads(run["output"] or "null")
        except ValueError:  # pragma: no cover - the worker stores canonical JSON
            view["result"] = None
        return True, view
    if run["status"] in TERMINAL_RUN_STATUSES:
        view["error"] = run.get("error")
        return True, view
    return False, view


class KnowledgeService(AccessBound):
    """Knowledge bases, documents, ingest, re-extract, reindex and the search tester (tenant-scoped).

    Per-resource authorisation (IAM P2b): a knowledge base is the resource; its documents, sources and runs are checked
    through it (`read` to see, `edit` to change, `run` to search), so no tuple is written per document."""

    def __init__(
        self, store: KnowledgeStore, run_store: RunStore, launcher: RunLauncher, access: Access | None = None
    ) -> None:
        self.store = store
        self.run_store = run_store
        self.launcher = launcher
        self._access = access  # None: no caller to check (see eap_api.services.access)

    def _require(self, action: str, kb_id: str) -> None:
        acl.require(self._access, action, "knowledge_base", str(kb_id))

    # ------------------------------------------------------------------ embedding models

    def embedding_models(self) -> list[dict[str, Any]]:
        """Every allowed concrete model with its dimensions, then `provider:*=<dims>` wildcards."""
        acl.require(self._access, "read", "tenant", None)
        policy = _policy()
        items: list[dict[str, Any]] = [
            {"id": m.id, "provider": m.provider, "name": m.name, "dimensions": m.dimensions, "wildcard": False}
            for m in policy.listed()
        ]
        for provider, dims in sorted(policy.wildcards.items()):
            if dims is not None:
                items.append(
                    {"id": f"{provider}:*", "provider": provider, "name": "*", "dimensions": dims, "wildcard": True}
                )
        return items

    # ------------------------------------------------------------------ knowledge bases

    def list_knowledge_bases(self, tenant_id: str) -> list[dict[str, Any]]:
        items = self.store.list_knowledge_bases(tenant_id)
        flt = acl.visible(self._access, "knowledge_base")  # one engine call, whatever the count
        allowed = set(acl.keep(flt, [str(i["id"]) for i in items]))
        return [i for i in items if str(i["id"]) in allowed]

    def create_knowledge_base(
        self, tenant_id: str, *, name: str, description: str | None, embedding_model: str, chunking: Any
    ) -> dict[str, Any]:
        acl.require(self._access, "create", "knowledge_base", None)
        model = _resolve_model(embedding_model)
        try:
            kb = self.store.create_knowledge_base(
                tenant_id, name=name, description=description, embedding_model=model.id,
                dimensions=model.dimensions, chunking=chunking,
            )
        except KnowledgeBaseNameConflict as exc:
            raise refusal(409, str(exc))
        acl.register(self._access, "knowledge_base", str(kb["id"]))  # before the response: the creator owns it
        return {**kb, "document_count": 0, "chunk_count": 0}

    def get_knowledge_base(self, tenant_id: str, kb_id: str) -> dict[str, Any]:
        self._require("read", kb_id)
        kb = _kb_or_404(self.store, tenant_id, kb_id)
        return {**kb, **ingest_totals(self.store, tenant_id, kb["id"])}

    def update_knowledge_base(self, tenant_id: str, kb_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        self._require("edit", kb_id)
        try:
            self.store.update_knowledge_base(tenant_id, kb_id, **fields)
        except KnowledgeBaseNotFound:
            raise refusal(404, KB_NOT_FOUND)
        except KnowledgeBaseNameConflict as exc:
            raise refusal(409, str(exc))
        return _kb_or_404(self.store, tenant_id, kb_id)

    def delete_knowledge_base(self, tenant_id: str, kb_id: str, force: bool = False) -> None:
        self._require("archive", kb_id)
        store = self.store
        kb = _kb_or_404(store, tenant_id, kb_id)
        if not force:
            agents = referencing_agents(store, tenant_id, kb["id"])
            if agents:
                names = ", ".join(a["agent_name"] for a in agents[:5]) + (" …" if len(agents) > 5 else "")
                raise refusal(
                    409,
                    f"knowledge base is attached to {len(agents)} agent(s) ({names}); "
                    "detach it or delete with force=true",
                    agents=agents,
                )
        if not _retry_deadlock_once(lambda: store.delete_knowledge_base(tenant_id, kb["id"])):
            raise refusal(404, KB_NOT_FOUND)
        acl.unregister(self._access, "knowledge_base", str(kb["id"]))

    # ------------------------------------------------------------------ documents

    def list_documents(self, tenant_id: str, kb_id: str, limit: int, after: str | None) -> dict[str, Any]:
        self._require("read", kb_id)
        kb = _kb_or_404(self.store, tenant_id, kb_id)
        after_key = None
        if after is not None:
            try:
                after_key = decode_document_cursor(after)
            except InvalidDocumentCursor:
                raise refusal(422, "after is not a valid cursor; pass the previous page's next_after verbatim")
        items = self.store.list_documents(tenant_id, kb["id"], limit=limit, after=after_key)
        next_after = encode_document_cursor(items[-1]["document_key"]) if len(items) == limit else None
        return {"items": [document_view(d) for d in items], "next_after": next_after}

    def get_document(self, tenant_id: str, kb_id: str, document_key: str, include_content: bool) -> dict[str, Any]:
        self._require("read", kb_id)
        store = self.store
        kb = _kb_or_404(store, tenant_id, kb_id)
        document = store.get_document(tenant_id, kb["id"], document_key)
        if document is None:
            raise refusal(404, DOCUMENT_NOT_FOUND)
        spec = _spec_or_none(document["mime_type"])
        if include_content:
            if spec is not None and spec.binary:
                document["content"] = None
            else:
                document = store.get_document(tenant_id, kb["id"], document_key, include_content=True) or document
        extraction = None
        if spec is not None and extractor_for(spec.mime_type, kb_extract_config(kb)) is not None:
            extraction = store.latest_extraction(tenant_id, kb["id"], document["id"])
        return document_view(document, extraction)

    def put_document(
        self, tenant_id: str, kb_id: str, document_key: str, body: DocumentPut, body_size: int
    ) -> dict[str, Any]:
        """Upload a document (see the route's docstring for the status rules). Returns the answer's body;
        `unchanged` says whether it is the 200 (no run) or the 202."""
        self._require("edit", kb_id)
        _check_document_key(document_key)
        if body.document_key is not None and body.document_key != document_key:
            raise refusal(422, "document_key in the body must match the path")
        with binary_upload_slot(_spec_or_none(body.mime_type)):
            return self._put_document(kb_id, document_key, tenant_id, body, body_size)

    def _put_document(
        self, kb_id: str, document_key: str, tenant_id: str, body: DocumentPut, body_size: int
    ) -> dict[str, Any]:
        store = self.store
        try:
            upload = prepare_upload(body, body_size)
        except UploadRejected as rejected:
            raise rejected.as_refusal() from None
        kb = _kb_or_404(store, tenant_id, kb_id)  # before extraction: no CPU for a missing or foreign KB
        title = (body.title or "").strip() or document_key
        if extractor_for(upload.mime_type, kb_extract_config(kb)) is None:
            # API-extracted (unchanged path): reject unsafe HTML or a document with no text early.
            try:
                extracted = extract(upload.content, upload.mime_type, title=title, source_url=body.source_url)
            except UnsupportedMimeType as exc:
                raise refusal(422, str(exc))
            except ExtractionFailed as exc:
                # The fixed message only: the exception's detail may name parser internals.
                raise refusal(422, f"{exc.user_message} ({exc.code}: {exc.reason})")
            if not _has_text(extracted):
                # Only dropped markup (script, style, nav, comments): nothing to chunk, so nothing is stored.
                raise refusal(422, "the document has no extractable text")
        trace_id, traceparent = current_trace_context()
        try:
            outcome = handoff.ingest_document(
                store, self.run_store, self.launcher.launch_kb_ingest, tenant_id, kb["id"],
                document_key=document_key, title=title, content=upload.content, mime_type=upload.mime_type,
                content_sha256=upload.digest, source_url=body.source_url, metadata=body.metadata,
                run_id=body.run_id, trace_id=trace_id, traceparent=traceparent,
            )
        except KnowledgeBaseNotFound:
            raise refusal(404, KB_NOT_FOUND)
        except KnowledgeBaseLimitExceeded as exc:
            raise refusal(409, str(exc))
        except handoff.InvalidDocument as exc:
            raise refusal(422, str(exc))
        except handoff.RunIdInUse as exc:
            raise _run_in_use(exc) from None
        except handoff.HandOffFailed as exc:
            raise HandOffFailed(exc.run_id, exc.detail) from None
        return {
            "unchanged": outcome.unchanged,
            "created": outcome.created,
            "document": document_view(outcome.document),
            **outcome.run,
        }

    def delete_document(self, tenant_id: str, kb_id: str, document_key: str) -> None:
        self._require("edit", kb_id)
        store = self.store
        kb = _kb_or_404(store, tenant_id, kb_id)
        if not _retry_deadlock_once(lambda: store.delete_document(tenant_id, kb["id"], document_key)):
            raise refusal(404, DOCUMENT_NOT_FOUND)

    def reingest_document(
        self, tenant_id: str, kb_id: str, document_key: str, run_id: uuid.UUID | None
    ) -> dict[str, Any]:
        """A new ingest run of the document's latest version. `unchanged` True (and `run_id` None) when it is
        already ingested: the route answers 200 for that and 202 otherwise."""
        self._require("edit", kb_id)
        store, run_store, launcher = self.store, self.run_store, self.launcher
        kb = _kb_or_404(store, tenant_id, kb_id)
        document = store.get_document(tenant_id, kb["id"], document_key)
        if document is None:
            raise refusal(404, DOCUMENT_NOT_FOUND)
        input_json = ingest_input(document["id"], document["latest_version"])
        existing = _resolve_client_run_id(store, run_store, tenant_id, run_id)
        if existing is not None:
            if not _is_run_of(existing, kb["id"], input_json):
                raise refusal(409, "run_id is already used by a different run request")
            view = _replay(run_store, launcher, tenant_id, existing)
        elif document["status"] == "ready" and document["active_version"] == document["latest_version"]:
            return {"run_id": None, "unchanged": True, "knowledge_base_id": kb["id"], "document": document_view(document)}
        else:
            latest = _latest_run(run_store, tenant_id, document)
            if latest is not None and (_enqueue_failed(latest) or _stale_queued(latest)):
                view = _hand_off(run_store, launcher, tenant_id, latest, created=False)
            elif latest is not None and latest["status"] not in TERMINAL_RUN_STATUSES:
                raise refusal(409, f"the document is already being ingested (run {latest['id']} is {latest['status']})")
            elif latest is None and document["last_run_id"] and not _orphaned(document) and document["status"] == "pending":
                raise refusal(409, f"an ingest run of the document is being started (run {document['last_run_id']})")
            else:
                new_run_id = run_id or uuid.uuid4()
                try:
                    document = store.request_reingest(
                        tenant_id, kb["id"], document_key, run_id=str(new_run_id),
                        expected_last_run_id=document["last_run_id"], doc_version=document["latest_version"],
                    )
                except DocumentNotFound:
                    raise refusal(404, DOCUMENT_NOT_FOUND)
                except IngestRunConflict:
                    raise refusal(409, "another ingest run of the document was started concurrently")
                if not _renew_claim(store, tenant_id, document, new_run_id):
                    raise refusal(409, "another ingest run of the document was started concurrently")
                run, created = _create_ingest_run(run_store, tenant_id, kb["id"], input_json, new_run_id, trace=current_trace_context())
                view = _hand_off(run_store, launcher, tenant_id, run, created)
        document = store.get_document(tenant_id, kb["id"], document_key) or document
        return {**view, "unchanged": False, "document": document_view(document)}

    def reextract_document(
        self, tenant_id: str, kb_id: str, document_key: str, run_id: uuid.UUID | None
    ) -> dict[str, Any]:
        """A new version with the same content and an ingest run of it (see the route's docstring)."""
        self._require("edit", kb_id)
        store, run_store, launcher = self.store, self.run_store, self.launcher
        kb = _kb_or_404(store, tenant_id, kb_id)
        document = store.get_document(tenant_id, kb["id"], document_key)
        if document is None:
            raise refusal(404, DOCUMENT_NOT_FOUND)
        existing = _resolve_client_run_id(store, run_store, tenant_id, run_id)
        if existing is not None and _is_document_run(existing, kb["id"], document["id"]):
            # The document read above can predate the version this run_id made (a concurrent
            # duplicate of the same request): compare against the document as it is now.
            document = store.get_document(tenant_id, kb["id"], document_key) or document
        if existing is not None:
            if not _is_run_of(existing, kb["id"], ingest_input(document["id"], document["latest_version"])):
                raise refusal(409, "run_id is already used by a different run request")
            view = _reextract_replay(run_store, launcher, tenant_id, existing, created=False)
        else:
            new_run_id = run_id or uuid.uuid4()
            try:
                document = store.new_document_version(
                    tenant_id, kb["id"], document_key, run_id=str(new_run_id),
                    expected_latest_version=document["latest_version"],
                )
            except DocumentNotFound:
                raise refusal(404, DOCUMENT_NOT_FOUND)
            except IngestRunConflict:
                current = store.get_document(tenant_id, kb["id"], document_key)
                if current is None:
                    raise refusal(404, DOCUMENT_NOT_FOUND)
                if current["last_run_id"] != str(new_run_id):
                    raise refusal(409, "another upload or re-extract of the document was started concurrently")
                # A concurrent duplicate with this run_id already made the version: answer its run.
                document = current
            # No _renew_claim here: the bump itself was the compare-and-set and recorded the run id,
            # so the run of the version this request made is always created (a later version
            # supersedes it in the worker); a crash before that is the 60 s orphan rule's case.
            input_json = ingest_input(document["id"], document["latest_version"])
            run, created = _create_ingest_run(run_store, tenant_id, kb["id"], input_json, new_run_id, trace=current_trace_context())
            view = _reextract_replay(run_store, launcher, tenant_id, run, created=created)
        document = store.get_document(tenant_id, kb["id"], document_key) or document
        return {**view, "unchanged": False, "document": document_view(document)}

    def reextract_knowledge_base(
        self, tenant_id: str, kb_id: str, *, formats: list[str] | None, only_outdated: bool,
        chunker_version: int | None, dry_run: bool,
    ) -> dict[str, Any]:
        """Re-extract many documents. The answer's `started` says whether the route answers 202 or 200."""
        self._require("edit", kb_id)
        store, run_store, launcher = self.store, self.run_store, self.launcher
        kb = _kb_or_404(store, tenant_id, kb_id)
        format_set: set[str] | None = None
        if formats is not None:
            format_set = set()
            for name in formats:
                try:
                    format_set.add(format_by_name(name).format)
                except KeyError:
                    expected = ", ".join(spec.format for spec in FORMAT_SPECS)
                    raise refusal(422, f"unknown format {name!r}: expected one of {expected}")
        current_chunker = int((kb.get("chunking") or {}).get("chunker_version", 1))
        change_chunker = chunker_version is not None and chunker_version != current_chunker

        if change_chunker:
            selected = store.list_documents(tenant_id, kb["id"])
        elif only_outdated:
            selected = store.outdated_extraction_documents(
                tenant_id, kb["id"], outdated_extractor_map(kb, format_set), orphan_after_seconds=ORPHAN_AFTER_SECONDS
            )
        else:
            selected = [
                d for d in store.list_documents(tenant_id, kb["id"])
                if format_set is None or ((spec := _spec_or_none(d["mime_type"])) is not None and spec.format in format_set)
            ]

        def answer(refs: list[dict[str, Any]], *, chunker: int, changed: bool, started: int, failed: int,
                   dry: bool = False) -> dict[str, Any]:
            return {
                "knowledge_base_id": kb["id"],
                "dry_run": dry,
                "chunker_version": chunker,
                "chunker_version_changed": changed,
                "selected": len(refs),
                "started": started,
                "failed": failed,
                "documents": refs,
                "knowledge_base": _kb_or_404(store, tenant_id, kb["id"]),
            }

        if dry_run or (not selected and not change_chunker):
            return answer(
                [_document_ref(d) for d in selected],
                chunker=chunker_version or current_chunker if dry_run else current_chunker,
                changed=False, started=0, failed=0, dry=dry_run,
            )

        try:
            updated_kb, bumped, changed = store.start_reextract(
                tenant_id, kb["id"],
                document_ids=None if change_chunker else [d["id"] for d in selected],
                chunker_version=chunker_version if change_chunker else None,
            )
        except KnowledgeBaseNotFound:
            raise refusal(404, KB_NOT_FOUND)
        except ValidationError as exc:  # the stored config cannot take chunker v2 (it always can today)
            msg = exc.errors()[0]["msg"] if exc.errors() else "invalid"
            raise refusal(422, f"the knowledge base cannot move to chunker v{chunker_version}: {msg}")

        refs: list[dict[str, Any]] = []
        started = failed = 0
        for document in bumped:
            run_id = document["last_run_id"]
            input_json = ingest_input(document["id"], document["latest_version"])
            try:
                run, created = _create_ingest_run(run_store, tenant_id, kb["id"], input_json, run_id, trace=current_trace_context())
                view = _hand_off(run_store, launcher, tenant_id, run, created)
            except Exception as exc:  # noqa: BLE001 - one document's failure must not strand the others
                # Its new version is stored with last_run_id = run_id: a reingest of the document starts
                # (or re-queues) that run, also when the run row was never written (the orphan rule).
                if not isinstance(exc, DefinitionError):
                    log_failure(
                        log, "re-extract run start failed", exc, code="reextract_start_failed", level=logging.ERROR,
                        tenant_id=tenant_id, run_id=str(run_id),
                    )
                failed += 1
                refs.append(_document_ref(
                    document, run_id=str(run_id), status="failed",
                    detail=(
                        str(exc.detail)
                        if isinstance(exc, DefinitionError) and not isinstance(exc, HandOffFailed)
                        else REEXTRACT_ENQUEUE_FAILED_DETAIL
                    ),
                ))
                continue
            started += 1
            refs.append(_document_ref(document, run_id=view["run_id"], status=view["status"]))
        chunker = int((updated_kb.get("chunking") or {}).get("chunker_version", 1))
        return answer(refs, chunker=chunker, changed=changed, started=started, failed=failed)

    # ------------------------------------------------------------------ reindex

    def reindex_knowledge_base(
        self, tenant_id: str, kb_id: str, *, embedding_model: str, dry_run: bool, run_id: uuid.UUID | None
    ) -> dict[str, Any]:
        """Re-embed into a new generation. A dry run answers the estimate (`dry_run` True: the route's 200);
        otherwise the kb_ingest run (the 202)."""
        self._require("edit", kb_id)
        store, run_store, launcher = self.store, self.run_store, self.launcher
        model = _resolve_model(embedding_model)
        kb = _kb_or_404(store, tenant_id, kb_id)

        if dry_run:
            try:
                estimate = store.estimate_reindex(tenant_id, kb["id"])
            except KnowledgeBaseNotFound:
                raise refusal(404, KB_NOT_FOUND)
            cost, source, note = estimate_embedding_cost(model.id, estimate.approx_tokens)
            budget = max_ingest_tokens()
            return {
                "dry_run": True,
                "knowledge_base_id": kb["id"],
                "embedding_model": model.id,
                "dimensions": model.dimensions,
                "chunks": estimate.chunks,
                "approx_tokens": estimate.approx_tokens,
                "max_ingest_tokens": budget,
                "exceeds_budget": estimate.approx_tokens > budget,
                "estimated_cost_usd": cost,
                "price_source": source,
                "cost_note": note,
                "reindex_pending": kb["pending_generation"] is not None,
            }

        def accepted(view: dict[str, Any]) -> dict[str, Any]:
            return {**view, "knowledge_base": _kb_or_404(store, tenant_id, kb["id"])}

        pending = kb["pending_generation"]
        existing = _resolve_client_run_id(store, run_store, tenant_id, run_id)
        if existing is not None:
            if not (
                pending is not None
                and kb["pending_embedding_model"] == model.id
                and _is_run_of(existing, kb["id"], reindex_input(pending))
            ):
                raise refusal(409, "run_id is not the run of this knowledge base's pending reindex with this model")
            if kb["status"] == "reindex_failed" and (
                _enqueue_failed(existing) or existing["status"] not in TERMINAL_RUN_STATUSES
            ):
                # The replay would hand the run off again: claim the failed generation first, so it
                # cannot race a plain retry (or another replay) into a second running reindex.
                if not store.claim_reindex_retry(tenant_id, kb["id"], pending):
                    raise refusal(409, "a retry of this reindex was started concurrently, or it was cancelled")
                try:
                    return accepted(_replay(run_store, launcher, tenant_id, existing))
                except HandOffFailed:
                    store.mark_reindex_failed(tenant_id, kb["id"], pending, "the reindex retry could not be started")
                    raise
            if _enqueue_failed(existing):
                # Its failed hand-off put the generation back to 'reindex_failed'; it is 'reindexing'
                # now, so another retry claimed it and runs it: never start a second run.
                raise refusal(409, "a retry of this reindex was started concurrently, or it was cancelled")
            return accepted(_replay(run_store, launcher, tenant_id, existing))
        if pending is not None and kb["status"] == "reindex_failed" and kb["pending_embedding_model"] == model.id:
            # Retry the failed reindex: a new run of the same pending generation reuses its
            # embeddings. The compare-and-set 'reindex_failed' -> 'reindexing' admits one retry.
            if not store.claim_reindex_retry(tenant_id, kb["id"], pending):
                raise refusal(409, "a retry of this reindex was started concurrently, or it was cancelled")
            try:
                run, created = _create_ingest_run(
                    run_store, tenant_id, kb["id"], reindex_input(pending), run_id or uuid.uuid4(), trace=current_trace_context()
                )
                return accepted(_hand_off(run_store, launcher, tenant_id, run, created))
            except DefinitionError:
                # Back to 'reindex_failed', so the same retry can be sent again.
                store.mark_reindex_failed(tenant_id, kb["id"], pending, "the reindex retry could not be started")
                raise

        try:
            opened = store.start_reindex(tenant_id, kb["id"], embedding_model=model.id, dimensions=model.dimensions)
        except KnowledgeBaseNotFound:
            raise refusal(404, KB_NOT_FOUND)
        except ReindexInProgress as exc:
            raise refusal(409, f"{exc}; wait for it, or cancel it with DELETE /knowledge-bases/{{id}}/reindex")
        generation = opened["pending_generation"]
        try:
            run, created = _create_ingest_run(
                run_store, tenant_id, kb["id"], reindex_input(generation), run_id or uuid.uuid4(), trace=current_trace_context()
            )
            return accepted(
                _hand_off(run_store, launcher, tenant_id, run, created, failed_detail=REINDEX_ENQUEUE_FAILED_DETAIL)
            )
        except DefinitionError:
            # No run will embed the generation just opened: drop it, so a plain retry can start over.
            self._cancel_generation(tenant_id, kb["id"], generation)
            raise

    def _cancel_generation(self, tenant_id: str, kb_id: str, generation: int) -> None:
        store = self.store
        current = store.get_knowledge_base(tenant_id, kb_id)
        if current is not None and current["pending_generation"] == generation:
            _retry_deadlock_once(lambda: store.cancel_reindex(tenant_id, kb_id))

    def cancel_reindex(self, tenant_id: str, kb_id: str) -> None:
        self._require("edit", kb_id)
        kb = _kb_or_404(self.store, tenant_id, kb_id)
        _retry_deadlock_once(lambda: self.store.cancel_reindex(tenant_id, kb["id"]))

    # ------------------------------------------------------------------ search tester (RM.11)

    def start_search(
        self, tenant_id: str, kb_id: str, *, query: str, top_k: int, min_score: float | None,
        filter: dict[str, Any] | None, run_id: uuid.UUID | None,
    ) -> tuple[dict[str, Any], bool, uuid.UUID]:
        """Record the `knowledge.search` tool run: (the run, whether this call created it, its id)."""
        self._require("run", kb_id)
        run_store = self.run_store
        kb = _kb_or_404(self.store, tenant_id, kb_id)
        try:
            validated = KnowledgeSearchInput.model_validate(
                {
                    "knowledge_base_id": kb["id"], "query": query, "top_k": top_k,
                    "min_score": min_score, "filter": filter,
                }
            )
        except ValidationError as exc:
            raise refusal(422, safe_errors(exc, ["body"]))
        input_json = _canonical_json(validated.model_dump(mode="json"))
        rid = run_id or uuid.uuid4()
        trace_id, traceparent = current_trace_context()
        try:
            _, created = run_store.create_tool_run(
                tenant_id, KNOWLEDGE_SEARCH_TOOL_ID, input_json, rid,
                trace_id=trace_id, traceparent=traceparent, knowledge_base_id=kb["id"],
            )
        except RunIdConflict:
            raise refusal(409, "run_id is already in use")
        run = run_store.get_run(tenant_id, rid)
        if run is None:  # pragma: no cover - deleted between insert and read
            raise refusal(404, "Run not found")
        if not created and (
            run["kind"] != "tool" or run["tool_id"] != KNOWLEDGE_SEARCH_TOOL_ID or run["input"] != input_json
        ):
            raise refusal(409, "run_id is already used by a different run request")
        return run, created, rid

    def hand_off_search(self, tenant_id: str, run: dict[str, Any], created: bool, run_id: uuid.UUID) -> None:
        """Enqueue the search run (503 HandOffFailed, the run marked failed, when the launcher refuses)."""
        run_store = self.run_store
        owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)
        if not owns_handoff and run["status"] in TERMINAL_RUN_STATUSES:
            return  # a retry of a finished search: answer what was recorded
        try:
            self.launcher.launch_tool(
                run_id=run_id, tenant_id=tenant_id, tool_id=KNOWLEDGE_SEARCH_TOOL_ID,
                input_json=run["input"], traceparent=run.get("traceparent"),
            )
        except Exception as exc:
            log_failure(
                log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
                run_id=str(run_id), tenant_id=tenant_id,
            )
            if owns_handoff:
                try:
                    run_store.mark_enqueue_failed(tenant_id, run_id)
                except Exception as mark_exc:  # noqa: BLE001 - the 503 is the answer either way
                    log_failure(
                        log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                        level=logging.ERROR, run_id=str(run_id), tenant_id=tenant_id,
                    )
            raise HandOffFailed(str(run_id)) from None
        log.info("knowledge search run %s queued: tenant=%s kb=%s", run_id, tenant_id, run.get("knowledge_base_id"))

    def search_progress(
        self, tenant_id: str, run_id: uuid.UUID, fallback: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        """(finished, body) of the search run as it is now (`fallback` when the read finds nothing)."""
        if fallback.get("knowledge_base_id") is not None:
            self._require("run", fallback["knowledge_base_id"])
        current = self.run_store.get_run(tenant_id, run_id) or fallback
        if current["error"] == ENQUEUE_FAILED_ERROR and current["status"] == "failed":
            # A concurrent owner's enqueue failed, but this one succeeded: the workflow exists.
            current = {**current, "status": "queued", "display_status": "queued"}
        return search_view(current)
