"""Knowledge base ingestion (RAG and memory milestone, RM.10): the durable `eap.kb_ingest` workflow.

Hand-off (apps/api/src/eap_api/launcher.py, KB_INGEST_WORKFLOW_NAME): the API inserts a `runs` row of
kind 'kb_ingest' and enqueues `eap.kb_ingest(run_id, tenant_id, knowledge_base_id, input_json)` on
queue `eap-agent-runs` with workflow_id = run_id. `input_json` is the run's canonical input:

    ingest:  {"doc_version": N, "document_id": "<uuid>"}   chunk and embed that version, then switch
    reindex: {"reindex": G}                                 embed the pending generation G, then switch

Step layout (each line is a DBOS step, so recovery replays recorded results):

    eap.start_ingest                    ensure the runs row, status -> running; ingest: snapshot the
                                        document's state and mark it 'ingesting'; reindex: check that
                                        G is still pending. Returns small data only (no content)
    eap.append_event   start            run_started {kind, mode, knowledge_base_id, ...}
    --- per round r (normally one; another only when a generation switched under an ingest) ---
    eap.kb_chunk       (ingest)         re-read the document, extract + chunk (eap_knowledge), upsert the
                                        chunk rows without embeddings (idempotent per slot), reuse cached
                                        embeddings, count what is left: returns counts only
    eap.kb_copy        (reindex)        copy the active chunks into G (no re-chunking), reuse cached
                                        embeddings in G, count what is left
                                        -> cost cap: un-cached approx tokens over EAP_KB_MAX_INGEST_TOKENS
                                           fails the run (ingest_budget_exceeded) before any provider call
    eap.append_event   chunk:{r}        ingest_progress {phase: chunked, embedded, total, pending_tokens}
    eap.kb_embed_batch (i = 0, 1, ...)  up to EAP_KB_EMBED_BATCH_CHUNKS (64) un-embedded chunks (about
                                        100k approx tokens at most) selected first, only those filled from
                                        the cache, the rest embedded in one embedding call (request key `embed:batch:{i}`), then the vectors,
                                        the usage row and an `ingest_progress` event (key
                                        `embed:batch:{i}`) in ONE transaction (write_embeddings)
    eap.kb_finalize                     finalize_document / finalize_reindex: the atomic switch (+ event
                                        `finalize:{r}`); 'incomplete' / 'stale_generation' -> next round
    --- end ---
    eap.append_event   end              run_completed {output, result} | run_failed {status, error, error_code}
    eap.finish_run                      status -> completed | failed

On a failure, `eap.kb_mark_failed` (before `end`) records it where the user sees it: the document
gets status 'failed' and the error "<code>: N of M sections embedded" (ingest), or the knowledge base
gets 'reindex_failed' with that error (reindex). The previous active version or generation stays
searchable; a retry run of the same version or generation reuses everything already embedded.

Durability:
- A crash inside a batch before its commit re-executes the step: the same chunks are still
  un-embedded, so the same batch is embedded again (one bounded duplicate provider charge) and the
  usage row is written once, on commit. A crash after the commit and before the checkpoint finds the
  batch's event (`embed:batch:{i}`) and returns its recorded counts without a provider call.
- `eap.kb_chunk` is idempotent (ON CONFLICT DO NOTHING per (document, version, generation,
  ordinal)); a finalisation is one transaction and answers 'already_active' when repeated.
- Failures are classified inside the steps and checkpointed as text-free failure dicts, so a replay
  takes the same path. `eap.kb_embed_batch` retries the provider itself (EMBED_ATTEMPTS, for
  embeddings.is_retryable errors: 408/409/429/5xx, connection errors, timeouts) and turns every
  provider failure into such a dict: provider exceptions, whose text may echo the input, never reach
  DBOS's retry handler, its logs or its span events. DBOS retries a step only for database deadlocks /
  serialization failures (SQLSTATE 40P01 / 40001; chunk-row locks have no fixed order, see the RM.4
  hand-off) and lost connections (`_transient`); anything else escaping a step is `internal_error`.
- The cost cap is per run: the approximate tokens already sent in this run plus each round's
  un-cached pending tokens (each distinct embed text once, the dry-run estimate's rule) must stay
  within EAP_KB_MAX_INGEST_TOKENS.
- Rounds: one normally. Rounds that make progress continue up to MAX_ROUNDS (50); MAX_IDLE_ROUNDS (3)
  consecutive rounds that insert, copy and embed nothing fail the run (`ingest_not_converged`).
- Cancellation: the document or knowledge base deleted mid-run (the store raises DocumentNotFound /
  KnowledgeBaseNotFound or answers 'missing'), or the reindex cancelled, ends the run with code
  `ingest_cancelled` (status 'failed'; runs have no 'cancelled' status), without touching the document.
- A newer upload of the document makes this version 'superseded': the run completes without
  switching (the newer version's own run does that).

Events carry counts, ids, models and costs only, never document text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from uuid import UUID

from dbos import DBOS

from eap_observability import exception_type, log_context, log_failure, run_span, set_run_outcome
from eap_run_store import KnowledgeStore
from eap_run_store.knowledge import (
    DEFAULT_BATCH_CHUNKS,
    DEFAULT_BATCH_TOKENS,
    DocumentNotFound,
    GenerationModelMismatch,
    GenerationNotWritable,
    KnowledgeBaseLimitExceeded,
)
from eap_schemas.embedding_policy import EmbeddingDimensionMismatch, EmbeddingModelNotAllowed
from eap_schemas.knowledge import ChunkingConfig, KnowledgeBaseNotFound
from eap_worker import embeddings as _embeddings
from eap_worker.crash import crash_point
from eap_worker.embeddings import is_retryable
from eap_worker.runtime import get_runtime
from eap_worker.workflow import _heartbeat, append_event_step, finish_run_step

log = logging.getLogger("eap_worker")

KB_INGEST_WORKFLOW_NAME = "eap.kb_ingest"
INGEST_PROGRESS = "ingest_progress"

MAX_INGEST_TOKENS_ENV = "EAP_KB_MAX_INGEST_TOKENS"
DEFAULT_MAX_INGEST_TOKENS = 2_000_000
BATCH_CHUNKS_ENV = "EAP_KB_EMBED_BATCH_CHUNKS"
# Rounds of chunk/copy -> embed -> finalise. One normally; another only when the generation switched
# under an ingest ('stale_generation'), or documents were activated during a reindex ('incomplete').
# Rounds that make progress (rows inserted, copied or embedded) never fail the run, up to the
# absolute bound; MAX_IDLE_ROUNDS consecutive rounds without progress do (code ingest_not_converged).
MAX_ROUNDS = 50
MAX_IDLE_ROUNDS = 3
NOT_CONVERGED = "ingest_not_converged"
# Provider attempts per batch, inside eap.kb_embed_batch (retryable errors only), with backoff
# EMBED_RETRY_SECONDS * 2**attempt.
EMBED_ATTEMPTS = 3
EMBED_RETRY_SECONDS = 1.0

STEP_RETRY = {"retries_allowed": True, "max_attempts": 3, "interval_seconds": 1.0, "backoff_rate": 2.0}

# Failure codes (eap_schemas.knowledge.INGEST_FAILURE_CODES plus the platform's own) -> fixed message.
INGEST_CANCELLED = "ingest_cancelled"
_CANCEL_MESSAGES = {
    "document_deleted": "the document was deleted during the ingest",
    "knowledge_base_deleted": "the knowledge base was deleted during the ingest",
    "reindex_cancelled": "the reindex was cancelled",
}

__all__ = [
    "INGEST_CANCELLED",
    "KB_INGEST_WORKFLOW_NAME",
    "IngestFailed",
    "batch_event_key",
    "batch_request_key",
    "kb_chunk_step",
    "kb_copy_step",
    "kb_embed_batch_step",
    "kb_finalize_step",
    "kb_ingest",
    "kb_mark_failed_step",
    "max_ingest_tokens",
    "progress_message",
    "start_ingest_step",
]


class IngestFailed(Exception):
    """The ingest run failed with a known code; `message` is fixed, user-safe text built from ids,
    model ids and counts only. Pickle-safe."""

    def __init__(self, code: str, error_type: str, message: str) -> None:
        self.code = code
        self.error_type = error_type
        self.message = message
        super().__init__(code, error_type, message)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    @property
    def error(self) -> str:
        return f"{self.error_type}: {self.message}"


# --------------------------------------------------------------------------- helpers


def max_ingest_tokens() -> int:
    """EAP_KB_MAX_INGEST_TOKENS: the per-run budget of un-cached approximate embedding tokens."""
    raw = os.environ.get(MAX_INGEST_TOKENS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_INGEST_TOKENS
    value = int(raw)
    if value < 1:
        raise ValueError(f"{MAX_INGEST_TOKENS_ENV} must be a positive integer, got {raw!r}")
    return value


def _batch_chunks() -> int:
    raw = os.environ.get(BATCH_CHUNKS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_BATCH_CHUNKS
    return max(1, int(raw))


def batch_request_key(index: int) -> str:
    """The usage request key (and event key) of batch `index`; stable across re-executions."""
    return f"embed:batch:{index}"


batch_event_key = batch_request_key


def progress_message(embedded: int, total: int) -> str:
    return f"{embedded} of {total} sections embedded"


def _sqlstate(exc: BaseException) -> str | None:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for candidate in (cur, getattr(cur, "orig", None)):
            state = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
            if isinstance(state, str):
                return state
        cur = cur.__cause__ or cur.__context__
    return None


def _transient(exc: BaseException) -> bool:
    """Whether DBOS should try a step again: database deadlocks (40P01), serialization failures
    (40001) and lost connections. Provider errors never get here: eap.kb_embed_batch retries and
    classifies them itself. Anything else fails at once."""
    if _sqlstate(exc) in ("40P01", "40001"):
        return True
    from sqlalchemy.exc import OperationalError

    return isinstance(exc, OperationalError) and getattr(exc, "connection_invalidated", False)


def _store() -> KnowledgeStore:
    return KnowledgeStore(get_runtime().engine)


def _failure(code: str, error_type: str, message: str) -> dict[str, Any]:
    return {"status": "failed", "code": code, "error_type": error_type, "message": message}


def _cancelled(reason: str) -> dict[str, Any]:
    return {"status": "missing", "reason": reason}


def _embedding_failure(exc: BaseException, model_id: str) -> dict[str, Any]:
    """A non-retryable embedding error as a failure code (never the exception's text)."""
    if isinstance(exc, EmbeddingModelNotAllowed):
        return _failure(
            "embedding_model_not_allowed", "EmbeddingModelNotAllowed",
            f"embedding model '{model_id}' is not allowed on this platform",
        )
    if isinstance(exc, EmbeddingDimensionMismatch):
        return _failure(
            "embedding_dimension_mismatch", "EmbeddingDimensionMismatch",
            f"embedding model '{model_id}' returned vectors of other dimensions than the knowledge base's",
        )
    status = getattr(exc, "status_code", None)
    message = (
        f"the embedding provider returned HTTP {status}" if isinstance(status, int)
        else "the embedding provider request failed"
    )
    return _failure("embedding_failed", exception_type(exc), message)


def _scope(mode: str, document_id: str | None, doc_version: int | None) -> dict[str, Any]:
    return {"document_id": document_id, "doc_version": doc_version} if mode == "ingest" else {}


# --------------------------------------------------------------------------- steps


@DBOS.step(name="eap.start_ingest")
async def start_ingest_step(run_id: str, tenant_id: str, kb_id: str, input_json: str) -> dict[str, Any]:
    """Returns {"traceparent", "outcome": ok | missing | superseded | already_active, "reason"?,
    "generation"?, "embedding_model"?}. Ingest 'ok' marks the document 'ingesting'."""
    rt = get_runtime()
    store = _store()
    params = json.loads(input_json)

    def work() -> dict[str, Any]:
        # The API inserts the row first; an idempotent safety net, as for tool runs.
        rt.run_store.create_kb_ingest_run(tenant_id, kb_id, input_json, UUID(run_id))
        rt.run_store.mark_running(tenant_id, UUID(run_id))
        rt.run_store.heartbeat(tenant_id, UUID(run_id))
        run = rt.run_store.get_run(tenant_id, UUID(run_id)) or {}
        out: dict[str, Any] = {"traceparent": run.get("traceparent")}
        if "reindex" in params:
            generation = int(params["reindex"])
            kb = store.get_knowledge_base(tenant_id, kb_id)
            if kb is None:
                return {**out, **_cancelled("knowledge_base_deleted"), "outcome": "missing"}
            if kb["pending_generation"] == generation:
                return {**out, "outcome": "ok", "generation": generation, "embedding_model": kb["pending_embedding_model"]}
            if kb["active_generation"] == generation:
                return {**out, "outcome": "already_active", "generation": generation, "embedding_model": kb["embedding_model"]}
            return {**out, **_cancelled("reindex_cancelled"), "outcome": "missing"}
        document_id, version = str(params["document_id"]), int(params["doc_version"])
        source = store.get_ingest_source(tenant_id, document_id, version)
        if source is None or source["document"]["kb_id"] != str(kb_id):
            reason = "knowledge_base_deleted" if store.get_knowledge_base(tenant_id, kb_id) is None else "document_deleted"
            return {**out, **_cancelled(reason), "outcome": "missing"}
        base = {**out, "generation": source["generation"], "embedding_model": source["embedding_model"]}
        if source["already_active"]:
            return {**base, "outcome": "already_active"}
        if source["superseded"]:
            return {**base, "outcome": "superseded"}
        store.mark_document_ingesting(tenant_id, document_id, version, run_id)
        return {**base, "outcome": "ok"}

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.kb_chunk", should_retry=_transient, **STEP_RETRY)
async def kb_chunk_step(run_id: str, tenant_id: str, kb_id: str, document_id: str, doc_version: int) -> dict[str, Any]:
    """Chunk the version and upsert its chunk rows without embeddings, reuse cached embeddings, and
    count what is left. Returns counts only: {"status": ok | missing | superseded | already_active |
    stale | failed, "generation", "embedding_model", "total", "embedded", "pending_tokens", "limit",
    "inserted"}."""
    from eap_knowledge import ExtractionFailed, InvalidChunkingConfig, UnsupportedMimeType, chunk, extract
    from pydantic import ValidationError

    store = _store()
    limit = max_ingest_tokens()

    def work() -> dict[str, Any]:
        source = store.get_ingest_source(tenant_id, document_id, doc_version)
        if source is None or source["document"]["kb_id"] != str(kb_id):
            kb_gone = store.get_knowledge_base(tenant_id, kb_id) is None
            return _cancelled("knowledge_base_deleted" if kb_gone else "document_deleted")
        generation, model = source["generation"], source["embedding_model"]
        base = {"generation": generation, "embedding_model": model}
        if source["already_active"]:
            return {**base, "status": "already_active"}
        if source["superseded"]:
            # The row's content is a newer version now: never chunk it under this version.
            return {**base, "status": "superseded"}
        doc = source["document"]
        try:
            config = ChunkingConfig.model_validate(source["chunking"])
            parsed = extract(doc["content"], doc["mime_type"], title=doc["title"] or "", source_url=doc["source_url"])
            chunks = chunk(parsed, config)
        except UnsupportedMimeType:
            return _failure("extraction_failed", "UnsupportedMimeType", "the document's MIME type is not supported")
        except ExtractionFailed as exc:
            return _failure("extraction_failed", "ExtractionFailed", f"the document could not be extracted ({exc.reason})")
        except (InvalidChunkingConfig, ValidationError):
            return _failure("extraction_failed", "InvalidChunkingConfig", "the knowledge base's chunking config is invalid")
        if parsed.extracted_chars == 0 or not chunks:
            # Fail closed: never switch to an empty version (that would delete the active one's chunks).
            return _failure("extraction_failed", "ExtractionFailed", "the document has no extractable text")
        try:
            upserted = store.upsert_chunks(
                tenant_id, document_id, doc_version, generation, chunks, metadata=doc["metadata"] or {}
            )
        except DocumentNotFound:
            return _cancelled("document_deleted")
        except KnowledgeBaseNotFound:
            return _cancelled("knowledge_base_deleted")
        except GenerationNotWritable:
            return {**base, "status": "stale"}
        except KnowledgeBaseLimitExceeded as exc:
            return _failure(
                "knowledge_base_limit_exceeded", "KnowledgeBaseLimitExceeded",
                f"the knowledge base would exceed {exc.limit} {exc.what}",
            )
        crash_point("kb_chunk")
        try:
            store.apply_cached_embeddings(
                tenant_id, kb_id, generation, document_id=document_id, doc_version=doc_version
            )
        except KnowledgeBaseNotFound:
            return _cancelled("knowledge_base_deleted")
        except GenerationNotWritable:
            return {**base, "status": "stale"}
        progress = store.embedding_progress(
            tenant_id, kb_id, generation, document_id=document_id, doc_version=doc_version
        )
        return {
            **base,
            "status": "ok",
            "total": progress.total,
            "embedded": progress.embedded,
            "pending_tokens": progress.pending_tokens,
            "limit": limit,
            "inserted": upserted.inserted,
        }

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.kb_copy", should_retry=_transient, **STEP_RETRY)
async def kb_copy_step(run_id: str, tenant_id: str, kb_id: str, generation: int) -> dict[str, Any]:
    """Reindex: copy the active chunks into the pending `generation` (idempotent), reuse cached
    embeddings, count what is left. Same shape as kb_chunk_step."""
    store = _store()
    limit = max_ingest_tokens()

    def work() -> dict[str, Any]:
        try:
            copied = store.copy_generation_chunks(tenant_id, kb_id, generation)
            store.apply_cached_embeddings(tenant_id, kb_id, generation)
        except KnowledgeBaseNotFound:
            return _cancelled("knowledge_base_deleted")
        except GenerationNotWritable:
            kb = store.get_knowledge_base(tenant_id, kb_id)
            if kb is not None and kb["active_generation"] == generation:
                return {"status": "already_active", "generation": generation}
            return _cancelled("knowledge_base_deleted" if kb is None else "reindex_cancelled")
        crash_point("kb_chunk")
        progress = store.embedding_progress(tenant_id, kb_id, generation)
        return {
            "status": "ok",
            "generation": generation,
            "total": progress.total,
            "embedded": progress.embedded,
            "pending_tokens": progress.pending_tokens,
            "limit": limit,
            "inserted": copied,
        }

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.kb_embed_batch", should_retry=_transient, **STEP_RETRY)
async def kb_embed_batch_step(
    run_id: str,
    tenant_id: str,
    kb_id: str,
    mode: str,
    generation: int,
    embedding_model: str,
    index: int,
    embedded_before: int,
    total: int,
    document_id: str | None = None,
    doc_version: int | None = None,
) -> dict[str, Any]:
    """Embed the next batch of the scope (one document version, or the whole reindex generation).

    The batch is selected first (up to EAP_KB_EMBED_BATCH_CHUNKS un-embedded chunks), then only
    those rows are filled from the cache, and the rest are embedded in one call. `embedded_before`
    and `total` come from the workflow (the previous step outputs), so no step recounts the scope.

    The provider call is retried here (EMBED_ATTEMPTS, with backoff) for retryable errors, and every
    provider failure ends as a checkpointed, text-free failure dict: no provider exception (whose
    text may echo the input) ever reaches DBOS's retry handler, logs or spans. Only database errors
    propagate (retried by DBOS when transient: 40P01, 40001, a lost connection).

    Returns {"status": embedded | done | missing | stale | failed, "embedded", "total",
    "written", "filled", "tokens"}: `tokens` is the approximate tokens sent to the provider (each
    distinct text once), which the workflow adds to the run's spent budget."""
    rt = get_runtime()
    store = _store()
    key = batch_request_key(index)
    scope = _scope(mode, document_id, doc_version)

    def gone(exc: BaseException) -> dict[str, Any]:
        if isinstance(exc, KnowledgeBaseNotFound):
            return _cancelled("knowledge_base_deleted")
        if mode == "reindex":  # a reindex generation no longer pending: cancelled (or switched)
            return _cancelled("reindex_cancelled")
        return {"status": "stale"}  # the active generation switched under an ingest: chunk again

    def prepare() -> dict[str, Any]:
        # Committed before a crash (after the commit, before the checkpoint): the batch's event holds
        # its counts. No provider call, nothing written twice.
        done = rt.event_log.get_event(tenant_id, run_id, key)
        if done is not None:
            d = done.data
            return {
                "status": "embedded", "embedded": int(d.get("embedded", embedded_before)),
                "total": int(d.get("total", total)), "written": int(d.get("written", 0)),
                "filled": int(d.get("filled", 0)), "tokens": int(d.get("approx_tokens", 0)), "replayed": True,
            }
        try:
            batch = store.next_unembedded_batch(
                tenant_id, kb_id, generation, **scope, max_chunks=_batch_chunks(), max_tokens=DEFAULT_BATCH_TOKENS
            )
            if not batch:
                return {"status": "done", "embedded": embedded_before, "total": total, "written": 0, "filled": 0, "tokens": 0}
            filled = store.fill_cached_embeddings(tenant_id, kb_id, generation, [c.id for c in batch])
        except (KnowledgeBaseNotFound, GenerationNotWritable) as exc:
            return gone(exc)
        return {"status": "pending", "batch": [c for c in batch if c.id not in filled], "filled": len(filled)}

    prepared = await asyncio.to_thread(prepare)
    if prepared["status"] != "pending":
        return prepared
    batch, filled = prepared["batch"], prepared["filled"]
    if not batch:  # all from the cache: nothing to embed, no event, no usage
        return {
            "status": "embedded", "embedded": embedded_before + filled, "total": total,
            "written": 0, "filled": filled, "tokens": 0,
        }
    # Identical texts in one batch are embedded once.
    texts: list[str] = []
    slot: dict[str, int] = {}
    tokens = 0
    for c in batch:
        if c.embed_text_hash not in slot:
            slot[c.embed_text_hash] = len(texts)
            texts.append(c.embed_text)
            tokens += c.approx_tokens
    call = None
    for attempt in range(EMBED_ATTEMPTS):
        try:
            call = await _embeddings.embed_documents(
                rt.settings, tenant_id=tenant_id, run_id=run_id, request_key=key, model_id=embedding_model, texts=texts
            )
            break
        except Exception as exc:  # noqa: BLE001 - classified here: a text-free failure dict, never re-raised
            if is_retryable(exc) and attempt + 1 < EMBED_ATTEMPTS:
                log_failure(
                    log, "kb ingest embedding attempt failed; retrying", exc, code="embedding_retry",
                    run_id=run_id, tenant_id=tenant_id,
                )
                await asyncio.sleep(EMBED_RETRY_SECONDS * (2**attempt))
                continue
            failure = _embedding_failure(exc, embedding_model)
            log_failure(log, "kb ingest embedding failed", exc, code=failure["code"], run_id=run_id, tenant_id=tenant_id)
            return {**failure, "embedded": embedded_before + filled, "total": total}
    assert call is not None
    crash_point("kb_batch_before_commit", event_key=key)
    vectors = {c.id: call.vectors[slot[c.embed_text_hash]] for c in batch}
    expected = embedded_before + filled + len(batch)
    data = {
        **call.event_data,
        "phase": "embedding",
        "batch": index,
        "chunks": len(batch),
        "filled": filled,
        "written": len(batch),
        "approx_tokens": tokens,
        "embedded": expected,
        "total": total,
        "message": progress_message(expected, total),
    }

    def write() -> dict[str, Any]:
        try:
            result = store.write_embeddings(
                tenant_id, kb_id, generation, vectors, embedding_model=embedding_model, run_id=run_id,
                event_key=key, event_type=INGEST_PROGRESS, event_data=data, usage=call.usage,
            )
        except (KnowledgeBaseNotFound, GenerationNotWritable) as exc:
            return gone(exc)
        except GenerationModelMismatch:
            return {"status": "stale"} if mode == "ingest" else _cancelled("reindex_cancelled")
        except EmbeddingDimensionMismatch as exc:
            return {**_embedding_failure(exc, embedding_model), "embedded": embedded_before + filled, "total": total}
        return {
            "status": "embedded", "embedded": embedded_before + filled + result.written, "total": total,
            "written": result.written, "filled": filled, "tokens": tokens, "requests": call.requests,
        }

    result = await asyncio.to_thread(write)
    if result["status"] == "embedded":
        crash_point("kb_batch_after_commit", event_key=key)
    return result


@DBOS.step(name="eap.kb_finalize", should_retry=_transient, **STEP_RETRY)
async def kb_finalize_step(
    run_id: str,
    tenant_id: str,
    kb_id: str,
    mode: str,
    generation: int,
    embedding_model: str,
    round_: int,
    document_id: str | None = None,
    doc_version: int | None = None,
) -> dict[str, Any]:
    """The atomic switch. Returns {"status": switched | already_active | incomplete | superseded |
    stale_generation | missing, "chunk_count", "pending"}."""
    store = _store()
    key = f"finalize:{round_}"
    data: dict[str, Any] = {"phase": "finalized", "generation": generation}
    if mode == "ingest":
        data.update(document_id=document_id, doc_version=doc_version)

    def work() -> dict[str, Any]:
        crash_point("kb_before_finalize")
        try:
            if mode == "ingest":
                result = store.finalize_document(
                    tenant_id, str(document_id), int(doc_version or 0), generation, embedding_model=embedding_model,
                    run_id=run_id, event_key=key, event_type=INGEST_PROGRESS, event_data=data,
                )
            else:
                result = store.finalize_reindex(
                    tenant_id, kb_id, generation, embedding_model=embedding_model,
                    run_id=run_id, event_key=key, event_type=INGEST_PROGRESS, event_data=data,
                )
        except GenerationModelMismatch:
            return {"status": "stale_generation" if mode == "ingest" else "missing", "chunk_count": 0, "pending": 0}
        crash_point("kb_finalize")
        return {"status": result.status, "chunk_count": result.chunk_count, "pending": result.pending}

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.kb_mark_failed", should_retry=_transient, **STEP_RETRY)
async def kb_mark_failed_step(
    run_id: str,
    tenant_id: str,
    kb_id: str,
    mode: str,
    code: str,
    generation: int | None,
    document_id: str | None = None,
    doc_version: int | None = None,
) -> dict[str, Any]:
    """Record the failure on the document ('failed', "<code>: N of M sections embedded") or the
    knowledge base ('reindex_failed'). The active version or generation stays searchable. Returns
    the counts ({"embedded", "total"}; zeros when nothing was chunked)."""
    store = _store()

    def work() -> dict[str, Any]:
        g = generation
        if g is None:
            kb = store.get_knowledge_base(tenant_id, kb_id)
            if kb is None:
                return {"embedded": 0, "total": 0, "marked": False}
            g = kb["active_generation"]
        try:
            progress = store.embedding_progress(tenant_id, kb_id, g, **_scope(mode, document_id, doc_version))
        except KnowledgeBaseNotFound:
            return {"embedded": 0, "total": 0, "marked": False}
        error = f"{code}: {progress_message(progress.embedded, progress.total)}"
        if mode == "ingest":
            marked = store.mark_document_failed(tenant_id, str(document_id), int(doc_version or 0), error)
        else:
            marked = store.mark_reindex_failed(tenant_id, kb_id, g, error)
        return {"embedded": progress.embedded, "total": progress.total, "marked": marked}

    return await asyncio.to_thread(work)


# --------------------------------------------------------------------------- the workflow


@DBOS.workflow(name=KB_INGEST_WORKFLOW_NAME)
async def kb_ingest(run_id: str, tenant_id: str, kb_id: str, input_json: str) -> dict[str, Any]:
    with log_context(tenant_id=tenant_id, run_id=run_id):
        start = await start_ingest_step(run_id, tenant_id, kb_id, input_json)
    with run_span(run_id, tenant_id, "", "", traceparent=start.get("traceparent")) as span:
        span.set_attribute("eap.run_kind", "kb_ingest")
        span.set_attribute("eap.knowledge_base_id", kb_id)
        log.info("kb_ingest run %s started: tenant=%s kb=%s", run_id, tenant_id, kb_id)
        result = await _ingest_body(run_id, tenant_id, kb_id, json.loads(input_json), start)
        set_run_outcome(span, result["status"], result.get("error_code"))
        log.info("kb_ingest run %s %s", run_id, result["status"])
        return result


class _Ctx:
    """The run's position, for the failure path (kept in workflow memory; rebuilt on replay)."""

    def __init__(self, mode: str, document_id: str | None, doc_version: int | None) -> None:
        self.mode = mode
        self.document_id = document_id
        self.doc_version = doc_version
        self.generation: int | None = None


def _cancel(reason: str) -> IngestFailed:
    return IngestFailed(INGEST_CANCELLED, "IngestCancelled", _CANCEL_MESSAGES.get(reason, "the ingest was cancelled"))


def _raise_failure(out: dict[str, Any]) -> None:
    if out["status"] == "failed":
        raise IngestFailed(out["code"], out["error_type"], out["message"])
    if out["status"] == "missing":
        raise _cancel(out.get("reason", "document_deleted"))


async def _ingest_body(
    run_id: str, tenant_id: str, kb_id: str, params: dict[str, Any], start: dict[str, Any]
) -> dict[str, Any]:
    mode = "reindex" if "reindex" in params else "ingest"
    ctx = _Ctx(
        mode,
        None if mode == "reindex" else str(params["document_id"]),
        None if mode == "reindex" else int(params["doc_version"]),
    )
    started: dict[str, Any] = {"kind": "kb_ingest", "mode": mode, "knowledge_base_id": kb_id}
    if mode == "reindex":
        started["generation"] = int(params["reindex"])
    else:
        started.update(document_id=ctx.document_id, doc_version=ctx.doc_version)
    await append_event_step(run_id, tenant_id, "start", "run_started", started)
    heartbeat = asyncio.create_task(_heartbeat(run_id, tenant_id))
    try:
        result = await _drive(run_id, tenant_id, kb_id, ctx, start)
    except Exception as exc:  # noqa: BLE001 - every failure ends the run with a durable run_failed
        heartbeat.cancel()
        return await _fail(run_id, tenant_id, kb_id, ctx, exc)
    finally:
        heartbeat.cancel()
    output = json.dumps(result, sort_keys=True, separators=(",", ":"))
    await append_event_step(run_id, tenant_id, "end", "run_completed", {"output": output, "result": result})
    await finish_run_step(run_id, tenant_id, "completed", output, None)
    return {"status": "completed", "output": output, "result": result}


async def _drive(run_id: str, tenant_id: str, kb_id: str, ctx: _Ctx, start: dict[str, Any]) -> dict[str, Any]:
    """Rounds of chunk/copy -> embed batches -> finalise until the switch. Every value here comes
    from checkpointed step outputs, so a replay takes the same path. The cost cap is per run:
    `spent` (approx tokens sent to the provider in this run) plus each round's un-cached `pending`
    must stay within the limit."""
    if start["outcome"] == "missing":
        raise _cancel(start.get("reason", "document_deleted"))
    ctx.generation = start.get("generation")
    if start["outcome"] == "superseded":
        return {"mode": ctx.mode, "status": "superseded"}
    model: str = start["embedding_model"]
    index = 0  # batch number across rounds: request keys stay unique in the run
    batches = 0
    spent = 0
    idle_rounds = 0
    skip_chunking = start["outcome"] == "already_active"
    for round_ in range(MAX_ROUNDS):
        progressed = False
        if not skip_chunking:
            if ctx.mode == "ingest":
                prep = await kb_chunk_step(run_id, tenant_id, kb_id, str(ctx.document_id), int(ctx.doc_version or 0))
            else:
                prep = await kb_copy_step(run_id, tenant_id, kb_id, int(ctx.generation or 0))
            _raise_failure(prep)
            if prep.get("generation") is not None:
                ctx.generation = prep["generation"]
            model = prep.get("embedding_model") or model
            if prep["status"] == "superseded":
                return {"mode": ctx.mode, "status": "superseded"}
            if prep["status"] == "stale":
                idle_rounds = _idle(idle_rounds, False)
                continue
            if prep["status"] == "ok":
                progressed = prep.get("inserted", 0) > 0
                if spent + prep["pending_tokens"] > prep["limit"]:
                    raise IngestFailed(
                        "ingest_budget_exceeded", "IngestBudgetExceeded",
                        f"the ingest needs about {spent + prep['pending_tokens']} embedding tokens, over the limit "
                        f"of {prep['limit']} (EAP_KB_MAX_INGEST_TOKENS)",
                    )
                embedded, total = prep["embedded"], prep["total"]
                await append_event_step(
                    run_id, tenant_id, f"chunk:{round_}", INGEST_PROGRESS,
                    {
                        "phase": "chunked", "generation": ctx.generation, "embedded": embedded,
                        "total": total, "pending_tokens": prep["pending_tokens"],
                        "message": progress_message(embedded, total),
                    },
                )
                stale = False
                while True:
                    out = await kb_embed_batch_step(
                        run_id, tenant_id, kb_id, ctx.mode, int(ctx.generation or 0), model, index,
                        embedded, total, ctx.document_id, ctx.doc_version,
                    )
                    _raise_failure(out)
                    if out["status"] == "stale":
                        stale = True
                        break
                    if out["status"] == "done":
                        break
                    embedded = out["embedded"]
                    spent += out.get("tokens", 0)
                    if out.get("written", 0) or out.get("filled", 0):
                        progressed = True
                    if out.get("tokens", 0):
                        batches += 1
                    index += 1
                if stale:
                    idle_rounds = _idle(idle_rounds, progressed)
                    continue
            # prep["status"] == "already_active": straight to the (idempotent) finalisation
        skip_chunking = False
        fin = await kb_finalize_step(
            run_id, tenant_id, kb_id, ctx.mode, int(ctx.generation or 0), model, round_,
            ctx.document_id, ctx.doc_version,
        )
        if fin["status"] in ("switched", "already_active"):
            # The final counts come from the database (finalize's chunk_count: every chunk is
            # embedded at a switch), never from the running tally: a crash between a batch's cache
            # fill and its checkpoint would make the tally miss the filled rows on re-execution.
            return {
                "mode": ctx.mode, "status": fin["status"], "chunk_count": fin["chunk_count"],
                "embedded": fin["chunk_count"], "total": fin["chunk_count"],
                "generation": ctx.generation, "embedding_model": model, "batches": batches,
                "approx_tokens": spent,
            }
        if fin["status"] == "superseded":
            return {"mode": ctx.mode, "status": "superseded"}
        if fin["status"] == "missing":
            raise _cancel("document_deleted" if ctx.mode == "ingest" else "reindex_cancelled")
        # incomplete / stale_generation: another round (re-chunk / re-copy is idempotent)
        idle_rounds = _idle(idle_rounds, progressed)
    raise _not_converged(f"the ingest did not finish within {MAX_ROUNDS} rounds")


def _not_converged(message: str) -> IngestFailed:
    return IngestFailed(NOT_CONVERGED, "IngestNotConverged", message)


def _idle(idle_rounds: int, progressed: bool) -> int:
    """Count consecutive rounds that embedded and copied nothing; too many fail the run."""
    if progressed:
        return 0
    idle_rounds += 1
    if idle_rounds >= MAX_IDLE_ROUNDS:
        raise _not_converged(f"the ingest made no progress in {MAX_IDLE_ROUNDS} rounds")
    return idle_rounds


def _classify(exc: BaseException) -> IngestFailed:
    """The run's failure. Known failures are classified inside the steps (checkpointed, text-free
    failure dicts raised here as IngestFailed), so replay cannot diverge. Anything else escaping a
    step (a database error after DBOS's retries, a bug) is `internal_error`, never `embedding_failed`."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    chain: list[BaseException] = []
    while stack:
        cur = stack.pop(0)
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        chain.append(cur)
        attempts = getattr(cur, "errors", None)  # DBOSMaxStepRetriesExceeded: one per attempt
        if isinstance(attempts, list):
            stack.extend(reversed([a for a in attempts if isinstance(a, BaseException)]))
        for linked in (cur.__cause__, cur.__context__):
            if linked is not None:
                stack.append(linked)
    for cur in chain:
        if isinstance(cur, IngestFailed) or type(cur).__name__ == "IngestFailed":
            return IngestFailed(
                str(getattr(cur, "code", "internal_error")), str(getattr(cur, "error_type", "IngestFailed")),
                str(getattr(cur, "message", "the ingest failed")),
            )
    if any(_sqlstate(cur) in ("40P01", "40001") for cur in chain):
        return IngestFailed("internal_error", "DeadlockDetected", "the ingest kept conflicting with other writes; retry it")
    return IngestFailed("internal_error", exception_type(exc), "the ingest failed with an internal error")


async def _fail(run_id: str, tenant_id: str, kb_id: str, ctx: _Ctx, exc: BaseException) -> dict[str, Any]:
    failure = _classify(exc)
    log_failure(
        log, "kb_ingest run failed", exc, code=failure.code, error_type=failure.error_type,
        run_id=run_id, tenant_id=tenant_id,
    )
    counts: dict[str, Any] = {}
    if failure.code != INGEST_CANCELLED:
        counts = await kb_mark_failed_step(
            run_id, tenant_id, kb_id, ctx.mode, failure.code, ctx.generation, ctx.document_id, ctx.doc_version
        )
    data: dict[str, Any] = {"status": "failed", "error": failure.error, "error_code": failure.code}
    if counts:
        data.update(
            embedded=counts["embedded"], total=counts["total"],
            message=progress_message(counts["embedded"], counts["total"]),
        )
    await append_event_step(run_id, tenant_id, "end", "run_failed", data)
    await finish_run_step(run_id, tenant_id, "failed", None, failure.error)
    return {"status": "failed", "error": failure.error, "error_code": failure.code}
