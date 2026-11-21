"""Knowledge bases, their documents, ingest and reindex runs (RAG and memory milestone, RM.7).

Every route is tenant-scoped: the tenant comes from the principal only, and every store call
filters by it in SQL. Another tenant's knowledge base, document or run answers 404, never 403.

Documents are uploaded as JSON text (`PUT /knowledge-bases/{id}/documents/{document_key}`). That
route has its own body cap, EAP_KB_MAX_DOCUMENT_BYTES (default 5 MiB, read per request), and keeps
the request guard's UTF-8, NUL and depth checks. The API extracts the document once (eap_knowledge)
to reject an unsupported MIME type or unsafe HTML early; chunking and embedding happen in the
worker. An upload with the same content, metadata, title, source URL and MIME type is a no-op
(200, `unchanged: true`, no run). A changed one bumps `latest_version` and starts a `kb_ingest` run
(202), enqueued as `eap.kb_ingest` on the partitioned `eap-kb-ingest-fair` queue, keyed by tenant and knowledge base (eap_api.launcher.KB_INGEST_WORKFLOW_NAME
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
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import GuardedJsonRoute, guarded_json_route
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
)
from eap_api.service_deps.knowledge import KnowledgeServiceDep
from eap_api.services.knowledge import (  # noqa: F401  (re-exported: app.py, the guard and tests use these names)
    DEFAULT_KB_MAX_DOCUMENT_BYTES,
    EXTRACTOR_VERSIONS,
    MAX_CURSOR_LENGTH,
    InvalidDocumentCursor,
    check_knowledge_configuration,
    decode_document_cursor,
    document_error_codes,
    document_view,
    encode_document_cursor,
    estimate_embedding_cost,
    max_document_bytes,
    safe_errors,
    upload_guard_bytes,
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]


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


async def _request_body_size(request: Request) -> int:
    """The request body's size in bytes (the guard has read and cached it)."""
    return len(await request.body())


BodySizeDep = Annotated[int, Depends(_request_body_size)]


def _json(status_code: int, content: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(content))


# ------------------------------------------------------------------------------------ embedding models


@router.get("/embedding-models", response_model=EmbeddingModelListResponse)
def list_embedding_models(principal: PrincipalDep, service: KnowledgeServiceDep) -> dict[str, Any]:
    """The embedding models a knowledge base may use (EAP_ALLOWED_EMBEDDING_MODELS): every concrete
    model with its dimensions, then `provider:*=<dims>` wildcards (`name` "*", `wildcard` true; any
    name of that provider at those dimensions). Platform-wide; authentication is still required."""
    del principal
    return {"items": service.embedding_models()}


# ------------------------------------------------------------------------------ knowledge bases


@_kb.get("", response_model=KnowledgeBaseListResponse)
def list_knowledge_bases(principal: PrincipalDep, service: KnowledgeServiceDep) -> dict[str, Any]:
    """The tenant's knowledge bases by name, with `document_count` and `chunk_count`."""
    return {"items": service.list_knowledge_bases(principal.tenant_id)}


