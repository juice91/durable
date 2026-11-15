"""Knowledge bases, their documents, ingest and reindex runs (RAG and memory milestone, RM.7).

Every route is tenant-scoped: the tenant comes from the principal only, and every store call
filters by it in SQL. Another tenant's knowledge base, document or run answers 404, never 403.

Documents are uploaded as JSON text (`PUT /knowledge-bases/{id}/documents/{document_key}`). That
route has its own body cap, EAP_KB_MAX_DOCUMENT_BYTES (default 5 MiB, read per request), and keeps
the request guard's UTF-8, NUL and depth checks. The API extracts the document once (eap_knowledge)
to reject an unsupported MIME type or unsafe HTML early; chunking and embedding happen in the
worker. An upload with the same content, metadata, title, source URL and MIME type is a no-op
(200, `unchanged: true`, no run). A changed one bumps `latest_version` and starts a `kb_ingest` run
(202), enqueued as `eap.kb_ingest` on the `eap-kb-ingest` queue (eap_api.launcher.KB_INGEST_WORKFLOW_NAME
documents the arguments). Run creation is idempotent by the optional client `run_id`, like tool runs, and a failed
hand-off answers 503 with the run marked failed so a retry with the same `run_id` re-queues it.

A document key is one URL path segment: no '/', '\\', control characters, surrounding whitespace,
and not only dots (`.` and `..` would be normalized away by a client, onto the knowledge base). The
app answers 404, never a 307, to a trailing-slash path (redirect_slashes=False). The document list
is paged (`limit`, `after`, `next_after`), and GET /knowledge-bases/{id} adds the ingest spend.

Deleting a knowledge base that an agent's latest published version or current draft attaches
answers 409 with the agents (`draft: true` for a draft-only reference), unless `force=true`. Deletes retry once on a deadlock (SQLSTATE 40P01): chunk-row locks
have no fixed order against an in-flight ingest (the RM.4 review).

The reindex dry run prices its token estimate from the operator price override file
(EAP_MODEL_PRICES_FILE, the file the worker prices `test:*` and `litellm:*` from), through the
worker's own loader and arithmetic (eap_pricing). Providers the worker prices from genai-prices
are not estimated here yet: their estimate has tokens and a `cost_note` instead of a cost. (The API
may use eap_pricing's genai-prices lookup, `price_request` / `price_known`; switching this estimate
to it is left to the agent-capabilities branch.)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from eap_api.auth import Principal, current_principal
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.request_guard import GuardedJsonRoute, guarded_json_route, too_large_detail
from eap_api.routes.runs import StoresDep
from eap_api.schemas import (
    DocumentPut,
    DocumentPutResponse,
    EmbeddingModelListResponse,
    EnqueueFailedResponse,
    ErrorDetail,
    IngestRunCreate,
    IngestRunResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseDetailResponse,
    KnowledgeBaseInUse,
    KnowledgeBaseListResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentResponse,
    ReextractCreate,
    ReextractResponse,
    ReindexCreate,
    ReindexEstimateResponse,
    document_key_problem,
)
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
from eap_observability import current_trace_context, log_failure
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
from eap_run_store.cursor import MAX_CURSOR_LENGTH
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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="too many binary uploads in progress; retry shortly",
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


def _guard_text_cap() -> int:
    """The upload guard's soft cap: the text cap (EAP_KB_MAX_DOCUMENT_BYTES) on the request body."""
    try:
        return max_document_bytes()
    except ValueError:  # checked at startup; the environment changed under the process
        return DEFAULT_KB_MAX_DOCUMENT_BYTES


_BASE64_KEY = b'"content_base64"'


def _is_base64_upload(prefix: bytes) -> bool:
    """Whether a body that streamed past the text cap may grow to the binary guard: its first
    text-cap bytes hold the `content_base64` key (every other field is far below the text cap, so
    a binary upload always names it by then). A text body (`content`) is refused at the text cap
    before it is buffered further, as before RAG P1."""
    return _BASE64_KEY in prefix


# The document upload's guard: the same checks as every JSON write route, with its own body cap:
# the text cap for a body without `content_base64`, the binary guard for a base64 one.
DocumentUploadRoute = guarded_json_route(
    max_body_bytes=upload_guard_bytes, soft_body_bytes=_guard_text_cap, allow_beyond_soft=_is_base64_upload
)

router = APIRouter(tags=["knowledge"])
_kb = APIRouter(
    prefix="/knowledge-bases",
    tags=["knowledge"],
    route_class=GuardedJsonRoute,
    responses={404: {"model": ErrorDetail}},
)
_upload = APIRouter(
    prefix="/knowledge-bases",
    tags=["knowledge"],
    route_class=DocumentUploadRoute,
    responses={404: {"model": ErrorDetail}},
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]


def get_knowledge_store(request: Request) -> KnowledgeStore:
    store = getattr(request.app.state, "knowledge_store", None)
    if store is None:  # an app built without create_app's wiring (tests)
        store = request.app.state.knowledge_store = KnowledgeStore(request.app.state.database_engine)
    return store


KnowledgeDep = Annotated[KnowledgeStore, Depends(get_knowledge_store)]
LauncherDep = Annotated[RunLauncher, Depends(get_run_launcher)]


async def _request_body_size(request: Request) -> int:
    """The request body's size in bytes (the guard has read and cached it)."""
    return len(await request.body())


BodySizeDep = Annotated[int, Depends(_request_body_size)]


# ----------------------------------------------------------------------------------- helpers


def _policy() -> EmbeddingModelPolicy:
    try:
        return EmbeddingModelPolicy.from_env()
    except ValueError as exc:  # checked at startup; the environment changed under the process
        log_failure(log, "embedding model allowlist invalid", exc, code="embedding_policy_invalid", level=logging.ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the embedding model allowlist (EAP_ALLOWED_EMBEDDING_MODELS) is misconfigured",
        )


def _resolve_model(model_id: str) -> EmbeddingModel:
    """The allowed embedding model, or 422 with the policy's message."""
    try:
        return _policy().resolve(model_id)
    except EmbeddingModelNotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


def _kb_or_404(store: KnowledgeStore, tenant_id: str, kb_id: str) -> dict[str, Any]:
    kb = store.get_knowledge_base(tenant_id, kb_id)
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
    return kb


def _check_document_key(document_key: str) -> None:
    problem = document_key_problem(document_key)
    if problem is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)


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


# P1.C5: new kb_ingest runs embed with the split steps (eap.kb_embed_call, then eap.kb_embed_write),
# so a database retry of the write never calls the provider again. Runs enqueued before (no flag)
# keep eap.kb_embed_batch, so in-flight workflows recover unchanged.
EMBED_SPLIT_KEY = "embed_split"


def ingest_input(document_id: str, doc_version: int) -> str:
    """The `input` of a document ingest run (and the enqueued `input_json`)."""
    return _canonical_json({"document_id": str(document_id), "doc_version": int(doc_version), EMBED_SPLIT_KEY: True})


def reindex_input(generation: int) -> str:
    """The `input` of a reindex run (and the enqueued `input_json`)."""
    return _canonical_json({"reindex": int(generation), EMBED_SPLIT_KEY: True})


