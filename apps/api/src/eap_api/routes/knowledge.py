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

Deleting a knowledge base that an agent's latest published version attaches answers 409 with the
agents, unless `force=true`. Deletes retry once on a deadlock (SQLSTATE 40P01): chunk-row locks
have no fixed order against an in-flight ingest (the RM.4 review).

The reindex dry run prices its token estimate from the operator price override file
(EAP_MODEL_PRICES_FILE, the file the worker prices `test:*` and `litellm:*` from). Other providers
are priced by genai-prices in the worker, which the API may not import: their estimate has tokens
and a `cost_note` instead of a cost.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from eap_api.auth import Principal, current_principal
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.request_guard import GuardedJsonRoute, guarded_json_route
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
    ReindexCreate,
    ReindexEstimateResponse,
    document_key_problem,
)
from eap_knowledge import ExtractionFailed, UnsupportedMimeType, extract
from eap_observability import current_trace_context, log_failure
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
from eap_schemas.embedding_policy import EmbeddingModel, EmbeddingModelNotAllowed, EmbeddingModelPolicy
from eap_schemas.tools import KNOWLEDGE_SEARCH_TOOL_ID, KnowledgeSearchInput

log = logging.getLogger("eap_api")

KB_MAX_DOCUMENT_BYTES_ENV = "EAP_KB_MAX_DOCUMENT_BYTES"
DEFAULT_KB_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
KB_MAX_INGEST_TOKENS_ENV = "EAP_KB_MAX_INGEST_TOKENS"
DEFAULT_KB_MAX_INGEST_TOKENS = 2_000_000
PRICES_FILE_ENV = "EAP_MODEL_PRICES_FILE"
# apps/api/src/eap_api/routes/knowledge.py -> the repository root (as the worker's pricing.py).
REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_PRICES_FILE = REPO_ROOT / "config" / "model-prices.json"
# Providers the worker prices from the override file only (eap_worker.pricing.OVERRIDE_ONLY_PROVIDERS).
OVERRIDE_ONLY_PROVIDERS = frozenset({"test", "litellm"})
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


def check_knowledge_configuration() -> None:
    """Fail startup on a malformed EAP_ALLOWED_EMBEDDING_MODELS, EAP_EMBEDDING_QUERY_INSTRUCTIONS,
    EAP_KB_MAX_DOCUMENT_BYTES or EAP_KB_MAX_INGEST_TOKENS (ValueError), not at the first request."""
    EmbeddingModelPolicy.from_env()
    max_document_bytes()
    max_ingest_tokens()


# The document upload's guard: the same checks as every JSON write route, with its own body cap.
DocumentUploadRoute = guarded_json_route(max_body_bytes=max_document_bytes)

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


def ingest_input(document_id: str, doc_version: int) -> str:
    """The `input` of a document ingest run (and the enqueued `input_json`)."""
    return _canonical_json({"document_id": str(document_id), "doc_version": int(doc_version)})


def reindex_input(generation: int) -> str:
    """The `input` of a reindex run (and the enqueued `input_json`)."""
    return _canonical_json({"reindex": int(generation)})


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
        cost = format(Decimal(cost).quantize(Decimal("0.0000000001")), "f")
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
        and run.get("input") == input_json
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
    """The tenant's agents whose latest published version attaches `kb_id` (spec.knowledge_bases),
    archived agents included. One query: the latest version per agent through the unique
    (agent_id, version) index, then a JSONB containment test on the spec."""
    needle = json.dumps([{"id": str(kb_id)}])
    with store.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.id, a.name, a.archived, v.version FROM agents a"
                " JOIN LATERAL (SELECT av.version, av.spec FROM agent_versions av"
                "   WHERE av.agent_id = a.id AND av.tenant_id = a.tenant_id ORDER BY av.version DESC LIMIT 1) v"
                " ON true"
                " WHERE a.tenant_id = :t AND v.spec -> 'knowledge_bases' @> CAST(:needle AS jsonb)"
                " ORDER BY a.name, a.id"
            ),
            {"t": tenant_id, "needle": needle},
        ).mappings().all()
    return [
        {"agent_id": str(r["id"]), "agent_name": r["name"], "version": r["version"], "archived": bool(r["archived"])}
        for r in rows
    ]


# ------------------------------------------------------------------------------- price estimate


def _prices_file() -> tuple[Path, bool]:
    configured = os.environ.get(PRICES_FILE_ENV)
    if not configured:
        return DEFAULT_PRICES_FILE, False
    path = Path(configured).expanduser()
    return (path if path.is_absolute() else REPO_ROOT / path), True