@_kb.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=KnowledgeBaseResponse,
    responses={409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def create_knowledge_base(
    body: KnowledgeBaseCreate, principal: PrincipalDep, service: KnowledgeServiceDep
) -> dict[str, Any]:
    """Create a knowledge base. 422 embedding model not allowed (or its dimensions unknown), or an
    invalid `chunking`; 409 the tenant already has a knowledge base of that name. A new knowledge
    base uses chunker v2 unless `chunking.chunker_version` is 1 (RAG P1)."""
    return service.create_knowledge_base(
        principal.tenant_id,
        name=body.name,
        description=body.description,
        embedding_model=body.embedding_model,
        chunking=body.chunking_config(),  # new_knowledge_base_chunking: chunker v2 by default
    )


@_kb.get("/{kb_id}", response_model=KnowledgeBaseDetailResponse)
def get_knowledge_base(kb_id: str, principal: PrincipalDep, service: KnowledgeServiceDep) -> dict[str, Any]:
    """The knowledge base with its counts and its ingest spend so far (`ingest_tokens`,
    `ingest_cost_usd`, `ingest_requests`: see KnowledgeBaseDetailResponse)."""
    return service.get_knowledge_base(principal.tenant_id, kb_id)


@_kb.patch("/{kb_id}", response_model=KnowledgeBaseResponse, responses={409: {"model": ErrorDetail}})
def update_knowledge_base(
    kb_id: str, body: KnowledgeBaseUpdate, principal: PrincipalDep, service: KnowledgeServiceDep
) -> dict[str, Any]:
    """Rename and/or set the description (null clears it). 409 name already used."""
    fields: dict[str, Any] = {}
    if "name" in body.model_fields_set:
        fields["name"] = body.name
    if "description" in body.model_fields_set:
        fields["description"] = body.description
    return service.update_knowledge_base(principal.tenant_id, kb_id, fields)


@_kb.delete(
    "/{kb_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": KnowledgeBaseInUse}},
)
def delete_knowledge_base(
    kb_id: str,
    principal: PrincipalDep,
    service: KnowledgeServiceDep,
    force: Annotated[bool, Query()] = False,
) -> Response:
    """Delete the knowledge base; its documents and chunks cascade, its runs stay. 409
    {detail, agents: [{agent_id, agent_name, version, archived, draft}]} when an agent's latest
    published version or current draft attaches it (`draft` true and `version` null when only the
    draft does), unless `force=true` (runs of such versions and drafts then get a tool error). An
    in-flight ingest run ends failed (`ingest_cancelled`) when it finds the knowledge base gone."""
    service.delete_knowledge_base(principal.tenant_id, kb_id, force)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------------ documents


@_kb.get("/{kb_id}/documents", response_model=KnowledgeDocumentListResponse)
def list_documents(
    kb_id: str,
    principal: PrincipalDep,
    service: KnowledgeServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    after: Annotated[
        str | None,
        Query(max_length=MAX_CURSOR_LENGTH, description="Opaque cursor: the previous page's next_after, verbatim"),
    ] = None,
) -> dict[str, Any]:
    """The knowledge base's documents (without content) by document_key, `limit` at a
    time (1–200, default 50). `next_after` is an opaque cursor when a full page came back (pass it
    back verbatim as `after`), else null. A cursor this API did not issue is a 422."""
    return service.list_documents(principal.tenant_id, kb_id, limit, after)