def _same_ingest_input(stored: Any, input_json: str) -> bool:
    """Whether a stored kb_ingest run input names the same work as `input_json`, ignoring the
    `embed_split` flag (a run created before P1.C5 has none and is still that version's run)."""
    if stored == input_json:
        return True
    try:
        a, b = json.loads(stored), json.loads(input_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    a.pop(EMBED_SPLIT_KEY, None)
    b.pop(EMBED_SPLIT_KEY, None)
    return a == b


# ------------------------------------------------------------------ formats and uploads (RAG P1.A4)


class UploadRejected(Exception):
    """An upload refused before anything is written: the route answers `status_code` with `body`."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(status_code, body)

    def response(self) -> JSONResponse:
        return _json(self.status_code, self.body)


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
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail, code="unsupported_content", detected_type=detected
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
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "content_base64 is not valid base64 (the standard alphabet with padding, no line breaks)",
        ) from None


def _format_or_422(mime_type: str) -> FormatSpec:
    try:
        return format_for_mime(mime_type)
    except UnsupportedMimeType as exc:
        raise _reject(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None


def _limits_or_503() -> ExtractLimits:
    try:
        return extract_limits()
    except ValueError as exc:
        log_failure(log, "extraction limits invalid", exc, code="extract_limits_invalid", level=logging.ERROR)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the knowledge base upload limits (EAP_KB_MAX_*) are misconfigured",
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
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"unsupported document mime type {body.mime_type!r} for text content: {declared} documents are"
                " uploaded as content_base64",
            )
        data = _decode_base64(body.content_base64)
        if not data:
            raise _reject(status.HTTP_422_UNPROCESSABLE_CONTENT, "content_base64 is empty")
        cap = limits.max_bytes_for(spec.cap_class, text_cap)
        if len(data) > cap:
            raise _reject(
                status.HTTP_413_CONTENT_TOO_LARGE,
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
        raise _reject(status.HTTP_413_CONTENT_TOO_LARGE, too_large_detail(text_cap))
    if body.content_base64 is not None:
        data = _decode_base64(body.content_base64)
        detected = detect_binary_type(data[:16])
        if detected is not None:
            raise _unsupported_content(declared, detected)
        raise _reject(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{declared} documents are uploaded as content (UTF-8 text), not content_base64",
        )
    content = body.content or ""
    if not content.strip():
        raise _reject(status.HTTP_422_UNPROCESSABLE_CONTENT, "content is empty")
    cap = limits.max_bytes_for(spec.cap_class, text_cap)
    size = len(content.encode("utf-8"))
    if size > cap:
        raise _reject(
            status.HTTP_413_CONTENT_TOO_LARGE, f"the document is too large ({size} bytes; max {cap} bytes for {declared})"
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


def _json(status_code: int, content: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(content))


# ------------------------------------------------------------------------ ingest run hand-off

# A document left 'pending' whose recorded run does not exist is orphaned (its request crashed, or
# its run_id clashed after the write) only after this long: a younger one belongs to a request that
# is still between its document write and its run insert.
ORPHAN_AFTER_SECONDS = 60
ENQUEUE_FAILED_DETAIL = "Run recorded but could not be started; retry the same request (same run_id)"
REINDEX_ENQUEUE_FAILED_DETAIL = (
    "Reindex could not be started and was cancelled; retry the request (without run_id, or with a new one)"
)


class HandOffFailed(Exception):
    """The enqueue of a recorded run failed: the route answers 503 {"detail", "run_id"}."""

    def __init__(self, run_id: str, detail: str = ENQUEUE_FAILED_DETAIL) -> None:
        self.run_id = run_id
        self.detail = detail
        super().__init__(run_id, detail)

    def response(self) -> JSONResponse:
        return _json(status.HTTP_503_SERVICE_UNAVAILABLE, {"detail": self.detail, "run_id": self.run_id})


def _create_ingest_run(
    run_store: RunStore, tenant_id: str, kb_id: str, input_json: str, run_id: uuid.UUID | str
) -> tuple[dict[str, Any], bool]:
    """Create (or find, for a retry with the same run_id) the kb_ingest run. 409 when `run_id` is
    another tenant's or another request's run (callers resolve run_id before writing; this is the
    last guard against a race)."""
    run_uuid = uuid.UUID(str(run_id))
    trace_id, traceparent = current_trace_context()
    try:
        _, created = run_store.create_kb_ingest_run(
            tenant_id, kb_id, input_json, run_uuid, trace_id=trace_id, traceparent=traceparent
        )
    except RunIdConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
    run = run_store.get_run(tenant_id, run_uuid)
    if run is None:  # pragma: no cover - deleted between insert and read
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if not created and not _is_run_of(run, kb_id, input_json):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
        )
    return run, created


def _is_run_of(run: dict[str, Any] | None, kb_id: str, input_json: str) -> bool:
    return (
        run is not None
        and run.get("kind") == "kb_ingest"
        and str(run.get("knowledge_base_id")) == str(kb_id)
        and _same_ingest_input(run.get("input"), input_json)
    )


def _run_input(run: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(run.get("input") or "")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_document_run(run: dict[str, Any], kb_id: str, document_id: str) -> bool:
    """A kb_ingest run of any version of this document."""
    return (
        run.get("kind") == "kb_ingest"
        and str(run.get("knowledge_base_id")) == str(kb_id)
        and str(_run_input(run).get("document_id")) == str(document_id)
    )


def _enqueue_failed(run: dict[str, Any] | None) -> bool:
    return run is not None and run["status"] == "failed" and run["error"] == ENQUEUE_FAILED_ERROR


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    run_status, display_status = run["status"], run["display_status"]
    if run["error"] == ENQUEUE_FAILED_ERROR and run["status"] != "failed":  # pragma: no cover - defensive
        run_status = display_status = "queued"
    return {
        "run_id": str(run["id"]),
        "kind": "kb_ingest",
        "knowledge_base_id": str(run["knowledge_base_id"]),
        "status": run_status,
        "display_status": display_status,
        "trace_id": run.get("trace_id"),
    }


def _hand_off(
    run_store: RunStore,
    launcher: RunLauncher,
    tenant_id: str,
    run: dict[str, Any],
    created: bool,
    failed_detail: str = ENQUEUE_FAILED_DETAIL,
) -> dict[str, Any]:
    """Enqueue `eap.kb_ingest` for the stored run, exactly as tool runs do: this request owns the
    hand-off of a row it created, or of one an earlier failed hand-off left failed (re-queued now).
    A failed enqueue marks an owned run failed (ENQUEUE_FAILED_ERROR) and raises HandOffFailed
    (503 with the run_id)."""
    run_id = uuid.UUID(str(run["id"]))
    owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)
    try:
        launcher.launch_kb_ingest(
            run_id=run_id,
            tenant_id=tenant_id,
            knowledge_base_id=str(run["knowledge_base_id"]),
            input_json=run["input"],
            traceparent=run.get("traceparent"),
        )
    except Exception as exc:
        # Type and code only: the error can quote the DBOS database URL or the enqueued payload.
        log_failure(
            log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
            run_id=str(run_id), tenant_id=tenant_id,
        )
        if owns_handoff:
            try:
                run_store.mark_enqueue_failed(tenant_id, run_id)
            except Exception as mark_exc:  # noqa: BLE001 - the 503 below is the answer either way
                log_failure(
                    log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                    level=logging.ERROR, run_id=str(run_id), tenant_id=tenant_id,
                )
        raise HandOffFailed(str(run_id), failed_detail) from None
    run = run_store.get_run(tenant_id, run_id) or run
    view = _run_view(run)
    if run["error"] == ENQUEUE_FAILED_ERROR:
        # A concurrent owner's enqueue failed and marked it, but this enqueue succeeded.
        view["status"] = view["display_status"] = "queued"
    log.info("kb_ingest run %s queued: tenant=%s kb=%s", run_id, tenant_id, run["knowledge_base_id"])
    return view


def _replay(run_store: RunStore, launcher: RunLauncher, tenant_id: str, run: dict[str, Any]) -> dict[str, Any]:
    """A retry of the request that created `run`: hand it off again unless it has finished. The
    enqueue is idempotent (DBOS returns the existing workflow of this workflow id), and it recovers
    a run whose creating request died before its enqueue. A finished run is answered as recorded."""
    if _enqueue_failed(run) or run["status"] not in TERMINAL_RUN_STATUSES:
        return _hand_off(run_store, launcher, tenant_id, run, created=False)
    return _run_view(run)


def _stale_queued(run: dict[str, Any]) -> bool:
    """Still 'queued' ORPHAN_AFTER_SECONDS after it was created: possibly never enqueued (its request
    died between the run insert and the enqueue). Handing it off again is idempotent."""
    created = run.get("created_at")
    if run.get("status") != "queued" or not isinstance(created, datetime):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() > ORPHAN_AFTER_SECONDS


def _renew_claim(store: KnowledgeStore, tenant_id: str, document: dict[str, Any], run_id: uuid.UUID | str) -> bool:
    """Compare-and-set `last_run_id` onto itself (refreshing `updated_at`) right before creating
    the run: False when a newer upload or an orphan recovery took the document over while this
    request stalled, so it must not create a second run. A deleted document passes (its run ends
    cancelled in the worker)."""
    if store.attach_ingest_run(
        tenant_id, document["id"], str(run_id),
        doc_version=document["latest_version"], expected_last_run_id=str(run_id),
    ):
        return True
    return store.get_document_by_id(tenant_id, document["id"]) is None


def _resolve_client_run_id(
    store: KnowledgeStore, run_store: RunStore, tenant_id: str, run_id: uuid.UUID | None
) -> dict[str, Any] | None:
    """Resolve a client run_id BEFORE anything is written: None when unused, the tenant's run when
    it exists, 409 when another tenant holds it."""
    if run_id is None:
        return None
    with store.engine.connect() as conn:
        owner = conn.execute(text("SELECT tenant_id FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar()
    if owner is None:
        return None
    if owner != tenant_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
    return run_store.get_run(tenant_id, run_id)


def _latest_run(run_store: RunStore, tenant_id: str, document: dict[str, Any]) -> dict[str, Any] | None:
    """The document's recorded ingest run when it exists and ingests the latest version, else None."""
    last_run_id = document.get("last_run_id")
    if not last_run_id:
        return None
    try:
        run = run_store.get_run(tenant_id, uuid.UUID(str(last_run_id)))
    except ValueError:
        return None
    if not _is_run_of(run, document["kb_id"], ingest_input(document["id"], document["latest_version"])):
        return None
    return run


def _orphaned(document: dict[str, Any]) -> bool:
    """'pending' for more than ORPHAN_AFTER_SECONDS (call only when its recorded run is missing)."""
    updated = document.get("updated_at")
    if document.get("status") != "pending" or not isinstance(updated, datetime):
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds() > ORPHAN_AFTER_SECONDS



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


# ------------------------------------------------------------------------- embedding models


@router.get("/embedding-models", response_model=EmbeddingModelListResponse)
def list_embedding_models(principal: PrincipalDep) -> dict[str, Any]:
    """The embedding models a knowledge base may use (EAP_ALLOWED_EMBEDDING_MODELS): every concrete
    model with its dimensions, then `provider:*=<dims>` wildcards (`name` "*", `wildcard` true; any
    name of that provider at those dimensions). Platform-wide; authentication is still required."""
    del principal
    policy = _policy()
    items: list[dict[str, Any]] = [
        {"id": m.id, "provider": m.provider, "name": m.name, "dimensions": m.dimensions, "wildcard": False}
        for m in policy.listed()
    ]
    for provider, dims in sorted(policy.wildcards.items()):
        if dims is not None:
            items.append({"id": f"{provider}:*", "provider": provider, "name": "*", "dimensions": dims, "wildcard": True})
    return {"items": items}


# ------------------------------------------------------------------------------ knowledge bases


@_kb.get("", response_model=KnowledgeBaseListResponse)
def list_knowledge_bases(principal: PrincipalDep, store: KnowledgeDep) -> dict[str, Any]:
    """The tenant's knowledge bases by name, with `document_count` and `chunk_count`."""
    return {"items": store.list_knowledge_bases(principal.tenant_id)}


@_kb.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=KnowledgeBaseResponse,
    responses={409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def create_knowledge_base(body: KnowledgeBaseCreate, principal: PrincipalDep, store: KnowledgeDep) -> dict[str, Any]:
    """Create a knowledge base. 422 embedding model not allowed (or its dimensions unknown), or an
    invalid `chunking`; 409 the tenant already has a knowledge base of that name. A new knowledge
    base uses chunker v2 unless `chunking.chunker_version` is 1 (RAG P1)."""
    model = _resolve_model(body.embedding_model)
    try:
        kb = store.create_knowledge_base(
            principal.tenant_id,
            name=body.name,
            description=body.description,
            embedding_model=model.id,
            dimensions=model.dimensions,
            chunking=body.chunking_config(),  # new_knowledge_base_chunking: chunker v2 by default
        )
    except KnowledgeBaseNameConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {**kb, "document_count": 0, "chunk_count": 0}


@_kb.get("/{kb_id}", response_model=KnowledgeBaseDetailResponse)
def get_knowledge_base(kb_id: str, principal: PrincipalDep, store: KnowledgeDep) -> dict[str, Any]:
    """The knowledge base with its counts and its ingest spend so far (`ingest_tokens`,
    `ingest_cost_usd`, `ingest_requests`: see KnowledgeBaseDetailResponse)."""
    kb = _kb_or_404(store, principal.tenant_id, kb_id)
    return {**kb, **ingest_totals(store, principal.tenant_id, kb["id"])}


@_kb.patch("/{kb_id}", response_model=KnowledgeBaseResponse, responses={409: {"model": ErrorDetail}})
def update_knowledge_base(
    kb_id: str, body: KnowledgeBaseUpdate, principal: PrincipalDep, store: KnowledgeDep
) -> dict[str, Any]:
    """Rename and/or set the description (null clears it). 409 name already used."""
    fields: dict[str, Any] = {}
    if "name" in body.model_fields_set:
        fields["name"] = body.name
    if "description" in body.model_fields_set:
        fields["description"] = body.description
    try:
        store.update_knowledge_base(principal.tenant_id, kb_id, **fields)
    except KnowledgeBaseNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
    except KnowledgeBaseNameConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return _kb_or_404(store, principal.tenant_id, kb_id)


@_kb.delete(
    "/{kb_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": KnowledgeBaseInUse}},
)
def delete_knowledge_base(
    kb_id: str,
    principal: PrincipalDep,
    store: KnowledgeDep,
    force: Annotated[bool, Query()] = False,
) -> Response:
    """Delete the knowledge base; its documents and chunks cascade, its runs stay. 409
    {detail, agents: [{agent_id, agent_name, version, archived, draft}]} when an agent's latest
    published version or current draft attaches it (`draft` true and `version` null when only the
    draft does), unless `force=true` (runs of such versions and drafts then get a tool error). An
    in-flight ingest run ends failed (`ingest_cancelled`) when it finds the knowledge base gone."""
    tenant_id = principal.tenant_id
    kb = _kb_or_404(store, tenant_id, kb_id)
    if not force:
        agents = referencing_agents(store, tenant_id, kb["id"])
        if agents:
            names = ", ".join(a["agent_name"] for a in agents[:5]) + (" …" if len(agents) > 5 else "")
            return _json(
                status.HTTP_409_CONFLICT,
                {
                    "detail": (
                        f"knowledge base is attached to {len(agents)} agent(s) ({names}); "
                        "detach it or delete with force=true"
                    ),
                    "agents": agents,
                },
            )
    if not _retry_deadlock_once(lambda: store.delete_knowledge_base(tenant_id, kb["id"])):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------------ documents