def estimate_embedding_cost(model_id: str, tokens: int, today: date | None = None) -> tuple[str | None, str | None, str | None]:
    """(cost_usd as a decimal string, price_source, note) for `tokens` input tokens of `model_id`.

    Reads the operator override file with the worker's lookup rules (an exact `provider:name` key
    wins over `provider:*`; the period with the latest `effective_from` not after today applies;
    input_per_mtok). A provider the worker prices through genai-prices gets no API estimate."""
    provider = model_id.partition(":")[0]
    path, explicit = _prices_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        models = data["models"]
        if not isinstance(models, dict):
            raise ValueError("models is not an object")
    except FileNotFoundError:
        if explicit:
            return None, None, f"the price file named by {PRICES_FILE_ENV} does not exist"
        models = {}
    except (OSError, ValueError, KeyError, TypeError):
        return None, None, "the model price override file is unreadable or invalid"
    day = today or datetime.now(timezone.utc).date()
    for key in (model_id, f"{provider}:*"):
        periods = []
        for period in models.get(key) or []:
            try:
                start = date.fromisoformat(str(period["effective_from"]))
                price = Decimal(str(period["input_per_mtok"]))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                return None, None, "the model price override file is unreadable or invalid"
            if start <= day:
                periods.append((start, price))
        if periods:
            _, per_mtok = max(periods, key=lambda p: p[0])
            cost = (Decimal(tokens) * per_mtok / Decimal(1_000_000)).quantize(Decimal("0.0000000001"))
            return format(cost, "f"), "override", None
    if provider in OVERRIDE_ONLY_PROVIDERS:
        return None, None, f"no price for '{model_id}' in the model price override file"
    return None, None, (
        f"'{provider}' models are priced by genai-prices in the worker, which the API cannot consult; "
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
    """Create a knowledge base. 422 embedding model not allowed (or its dimensions unknown); 409
    the tenant already has a knowledge base of that name."""
    model = _resolve_model(body.embedding_model)
    try:
        kb = store.create_knowledge_base(
            principal.tenant_id,
            name=body.name,
            description=body.description,
            embedding_model=model.id,
            dimensions=model.dimensions,
            chunking=body.chunking,
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
    {detail, agents: [{agent_id, agent_name, version, archived}]} when an agent's latest published
    version attaches it, unless `force=true` (runs of such versions then get a tool error). An
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
    return {"items": items, "next_after": next_after}


@_kb.get("/{kb_id}/documents/{document_key}", response_model=KnowledgeDocumentResponse)
def get_document(
    kb_id: str,
    document_key: str,
    principal: PrincipalDep,
    store: KnowledgeDep,
    include_content: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    kb = _kb_or_404(store, principal.tenant_id, kb_id)
    document = store.get_document(principal.tenant_id, kb["id"], document_key, include_content=include_content)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOCUMENT_NOT_FOUND)
    return document


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
    store: KnowledgeDep,
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Upload a document as UTF-8 text. A sync route: FastAPI runs it (and its extraction) in the
    threadpool, off the event loop.

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
    if not body.content.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="content is empty")
    kb = _kb_or_404(store, tenant_id, kb_id)  # before extraction: no CPU for a missing or foreign KB
    title = (body.title or "").strip() or document_key
    try:
        extracted = extract(body.content, body.mime_type, title=title, source_url=body.source_url)
    except (UnsupportedMimeType, ExtractionFailed) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
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
                or document["content_hash"] != content_hash(body.content)
            ):
                # Not a retry of the request that stored the current version: refuse, write nothing.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
                )
            run = _replay(run_store, launcher, tenant_id, existing)
            created = _run_input(existing).get("doc_version") == 1
            return _json(
                status.HTTP_202_ACCEPTED, {"unchanged": False, "created": created, "document": document, **run}
            )

        new_run_id = body.run_id or uuid.uuid4()
        try:
            result = store.put_document(
                tenant_id,
                kb["id"],
                document_key=document_key,
                title=title,
                content=body.content,
                mime_type=body.mime_type,
                source_url=body.source_url,
                metadata=body.metadata,
                run_id=str(new_run_id),  # last_run_id, in the same transaction as the new version
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
                    {"unchanged": False, "created": result.created, "document": current,
                     "run_id": current["last_run_id"]},
                )
            input_json = ingest_input(document["id"], document["latest_version"])
            run, created = _create_ingest_run(run_store, tenant_id, kb["id"], input_json, new_run_id)
            view = _hand_off(run_store, launcher, tenant_id, run, created)
            return _json(
                status.HTTP_202_ACCEPTED,
                {"unchanged": False, "created": result.created, "document": document, **view},
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
                {"unchanged": True, "created": False, "document": document, "run_id": None},
            )
    except HandOffFailed as exc:
        return exc.response()
    document = store.get_document(tenant_id, kb["id"], document_key) or document
    return _json(
        status.HTTP_202_ACCEPTED,
        {"unchanged": False, "created": document["latest_version"] == 1, "document": document, **view},
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
                {"run_id": None, "unchanged": True, "knowledge_base_id": kb["id"], "document": document},
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
    return _json(status.HTTP_202_ACCEPTED, {**view, "unchanged": False, "document": document})


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