@_kb.get("/{kb_id}/documents/{document_key}", response_model=KnowledgeDocumentResponse)
def get_document(
    kb_id: str,
    document_key: str,
    principal: PrincipalDep,
    service: KnowledgeServiceDep,
    include_content: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """The document, with `extraction` (the worker extraction of its ACTIVE version: extractor, versions, pages,
    image-only pages, sections, warnings; null for API-extracted documents). `include_content=true`
    adds `content`, except for a binary document (`content_encoding` "base64"): its `content` is
    always null, and the stored bytes are never returned."""
    return service.get_document(principal.tenant_id, kb_id, document_key, include_content)


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
    service: KnowledgeServiceDep,
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
    out = service.put_document(principal.tenant_id, kb_id, document_key, body, body_size)
    return _json(status.HTTP_200_OK if out["unchanged"] else status.HTTP_202_ACCEPTED, out)


@_kb.delete("/{kb_id}/documents/{document_key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(kb_id: str, document_key: str, principal: PrincipalDep, service: KnowledgeServiceDep) -> Response:
    """Delete the document; its chunks cascade. An in-flight ingest run ends failed (`ingest_cancelled`)."""
    service.delete_document(principal.tenant_id, kb_id, document_key)
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
    service: KnowledgeServiceDep,
    body: Annotated[IngestRunCreate | None, Body()] = None,
) -> JSONResponse:
    """Start a new ingest run of the document's latest version (the "Retry" of a failed ingest;
    embeddings already written are reused).

    200 `{run_id: null, unchanged: true, document}`: the latest version is already active and
    ready, nothing to do. 409 while an ingest run of that version is queued, running or starting,
    or when a concurrent reingest won. Idempotent by the optional `run_id` (resolved before any
    write). 503 {detail, run_id}: retry with the same run_id (or none) to hand it off again."""
    out = service.reingest_document(principal.tenant_id, kb_id, document_key, body.run_id if body is not None else None)
    return _json(status.HTTP_200_OK if out["unchanged"] and out["run_id"] is None else status.HTTP_202_ACCEPTED, out)


# --------------------------------------------------------------------------- re-extract (RAG P1.A4)


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
    service: KnowledgeServiceDep,
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
    out = service.reextract_document(principal.tenant_id, kb_id, document_key, body.run_id if body is not None else None)
    return _json(status.HTTP_202_ACCEPTED, out)


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
    service: KnowledgeServiceDep,
) -> JSONResponse:
    """Re-extract many documents (see ReextractCreate): each selected document gets a new version
    with the same content and its own ingest run on the `eap-kb-ingest-fair` queue (this knowledge base's partition), which runs at most
    EAP_KB_INGEST_CONCURRENCY of them at a time. Embeddings of unchanged chunk texts are reused, so
    the cost is the changed chunks only.

    With `chunker_version: 2` on a v1 knowledge base, the chunking config's `chunker_version` is
    rewritten and EVERY document gets its new version in the same transaction (all or nothing).

    202 when at least one run started, else 200 (also for `dry_run`). A run whose enqueue failed is
    listed with `status` "failed" and a `detail` (its document stays `pending`; POST …/reingest
    starts it again), and the call goes on with the others. 422 unknown format names. 404 unknown
    knowledge base (another tenant's included)."""
    out = service.reextract_knowledge_base(
        principal.tenant_id, kb_id, formats=body.formats, only_outdated=body.only_outdated,
        chunker_version=body.chunker_version, dry_run=body.dry_run,
    )
    return _json(status.HTTP_202_ACCEPTED if out["started"] else status.HTTP_200_OK, out)


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
    service: KnowledgeServiceDep,
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
    out = service.reindex_knowledge_base(
        principal.tenant_id, kb_id, embedding_model=body.embedding_model, dry_run=body.dry_run, run_id=body.run_id
    )
    return _json(status.HTTP_200_OK if body.dry_run else status.HTTP_202_ACCEPTED, out)


@_kb.delete("/{kb_id}/reindex", status_code=status.HTTP_204_NO_CONTENT)
def cancel_reindex(kb_id: str, principal: PrincipalDep, service: KnowledgeServiceDep) -> Response:
    """Drop the pending generation and its chunks (status back to `ready`); a running reindex run
    then ends failed (`ingest_cancelled`). 204 also when nothing is pending."""
    service.cancel_reindex(principal.tenant_id, kb_id)
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


async def _search_body(request: Request) -> KnowledgeSearchRequest:
    try:
        return KnowledgeSearchRequest.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=safe_errors(exc, ["body"]))


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
    service: KnowledgeServiceDep,
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
    run, created, run_id = await asyncio.to_thread(
        service.start_search, tenant_id, kb_id,
        query=body.query, top_k=body.top_k, min_score=body.min_score, filter=body.filter, run_id=body.run_id,
    )
    await asyncio.to_thread(service.hand_off_search, tenant_id, run, created, run_id)

    deadline = time.monotonic() + wait
    interval = SEARCH_POLL_SECONDS
    while True:
        finished, view = await asyncio.to_thread(service.search_progress, tenant_id, run_id, run)
        if finished or time.monotonic() >= deadline or await request.is_disconnected():
            return _json(status.HTTP_200_OK if finished else status.HTTP_202_ACCEPTED, view)
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        interval = min(SEARCH_POLL_MAX_SECONDS, interval * 1.5)


router.include_router(_upload)
router.include_router(_kb)