@_kb.get("/{kb_id}/documents", response_model=KnowledgeDocumentListResponse)
def list_documents(
    kb_id: str,
    principal: PrincipalDep,
    store: KnowledgeDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    after: Annotated[
        str | None,
        Query(max_length=MAX_CURSOR_LENGTH, description="Opaque cursor: the previous page's next_after, verbatim"),
    ] = None,
) -> dict[str, Any]:
    """The knowledge base's documents (without content) by document_key, `limit` at a
    time (1–200, default 50). `next_after` is an opaque cursor when a full page came back (pass it
    back verbatim as `after`), else null. A cursor this API did not issue is a 422."""
    kb = _kb_or_404(store, principal.tenant_id, kb_id)
    after_key = None
    if after is not None:
        try:
            after_key = decode_document_cursor(after)
        except InvalidDocumentCursor:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="after is not a valid cursor; pass the previous page's next_after verbatim",
            )
    items = store.list_documents(principal.tenant_id, kb["id"], limit=limit, after=after_key)
    next_after = encode_document_cursor(items[-1]["document_key"]) if len(items) == limit else None
    return {"items": [document_view(d) for d in items], "next_after": next_after}


@_kb.get("/{kb_id}/documents/{document_key}", response_model=KnowledgeDocumentResponse)
def get_document(
    kb_id: str,
    document_key: str,
    principal: PrincipalDep,
    store: KnowledgeDep,
    include_content: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """The document, with `extraction` (the worker extraction of its ACTIVE version: extractor, versions, pages,
    image-only pages, sections, warnings; null for API-extracted documents). `include_content=true`
    adds `content`, except for a binary document (`content_encoding` "base64"): its `content` is
    always null, and the stored bytes are never returned."""
    tenant_id = principal.tenant_id
    kb = _kb_or_404(store, tenant_id, kb_id)
    document = store.get_document(tenant_id, kb["id"], document_key)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOCUMENT_NOT_FOUND)
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


@_upload.put(
    "/{kb_id}/documents/{document_key}",
    response_model=DocumentPutResponse,
    responses={
        202: {"model": DocumentPutResponse, "description": "Stored as a new version; an ingest run started"},
        409: {"model": ErrorDetail},
        413: {"model": ErrorDetail},
        415: {"model": ErrorDetail},
        503: {"model": EnqueueFailedResponse},
    },
)
def put_document(
    kb_id: str,
    document_key: str,
    principal: PrincipalDep,
    body: DocumentPut,
    body_size: BodySizeDep,
    store: KnowledgeDep,
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Upload a document: UTF-8 text as `content`, or a binary type (application/pdf) as
    `content_base64`. A sync route: FastAPI runs it (and its extraction) in the threadpool, off the
    event loop.

    RAG P1. The MIME type comes from the format registry (aliases are stored as the canonical type).
    Text formats the API extracts (Markdown, HTML, plain text; Markdown only while the knowledge
    base's `chunking.extract.markdown_flavor` is not `doxygen`) are extracted here as before; the
    worker-extracted ones (reStructuredText, PDF, YAML, JSON, Doxygen Markdown) are never parsed by
    the API, only size-checked and sniffed (see prepare_upload for the 413/415/422 rules). A PDF is
    stored as base64 and its `content_hash` is the sha256 of its bytes, so re-uploading the same
    bytes is `unchanged`.

    200 `{unchanged: true, run_id: null}`: same content, metadata, title, source URL and MIME type,
    nothing written and no run. Exception: when the version's recorded run is still `queued` more
    than ORPHAN_AFTER_SECONDS after it was created (a lagging queue, or a request that died before
    its enqueue), an identical upload hands that run off again and answers 202 with its existing
    run_id (`unchanged: false`); no new run is created and nothing is written. 202 `{unchanged: false, created, document, run_id, …}`: stored as
    `document.latest_version` and a kb_ingest run started. 413 body over EAP_KB_MAX_DOCUMENT_BYTES;
    415 not UTF-8 or a NUL byte; 422 invalid key, empty content, unsupported MIME type, content that
    cannot be extracted or extracts to no text ("the document has no extractable text"), bad
    metadata or source URL; 409 the 2,000 document cap, or `run_id` used by another tenant, another
    document or another kind of run (nothing is written).

    `run_id` is resolved before anything is written: the id of an earlier ingest run of this
    document is a replay (nothing is written; the recorded run is returned, and handed off again if
    its enqueue failed). 503 {detail, run_id}: the run could not be enqueued; it is marked failed,
    and retrying the same request (same run_id, or the same upload without one) hands it off again.
    """
    tenant_id = principal.tenant_id
    _check_document_key(document_key)
    if body.document_key is not None and body.document_key != document_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="document_key in the body must match the path"
        )
    with binary_upload_slot(_spec_or_none(body.mime_type)):
        return _put_document(kb_id, document_key, tenant_id, body, body_size, store, stores, launcher)


def _put_document(
    kb_id: str,
    document_key: str,
    tenant_id: str,
    body: DocumentPut,
    body_size: int,
    store: KnowledgeStore,
    stores: Any,
    launcher: RunLauncher,
) -> JSONResponse:
    """put_document's body, run while holding a binary-upload slot for binary formats."""
    try:
        upload = prepare_upload(body, body_size)
    except UploadRejected as rejected:
        return rejected.response()
    kb = _kb_or_404(store, tenant_id, kb_id)  # before extraction: no CPU for a missing or foreign KB
    title = (body.title or "").strip() or document_key
    if extractor_for(upload.mime_type, kb_extract_config(kb)) is None:
        # API-extracted (unchanged path): reject unsafe HTML or a document with no text early.
        try:
            extracted = extract(upload.content, upload.mime_type, title=title, source_url=body.source_url)
        except UnsupportedMimeType as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
        except ExtractionFailed as exc:
            # The fixed message only: the exception's detail may name parser internals.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{exc.user_message} ({exc.code}: {exc.reason})",
            )
        if not _has_text(extracted):
            # Only dropped markup (script, style, nav, comments): nothing to chunk, so nothing is stored.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="the document has no extractable text"
            )
    run_store, _ = stores
    try:
        existing = _resolve_client_run_id(store, run_store, tenant_id, body.run_id)
        if existing is not None:
            document = store.get_document(tenant_id, kb["id"], document_key)
            if (
                document is None
                or not _is_document_run(existing, kb["id"], document["id"])
                or _run_input(existing).get("doc_version") != document["latest_version"]
                or document["content_hash"] != upload.content_hash
            ):
                # Not a retry of the request that stored the current version: refuse, write nothing.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
                )
            run = _replay(run_store, launcher, tenant_id, existing)
            created = _run_input(existing).get("doc_version") == 1
            return _json(
                status.HTTP_202_ACCEPTED,
                {"unchanged": False, "created": created, "document": document_view(document), **run},
            )

        new_run_id = body.run_id or uuid.uuid4()
        try:
            result = store.put_document(
                tenant_id,
                kb["id"],
                document_key=document_key,
                title=title,
                content=upload.content,
                mime_type=upload.mime_type,
                source_url=body.source_url,
                metadata=body.metadata,
                run_id=str(new_run_id),  # last_run_id, in the same transaction as the new version
                content_sha256=upload.digest,
            )
        except KnowledgeBaseNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
        except KnowledgeBaseLimitExceeded as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        except ValueError as exc:  # metadata the store's validator refuses (the body model checks it first)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
        document = result.document

        if not result.unchanged:
            if not _renew_claim(store, tenant_id, document, new_run_id):
                # Stalled past ORPHAN_AFTER_SECONDS and taken over: the version is stored and its
                # ingest belongs to the request that took over.
                current = store.get_document(tenant_id, kb["id"], document_key) or document
                return _json(
                    status.HTTP_202_ACCEPTED,
                    {"unchanged": False, "created": result.created, "document": document_view(current),
                     "run_id": current["last_run_id"]},
                )
            input_json = ingest_input(document["id"], document["latest_version"])
            run, created = _create_ingest_run(run_store, tenant_id, kb["id"], input_json, new_run_id)
            view = _hand_off(run_store, launcher, tenant_id, run, created)
            return _json(
                status.HTTP_202_ACCEPTED,
                {"unchanged": False, "created": result.created, "document": document_view(document), **view},
            )

        latest = _latest_run(run_store, tenant_id, document)
        if latest is not None and (_enqueue_failed(latest) or _stale_queued(latest)):
            # A retry of an upload whose hand-off failed, or whose request died before enqueuing:
            # hand the recorded run off again (idempotent in DBOS).
            view = _hand_off(run_store, launcher, tenant_id, latest, created=False)
        elif latest is None and _orphaned(document) and store.attach_ingest_run(
            tenant_id,
            document["id"],
            str(new_run_id),
            doc_version=document["latest_version"],
            expected_last_run_id=document["last_run_id"],
            expected_updated_at=document["updated_at"],  # a stalled owner's lease renewal makes this lose
        ):
            # Stored long ago by a request that never created its run: this upload won the
            # compare-and-set on last_run_id and starts the run the version is waiting for.
            input_json = ingest_input(document["id"], document["latest_version"])
            run, created = _create_ingest_run(run_store, tenant_id, kb["id"], input_json, new_run_id)
            view = _hand_off(run_store, launcher, tenant_id, run, created)
        else:
            return _json(
                status.HTTP_200_OK,
                {"unchanged": True, "created": False, "document": document_view(document), "run_id": None},
            )
    except HandOffFailed as exc:
        return exc.response()
    document = store.get_document(tenant_id, kb["id"], document_key) or document
    return _json(
        status.HTTP_202_ACCEPTED,
        {"unchanged": False, "created": document["latest_version"] == 1, "document": document_view(document), **view},
    )


@_kb.delete("/{kb_id}/documents/{document_key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(kb_id: str, document_key: str, principal: PrincipalDep, store: KnowledgeDep) -> Response:
    """Delete the document; its chunks cascade. An in-flight ingest run ends failed (`ingest_cancelled`)."""
    tenant_id = principal.tenant_id
    kb = _kb_or_404(store, tenant_id, kb_id)
    if not _retry_deadlock_once(lambda: store.delete_document(tenant_id, kb["id"], document_key)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOCUMENT_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@_kb.post(
    "/{kb_id}/documents/{document_key}/reingest",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestRunResponse,
    responses={
        200: {"model": IngestRunResponse, "description": "Already ingested: no run (`run_id` null)"},
        409: {"model": ErrorDetail},
        503: {"model": EnqueueFailedResponse},
    },
)
def reingest_document(
    kb_id: str,
    document_key: str,
    principal: PrincipalDep,
    store: KnowledgeDep,
    stores: StoresDep,
    launcher: LauncherDep,
    body: Annotated[IngestRunCreate | None, Body()] = None,
) -> JSONResponse:
    """Start a new ingest run of the document's latest version (the "Retry" of a failed ingest;
    embeddings already written are reused).

    200 `{run_id: null, unchanged: true, document}`: the latest version is already active and
    ready, nothing to do. 409 while an ingest run of that version is queued, running or starting,
    or when a concurrent reingest won. Idempotent by the optional `run_id` (resolved before any
    write). 503 {detail, run_id}: retry with the same run_id (or none) to hand it off again."""
    tenant_id = principal.tenant_id
    run_store, _ = stores
    run_id = body.run_id if body is not None else None
    kb = _kb_or_404(store, tenant_id, kb_id)
    document = store.get_document(tenant_id, kb["id"], document_key)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOCUMENT_NOT_FOUND)
    input_json = ingest_input(document["id"], document["latest_version"])
    try:
        existing = _resolve_client_run_id(store, run_store, tenant_id, run_id)
        if existing is not None:
            if not _is_run_of(existing, kb["id"], input_json):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
                )
            view = _replay(run_store, launcher, tenant_id, existing)
        elif document["status"] == "ready" and document["active_version"] == document["latest_version"]:
            return _json(
                status.HTTP_200_OK,
                {"run_id": None, "unchanged": True, "knowledge_base_id": kb["id"], "document": document_view(document)},
            )
        else:
            latest = _latest_run(run_store, tenant_id, document)
            if latest is not None and (_enqueue_failed(latest) or _stale_queued(latest)):
                view = _hand_off(run_store, launcher, tenant_id, latest, created=False)
            elif latest is not None and latest["status"] not in TERMINAL_RUN_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"the document is already being ingested (run {latest['id']} is {latest['status']})",
                )
            elif latest is None and document["last_run_id"] and not _orphaned(document) and document["status"] == "pending":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"an ingest run of the document is being started (run {document['last_run_id']})",
                )
            else:
                new_run_id = run_id or uuid.uuid4()
                try:
                    document = store.request_reingest(
                        tenant_id,
                        kb["id"],
                        document_key,
                        run_id=str(new_run_id),
                        expected_last_run_id=document["last_run_id"],
                        doc_version=document["latest_version"],
                    )
                except DocumentNotFound:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOCUMENT_NOT_FOUND)
                except IngestRunConflict:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="another ingest run of the document was started concurrently",
                    )
                if not _renew_claim(store, tenant_id, document, new_run_id):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="another ingest run of the document was started concurrently",
                    )
                run, created = _create_ingest_run(run_store, tenant_id, kb["id"], input_json, new_run_id)
                view = _hand_off(run_store, launcher, tenant_id, run, created)
    except HandOffFailed as exc:
        return exc.response()
    document = store.get_document(tenant_id, kb["id"], document_key) or document
    return _json(status.HTTP_202_ACCEPTED, {**view, "unchanged": False, "document": document_view(document)})


# --------------------------------------------------------------------------- re-extract (RAG P1.A4)

REEXTRACT_ENQUEUE_FAILED_DETAIL = (
    "the ingest run was recorded but could not be started; retry it with POST "
    "/knowledge-bases/{id}/documents/{key}/reingest"
)


@_kb.post(
    "/{kb_id}/documents/{document_key}/reextract",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestRunResponse,
    responses={409: {"model": ErrorDetail}, 503: {"model": EnqueueFailedResponse}},
)
def reextract_document(
    kb_id: str,
    document_key: str,
    principal: PrincipalDep,
    store: KnowledgeDep,
    stores: StoresDep,
    launcher: LauncherDep,
    body: Annotated[IngestRunCreate | None, Body()] = None,
) -> JSONResponse:
    """Re-extract the document: a new version with the SAME content (`latest_version` + 1) and an
    ingest run of it, so the current extractor and the knowledge base's current chunker produce its
    chunks (chunk texts that did not change reuse their embeddings). Any format: an API-extracted
    document is re-chunked.

    202 {run_id, status, …, document}. Idempotent by the optional `run_id`, resolved before any
    write: the run of this document's latest version is a replay (handed off again if its enqueue
    failed); any other run is 409, and another tenant's is 409 too. 404 unknown knowledge base or
    document (another tenant's included). 503 {detail, run_id}: retry with the same run_id.

    Concurrency: the new version is a compare-and-set on the version this request read, so of
    concurrent submits only those whose bump applied answer 202, each with its own run; a request
    that lost answers 409 and wrote nothing. Concurrent submits with ONE `run_id` make one version
    and one run: a loser that finds the document's `last_run_id` already equal to its `run_id`
    answers that run (202), created or found idempotently by the run id."""
    tenant_id = principal.tenant_id
    run_store, _ = stores
    run_id = body.run_id if body is not None else None
    kb = _kb_or_404(store, tenant_id, kb_id)
    document = store.get_document(tenant_id, kb["id"], document_key)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOCUMENT_NOT_FOUND)
    try:
        existing = _resolve_client_run_id(store, run_store, tenant_id, run_id)
        if existing is not None and _is_document_run(existing, kb["id"], document["id"]):
            # The document read above can predate the version this run_id made (a concurrent
            # duplicate of the same request): compare against the document as it is now.
            document = store.get_document(tenant_id, kb["id"], document_key) or document
        if existing is not None:
            if not _is_run_of(existing, kb["id"], ingest_input(document["id"], document["latest_version"])):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
                )
            view = _reextract_replay(run_store, launcher, tenant_id, existing, created=False)
        else:
            new_run_id = run_id or uuid.uuid4()
            try:
                document = store.new_document_version(
                    tenant_id, kb["id"], document_key, run_id=str(new_run_id),
                    expected_latest_version=document["latest_version"],
                )
            except DocumentNotFound:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOCUMENT_NOT_FOUND)
            except IngestRunConflict:
                current = store.get_document(tenant_id, kb["id"], document_key)
                if current is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOCUMENT_NOT_FOUND)
                if current["last_run_id"] != str(new_run_id):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="another upload or re-extract of the document was started concurrently",
                    )
                # A concurrent duplicate with this run_id already made the version: answer its run.
                document = current
            # No _renew_claim here: the bump itself was the compare-and-set and recorded the run id,
            # so the run of the version this request made is always created (a later version
            # supersedes it in the worker); a crash before that is the 60 s orphan rule's case.
            input_json = ingest_input(document["id"], document["latest_version"])
            run, created = _create_ingest_run(run_store, tenant_id, kb["id"], input_json, new_run_id)
            view = _reextract_replay(run_store, launcher, tenant_id, run, created=created)
    except HandOffFailed as exc:
        return exc.response()
    document = store.get_document(tenant_id, kb["id"], document_key) or document
    return _json(status.HTTP_202_ACCEPTED, {**view, "unchanged": False, "document": document_view(document)})


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


@_kb.post(
    "/{kb_id}/reextract",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReextractResponse,
    responses={
        200: {"model": ReextractResponse, "description": "dry_run, or nothing selected: no run started"},
        422: {"model": ErrorDetail},
    },
)
def reextract_knowledge_base(
    kb_id: str,
    body: ReextractCreate,
    principal: PrincipalDep,
    store: KnowledgeDep,
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Re-extract many documents (see ReextractCreate): each selected document gets a new version
    with the same content and its own ingest run on the `eap-kb-ingest` queue, which runs at most
    EAP_KB_INGEST_CONCURRENCY of them at a time. Embeddings of unchanged chunk texts are reused, so
    the cost is the changed chunks only.

    With `chunker_version: 2` on a v1 knowledge base, the chunking config's `chunker_version` is
    rewritten and EVERY document gets its new version in the same transaction (all or nothing).

    202 when at least one run started, else 200 (also for `dry_run`). A run whose enqueue failed is
    listed with `status` "failed" and a `detail` (its document stays `pending`; POST …/reingest
    starts it again), and the call goes on with the others. 422 unknown format names. 404 unknown
    knowledge base (another tenant's included)."""
    tenant_id = principal.tenant_id
    run_store, _ = stores
    kb = _kb_or_404(store, tenant_id, kb_id)
    formats: set[str] | None = None
    if body.formats is not None:
        formats = set()
        for name in body.formats:
            try:
                formats.add(format_by_name(name).format)
            except KeyError:
                expected = ", ".join(spec.format for spec in FORMAT_SPECS)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"unknown format {name!r}: expected one of {expected}",
                )
    current_chunker = int((kb.get("chunking") or {}).get("chunker_version", 1))
    change_chunker = body.chunker_version is not None and body.chunker_version != current_chunker

    if change_chunker:
        selected = store.list_documents(tenant_id, kb["id"])
    elif body.only_outdated:
        selected = store.outdated_extraction_documents(tenant_id, kb["id"], outdated_extractor_map(kb, formats))
    else:
        selected = [
            d for d in store.list_documents(tenant_id, kb["id"])
            if formats is None or ((spec := _spec_or_none(d["mime_type"])) is not None and spec.format in formats)
        ]

    def answer(code: int, refs: list[dict[str, Any]], *, chunker: int, changed: bool, started: int, failed: int,
               dry_run: bool = False) -> JSONResponse:
        return _json(
            code,
            {
                "knowledge_base_id": kb["id"],
                "dry_run": dry_run,
                "chunker_version": chunker,
                "chunker_version_changed": changed,
                "selected": len(refs),
                "started": started,
                "failed": failed,
                "documents": refs,
                "knowledge_base": _kb_or_404(store, tenant_id, kb["id"]),
            },
        )

    if body.dry_run or (not selected and not change_chunker):
        return answer(
            status.HTTP_200_OK, [_document_ref(d) for d in selected],
            chunker=body.chunker_version or current_chunker if body.dry_run else current_chunker,
            changed=False, started=0, failed=0, dry_run=body.dry_run,
        )

    try:
        updated_kb, bumped, changed = store.start_reextract(
            tenant_id,
            kb["id"],
            document_ids=None if change_chunker else [d["id"] for d in selected],
            chunker_version=body.chunker_version if change_chunker else None,
        )
    except KnowledgeBaseNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
    except ValidationError as exc:  # the stored config cannot take chunker v2 (it always can today)
        msg = exc.errors()[0]["msg"] if exc.errors() else "invalid"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"the knowledge base cannot move to chunker v{body.chunker_version}: {msg}",
        )

    refs: list[dict[str, Any]] = []
    started = failed = 0
    for document in bumped:
        run_id = document["last_run_id"]
        input_json = ingest_input(document["id"], document["latest_version"])
        try:
            run, created = _create_ingest_run(run_store, tenant_id, kb["id"], input_json, run_id)
            view = _hand_off(run_store, launcher, tenant_id, run, created)
        except Exception as exc:  # noqa: BLE001 - one document's failure must not strand the others
            # Its new version is stored with last_run_id = run_id: a reingest of the document starts
            # (or re-queues) that run, also when the run row was never written (the orphan rule).
            if not isinstance(exc, (HandOffFailed, HTTPException)):
                log_failure(
                    log, "re-extract run start failed", exc, code="reextract_start_failed", level=logging.ERROR,
                    tenant_id=tenant_id, run_id=str(run_id),
                )
            failed += 1
            refs.append(_document_ref(
                document, run_id=str(run_id), status="failed",
                detail=str(exc.detail) if isinstance(exc, HTTPException) else REEXTRACT_ENQUEUE_FAILED_DETAIL,
            ))
            continue
        started += 1
        refs.append(_document_ref(document, run_id=view["run_id"], status=view["status"]))
    chunker = int((updated_kb.get("chunking") or {}).get("chunker_version", 1))
    return answer(
        status.HTTP_202_ACCEPTED if started else status.HTTP_200_OK, refs,
        chunker=chunker, changed=changed, started=started, failed=failed,
    )


# -------------------------------------------------------------------------------------- reindex


@_kb.post(
    "/{kb_id}/reindex",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestRunResponse,
    responses={
        200: {"model": ReindexEstimateResponse, "description": "dry_run: the estimate, nothing started"},
        409: {"model": ErrorDetail},
        422: {"model": ErrorDetail},
        503: {"model": EnqueueFailedResponse},
    },
)
def reindex_knowledge_base(
    kb_id: str,
    body: ReindexCreate,
    principal: PrincipalDep,
    store: KnowledgeDep,
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Re-embed the knowledge base with `embedding_model` into a new generation; retrieval keeps the
    current one until the run switches atomically.

    `dry_run: true` answers 200 with the chunks and approximate tokens it would embed (distinct
    texts), whether that exceeds EAP_KB_MAX_INGEST_TOKENS, and the estimated cost when the API can
    price the model. Otherwise 202 with the kb_ingest run (`input` {"reindex": generation}). 422
    model not allowed; 409 a reindex is already pending (DELETE …/reindex cancels it), or `run_id`
    is not this pending reindex's run with this model. After a failed reindex (status
    `reindex_failed`), the same request with the pending model retries it.

    Recovery from 503 {detail, run_id}: when this request opened the generation, the reindex is
    cancelled before answering, so a plain retry (no run_id, or a new one) starts it again; the
    failed run stays as the record. A failed hand-off of a retry of a `reindex_failed` reindex
    leaves that state, and the same retry works again.
    """
    tenant_id = principal.tenant_id
    run_store, _ = stores
    model = _resolve_model(body.embedding_model)
    kb = _kb_or_404(store, tenant_id, kb_id)

    if body.dry_run:
        try:
            estimate = store.estimate_reindex(tenant_id, kb["id"])
        except KnowledgeBaseNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
        cost, source, note = estimate_embedding_cost(model.id, estimate.approx_tokens)
        budget = max_ingest_tokens()
        return _json(
            status.HTTP_200_OK,
            {
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
            },
        )

    def accepted(view: dict[str, Any]) -> JSONResponse:
        return _json(status.HTTP_202_ACCEPTED, {**view, "knowledge_base": _kb_or_404(store, tenant_id, kb["id"])})

    pending = kb["pending_generation"]
    try:
        existing = _resolve_client_run_id(store, run_store, tenant_id, body.run_id)
        if existing is not None:
            if not (
                pending is not None
                and kb["pending_embedding_model"] == model.id
                and _is_run_of(existing, kb["id"], reindex_input(pending))
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="run_id is not the run of this knowledge base's pending reindex with this model",
                )
            if kb["status"] == "reindex_failed" and (
                _enqueue_failed(existing) or existing["status"] not in TERMINAL_RUN_STATUSES
            ):
                # The replay would hand the run off again: claim the failed generation first, so it
                # cannot race a plain retry (or another replay) into a second running reindex.
                if not store.claim_reindex_retry(tenant_id, kb["id"], pending):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="a retry of this reindex was started concurrently, or it was cancelled",
                    )
                try:
                    return accepted(_replay(run_store, launcher, tenant_id, existing))
                except HandOffFailed:
                    store.mark_reindex_failed(tenant_id, kb["id"], pending, "the reindex retry could not be started")
                    raise
            if _enqueue_failed(existing):
                # Its failed hand-off put the generation back to 'reindex_failed'; it is 'reindexing'
                # now, so another retry claimed it and runs it: never start a second run.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="a retry of this reindex was started concurrently, or it was cancelled",
                )
            return accepted(_replay(run_store, launcher, tenant_id, existing))
        if pending is not None and kb["status"] == "reindex_failed" and kb["pending_embedding_model"] == model.id:
            # Retry the failed reindex: a new run of the same pending generation reuses its
            # embeddings. The compare-and-set 'reindex_failed' -> 'reindexing' admits one retry.
            if not store.claim_reindex_retry(tenant_id, kb["id"], pending):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="a retry of this reindex was started concurrently, or it was cancelled",
                )
            try:
                run, created = _create_ingest_run(
                    run_store, tenant_id, kb["id"], reindex_input(pending), body.run_id or uuid.uuid4()
                )
                return accepted(_hand_off(run_store, launcher, tenant_id, run, created))
            except (HTTPException, HandOffFailed):
                # Back to 'reindex_failed', so the same retry can be sent again.
                store.mark_reindex_failed(tenant_id, kb["id"], pending, "the reindex retry could not be started")
                raise

        try:
            opened = store.start_reindex(tenant_id, kb["id"], embedding_model=model.id, dimensions=model.dimensions)
        except KnowledgeBaseNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
        except ReindexInProgress as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{exc}; wait for it, or cancel it with DELETE /knowledge-bases/{{id}}/reindex",
            )
        generation = opened["pending_generation"]
        try:
            run, created = _create_ingest_run(
                run_store, tenant_id, kb["id"], reindex_input(generation), body.run_id or uuid.uuid4()
            )
            return accepted(
                _hand_off(run_store, launcher, tenant_id, run, created, failed_detail=REINDEX_ENQUEUE_FAILED_DETAIL)
            )
        except (HTTPException, HandOffFailed):
            # No run will embed the generation just opened: drop it, so a plain retry can start over.
            _cancel_generation(store, tenant_id, kb["id"], generation)
            raise
    except HandOffFailed as exc:
        return exc.response()


def _cancel_generation(store: KnowledgeStore, tenant_id: str, kb_id: str, generation: int) -> None:
    current = store.get_knowledge_base(tenant_id, kb_id)
    if current is not None and current["pending_generation"] == generation:
        _retry_deadlock_once(lambda: store.cancel_reindex(tenant_id, kb_id))



@_kb.delete("/{kb_id}/reindex", status_code=status.HTTP_204_NO_CONTENT)
def cancel_reindex(kb_id: str, principal: PrincipalDep, store: KnowledgeDep) -> Response:
    """Drop the pending generation and its chunks (status back to `ready`); a running reindex run
    then ends failed (`ingest_cancelled`). 204 also when nothing is pending."""
    kb = _kb_or_404(store, principal.tenant_id, kb_id)
    _retry_deadlock_once(lambda: store.cancel_reindex(principal.tenant_id, kb["id"]))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------ search tester (RM.11)

SEARCH_WAIT_SECONDS = 15.0
SEARCH_POLL_SECONDS = 0.2  # first poll interval, backing off to SEARCH_POLL_MAX_SECONDS
SEARCH_POLL_MAX_SECONDS = 1.0


class KnowledgeSearchRequest(BaseModel):
    """Body of POST /knowledge-bases/{id}/search. `run_id` (optional, client-generated) makes a retry
    idempotent, as for tool runs."""

    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int = 8
    min_score: float | None = None
    filter: dict[str, Any] | None = None
    run_id: uuid.UUID | None = None


def _safe_errors(exc: ValidationError, prefix: list[Any]) -> list[dict[str, Any]]:
    """loc/msg/type only: never the rejected values (a query can hold anything a user typed)."""
    return [
        {"loc": [*prefix, *err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


async def _search_body(request: Request) -> KnowledgeSearchRequest:
    try:
        return KnowledgeSearchRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_safe_errors(exc, ["body"]))


def _search_view(run: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """(status code, body): 200 with the result (or the failure) once the tool run ended, else 202
    with the run id to follow through GET /runs/{id}."""
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
        return status.HTTP_200_OK, view
    if run["status"] in TERMINAL_RUN_STATUSES:
        view["error"] = run.get("error")
        return status.HTTP_200_OK, view
    return status.HTTP_202_ACCEPTED, view


@_kb.post(
    "/{kb_id}/search",
    responses={
        202: {"description": "Still running after the wait: follow GET /runs/{run_id}"},
        409: {"model": ErrorDetail},
        422: {"model": ErrorDetail},
        503: {"model": EnqueueFailedResponse},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": KnowledgeSearchRequest.model_json_schema()}},
        }
    },
)
async def search_knowledge_base(
    kb_id: str,
    request: Request,
    principal: PrincipalDep,  # before the body: 401 wins over 422
    body: Annotated[KnowledgeSearchRequest, Depends(_search_body)],
    store: KnowledgeDep,
    stores: StoresDep,
    launcher: LauncherDep,
    wait: Annotated[float, Query(ge=0.0, le=SEARCH_WAIT_SECONDS)] = SEARCH_WAIT_SECONDS,
) -> JSONResponse:
    """Search one knowledge base the way an agent's `retrieve` tool does (the search tester).

    The API never embeds: this records a tool run of the hidden platform tool `knowledge.search`
    (kind 'tool', `knowledge_base_id` set, so its embedding cost is listed with the knowledge base)
    and enqueues it like POST /tools/{id}/runs, then waits up to `wait` seconds (default and maximum
    15) for it. 200 `{run_id, status: completed, result: {knowledge_base_id, embedding_model, hits:
    [{n, title, url, ref, kb_id, document_id, document_key, chunk_id, doc_version, score,
    document_title, heading_path, metadata, content, content_truncated}], text}}`; 200 with
    `status` failed and `error` when the run failed; 202 `{run_id, status}` while it is still
    running. 404 unknown knowledge base (another tenant's too); 422 invalid body or settings (loc,
    msg and type only); 409 `run_id` already used by another request; 503 hand-off failed (the run
    is marked failed; retry with the same run_id). The wait polls the run from every 0.2 s backing
    off to 1 s, and stops early when the client disconnects (the run itself goes on)."""
    tenant_id = principal.tenant_id
    kb = await asyncio.to_thread(_kb_or_404, store, tenant_id, kb_id)
    try:
        validated = KnowledgeSearchInput.model_validate(
            {
                "knowledge_base_id": kb["id"],
                "query": body.query,
                "top_k": body.top_k,
                "min_score": body.min_score,
                "filter": body.filter,
            }
        )
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_safe_errors(exc, ["body"]))
    input_json = _canonical_json(validated.model_dump(mode="json"))
    run_store, _ = stores
    run_id = body.run_id or uuid.uuid4()
    trace_id, traceparent = current_trace_context()

    def create() -> tuple[dict[str, Any], bool]:
        try:
            _, created = run_store.create_tool_run(
                tenant_id, KNOWLEDGE_SEARCH_TOOL_ID, input_json, run_id,
                trace_id=trace_id, traceparent=traceparent, knowledge_base_id=kb["id"],
            )
        except RunIdConflict:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
        run = run_store.get_run(tenant_id, run_id)
        if run is None:  # pragma: no cover - deleted between insert and read
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        if not created and (
            run["kind"] != "tool" or run["tool_id"] != KNOWLEDGE_SEARCH_TOOL_ID or run["input"] != input_json
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
            )
        return run, created

    run, created = await asyncio.to_thread(create)

    def hand_off() -> None:
        owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)
        if not owns_handoff and run["status"] in TERMINAL_RUN_STATUSES:
            return  # a retry of a finished search: answer what was recorded
        try:
            launcher.launch_tool(
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
                except Exception as mark_exc:  # noqa: BLE001 - the 503 below is the answer either way
                    log_failure(
                        log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                        level=logging.ERROR, run_id=str(run_id), tenant_id=tenant_id,
                    )
            raise HandOffFailed(str(run_id)) from None

    try:
        await asyncio.to_thread(hand_off)
    except HandOffFailed as failed:
        return failed.response()
    log.info("knowledge search run %s queued: tenant=%s kb=%s", run_id, tenant_id, kb["id"])

    deadline = time.monotonic() + wait
    interval = SEARCH_POLL_SECONDS
    while True:
        current = await asyncio.to_thread(run_store.get_run, tenant_id, run_id) or run
        if current["error"] == ENQUEUE_FAILED_ERROR and current["status"] == "failed":
            # A concurrent owner's enqueue failed, but this one succeeded: the workflow exists.
            current = {**current, "status": "queued", "display_status": "queued"}
        code, view = _search_view(current)
        if code == status.HTTP_200_OK or time.monotonic() >= deadline or await request.is_disconnected():
            return _json(code, view)
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        interval = min(SEARCH_POLL_MAX_SECONDS, interval * 1.5)


router.include_router(_upload)
router.include_router(_kb)
