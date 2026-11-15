"""Knowledge base ingestion (RAG and memory milestone, RM.10): the durable `eap.kb_ingest` workflow.

Hand-off (apps/api/src/eap_api/launcher.py, KB_INGEST_WORKFLOW_NAME): the API inserts a `runs` row of
kind 'kb_ingest' and enqueues `eap.kb_ingest(run_id, tenant_id, knowledge_base_id, input_json)` on
its own queue `eap-kb-ingest-fair` (eap_worker.config.KB_INGEST_QUEUE_NAME, partitioned by tenant
and knowledge base; at most EAP_KB_INGEST_CONCURRENCY, default 2, running per worker) with
workflow_id = run_id. Runs enqueued on the unpartitioned `eap-kb-ingest` or on `eap-agent-runs`
before those changes still run there. `input_json` is the run's canonical input:

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
    eap.kb_extract_chunk (ingest, RAG P1) in kb_chunk's slot for a WORKER-EXTRACTED document (rst, PDF,
                                        YAML, JSON, Doxygen Markdown: eap_knowledge.formats.extractor_for)
                                        of a run whose input has `embed_split: true`: the extractor id is
                                        recorded in eap.start_ingest's output (`extractor`). Re-read the
                                        document, parse it in the sandbox child (eap_worker.extract_sandbox),
                                        chunk it, upsert the rows with `metadata.eap` (extractor, version,
                                        page, condition), then the `extracted` event (key
                                        `extract:{doc_version}`, counts only); reuse cached embeddings,
                                        count what is left. The chunk rows ARE the stored extraction: a
                                        re-execution that finds this run's event and the rows returns
                                        the same total/embedded/pending counts without parsing, with
                                        `inserted` 0 (as kb_chunk's re-execution). Text/Markdown/HTML documents and
                                        runs without the flag keep eap.kb_chunk; reindex never parses
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
    -- or, for a run whose input has `embed_split: true` (every run the API creates since P1.C5),
       each batch is two steps, so a database retry of the write never calls the provider again:
    eap.kb_embed_call  (i = 0, 1, ...)  select, cache fill, the one provider call (retryable provider
                                        errors retried inside); returns the chunk ids, the vectors
                                        as float32 bytes, usage and event data (no text)
    eap.kb_embed_write                  only when the call returned vectors: the vectors, usage row
                                        and `ingest_progress` event (key `embed:batch:{i}`) in ONE
                                        transaction, or the refusal; retried on 40P01/40001/lost
                                        connection without a provider call
       Runs enqueued without the flag keep eap.kb_embed_batch, so in-flight workflows recover
       unchanged (the step layout differs only for new runs).
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
- A batch embedded (and charged) whose vectors the store then refuses (the generation switched, the
  knowledge base was deleted, the reindex cancelled, a dimension mismatch) still records its usage:
  an `embedding` event under the batch's key, `{phase: refused, refused: <reason>}`, with its
  run_model_requests row in one transaction. A re-execution finds that event and replays the
  refusal without a provider call. A stale batch advances the batch number, so the next round's
  request keys never collide with the refused one's.
- The run always ends: a failure in eap.start_ingest, in the run_started write or in
  eap.kb_mark_failed still writes run_failed and sets the status 'failed' (the document or knowledge
  base mark is best effort).
- `eap.kb_chunk` is idempotent (ON CONFLICT DO NOTHING per (document, version, generation,
  ordinal)); a finalisation is one transaction and answers 'already_active' when repeated.
- Failures are classified inside the steps and checkpointed as text-free failure dicts, so a replay
  takes the same path. `eap.kb_embed_batch` retries the provider itself (EMBED_ATTEMPTS, for
  embeddings.is_retryable errors: 408/409/429/5xx, connection errors, timeouts; a rate limit gets
  RATE_LIMIT_ATTEMPTS with a longer jittered backoff that honours Retry-After, capped: see
  embed_retry_delay) and turns every
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
import random
import sys
from array import array
from collections.abc import Callable
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
MAX_BATCH_CHUNKS = 128  # P1.C5: bounds the eap.kb_embed_call checkpoint (see _batch_chunks)
# Rounds of chunk/copy -> embed -> finalise. One normally; another only when the generation switched
# under an ingest ('stale_generation'), or documents were activated during a reindex ('incomplete').
# Rounds that make progress (rows inserted, copied or embedded) never fail the run, up to the
# absolute bound; MAX_IDLE_ROUNDS consecutive rounds without progress do (code ingest_not_converged).
MAX_ROUNDS = 50
MAX_IDLE_ROUNDS = 3
NOT_CONVERGED = "ingest_not_converged"
# Provider attempts per batch, inside eap.kb_embed_batch (retryable errors only), with backoff
# EMBED_RETRY_SECONDS * 2**attempt (plus up to 25 % jitter).
EMBED_ATTEMPTS = 3
EMBED_RETRY_SECONDS = 1.0
# A rate limit (HTTP 429 / ModelRateLimitError) gets more attempts and a longer, jittered backoff:
# RATE_LIMIT_RETRY_SECONDS * 2**attempt, at least the provider's Retry-After, at most
# RATE_LIMIT_MAX_DELAY_SECONDS per wait (about 3.5 minutes in the worst case over all attempts).
RATE_LIMIT_ATTEMPTS = 6
RATE_LIMIT_RETRY_SECONDS = 2.0
RATE_LIMIT_MAX_DELAY_SECONDS = 60.0

STEP_RETRY = {"retries_allowed": True, "max_attempts": 3, "interval_seconds": 1.0, "backoff_rate": 2.0}

# Failure codes (eap_schemas.knowledge.INGEST_FAILURE_CODES plus the platform's own) -> fixed message.
INGEST_CANCELLED = "ingest_cancelled"
# The workflow result's error_code of a run that was already terminal when its workflow started
# (loops D12; the same code as eap_worker.tool_run.RUN_ENDED).
RUN_ENDED = "run_ended"
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
    "embed_retry_delay",
    "is_rate_limited",
    "batch_request_key",
    "kb_chunk_step",
    "kb_copy_step",
    "kb_extract_chunk_step",
    "extract_event_key",
    "EMBED_SPLIT_KEY",
    "MAX_BATCH_CHUNKS",
    "embed_batch",
    "kb_embed_batch_step",
    "kb_embed_call_step",
    "kb_embed_write_step",
    "kb_finalize_step",
    "pack_vectors",
    "unpack_vectors",
    "kb_ingest",
    "kb_mark_failed_step",
    "max_ingest_tokens",
    "progress_message",
    "start_ingest_step",
    "validate_settings",
]


class IngestFailed(Exception):
    """The ingest run failed with a known code; `message` is fixed, user-safe text built from ids,
    model ids and counts only. `reason` (RAG P1) is the extraction error code of an
    `extraction_failed` from the sandbox (`document_encrypted`, ...), else None. Pickle-safe."""

    def __init__(self, code: str, error_type: str, message: str, reason: str | None = None) -> None:
        self.code = code
        self.error_type = error_type
        self.message = message
        self.reason = reason
        super().__init__(code, error_type, message, reason)

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
    """EAP_KB_EMBED_BATCH_CHUNKS: un-embedded chunks per batch (default 64; below 1 means 1, above
    MAX_BATCH_CHUNKS means MAX_BATCH_CHUNKS, so eap.kb_embed_call's checkpoint of the batch's
    float32 vectors stays far below DBOS's 2 MB guidance: 128 x 1536 x 4 bytes = 768 KiB)."""
    raw = os.environ.get(BATCH_CHUNKS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_BATCH_CHUNKS
    try:
        return min(MAX_BATCH_CHUNKS, max(1, int(raw)))
    except ValueError:
        raise ValueError(f"{BATCH_CHUNKS_ENV} must be a positive integer, got {raw!r}") from None


def validate_settings() -> None:
    """Parse the ingest settings that the steps read (EAP_KB_MAX_INGEST_TOKENS,
    EAP_KB_EMBED_BATCH_CHUNKS, and the EAP_KB_* extraction limits of the sandbox), so a bad value
    fails the worker at start, not each run."""
    from eap_worker.config import extract_limits

    max_ingest_tokens()
    _batch_chunks()
    extract_limits()


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
    classifies them itself. RAG P1: a sandbox child that could not be started (SandboxSpawnFailed:
    EAGAIN, ENOMEM, EMFILE) is transient too. Anything else fails at once."""
    if _sqlstate(exc) in ("40P01", "40001"):
        return True
    if type(exc).__name__ == "SandboxSpawnFailed":
        return True
    from sqlalchemy.exc import OperationalError

    return isinstance(exc, OperationalError) and getattr(exc, "connection_invalidated", False)


def _store() -> KnowledgeStore:
    return KnowledgeStore(get_runtime().engine)


def is_rate_limited(exc: BaseException) -> bool:
    """A provider rate limit: HTTP 429 or pydantic-ai's ModelRateLimitError."""
    from pydantic_ai.exceptions import ModelHTTPError, ModelRateLimitError

    if isinstance(exc, ModelRateLimitError):
        return True
    return isinstance(exc, ModelHTTPError) and exc.status_code == 429


def _retry_after(exc: BaseException) -> float | None:
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def embed_retry_delay(exc: BaseException, attempt: int, rand: Callable[[], float] = random.random) -> float:
    """Seconds to wait before the next embedding attempt (`attempt` is 0 for the first failure).

    A rate limit: RATE_LIMIT_RETRY_SECONDS * 2**attempt with up to 25 % jitter, at least the
    provider's Retry-After (pydantic-ai's `retry_after`: `retry-after-ms` / `Retry-After`), at most
    RATE_LIMIT_MAX_DELAY_SECONDS. Anything else: EMBED_RETRY_SECONDS * 2**attempt with up to 25 %
    jitter. Jitter spreads the retries of concurrent ingests that hit the limit together."""
    jitter = 1.0 + 0.25 * rand()
    if not is_rate_limited(exc):
        return EMBED_RETRY_SECONDS * (2**attempt) * jitter
    delay = RATE_LIMIT_RETRY_SECONDS * (2**attempt) * jitter
    after = _retry_after(exc)
    if after is not None:
        delay = max(delay, after)
    return min(delay, RATE_LIMIT_MAX_DELAY_SECONDS)


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


REFUSAL_REASONS = ("stale", "knowledge_base_deleted", "reindex_cancelled", "embedding_dimension_mismatch")


def refusal_outcome(reason: str, mode: str, model_id: str, embedded: int, total: int) -> dict[str, Any]:
    """The step result of a batch whose vectors the store refused after the provider call (the
    `refused` code of its `embedding` event): stale (chunk again), cancelled, or a failure."""
    if reason == "stale":
        return {"status": "stale"}
    if reason == "embedding_dimension_mismatch":
        return {**_embedding_failure(EmbeddingDimensionMismatch(model_id, 0, 0), model_id), "embedded": embedded, "total": total}
    if reason in ("knowledge_base_deleted", "reindex_cancelled"):
        return _cancelled(reason)
    return _failure("internal_error", "IngestFailed", "the ingest failed with an internal error")


def _worker_extractor(params: dict[str, Any], source: dict[str, Any]) -> str | None:
    """The worker extractor id of the source document (eap_knowledge.formats.extractor_for with the
    knowledge base's `chunking.extract`), or None: for API-extracted types, for a run without
    `embed_split` (enqueued by an earlier API), and for an unknown type or an invalid config (then
    eap.kb_chunk fails the run with `extraction_failed` as before)."""
    if params.get(EMBED_SPLIT_KEY) is not True:
        return None
    from eap_knowledge import extractor_for

    try:
        config = ChunkingConfig.model_validate(source["chunking"])
        return extractor_for(str(source["document"]["mime_type"]), config.extract)
    except Exception:  # noqa: BLE001 - unsupported or invalid: eap.kb_chunk reports it
        return None


def extract_event_key(doc_version: int) -> str:
    """The `extracted` event's key: one per document version in a run."""
    return f"extract:{int(doc_version)}"


def _scope(mode: str, document_id: str | None, doc_version: int | None) -> dict[str, Any]:
    return {"document_id": document_id, "doc_version": doc_version} if mode == "ingest" else {}


# --------------------------------------------------------------------------- steps


@DBOS.step(name="eap.start_ingest")
async def start_ingest_step(run_id: str, tenant_id: str, kb_id: str, input_json: str) -> dict[str, Any]:
    """Returns {"traceparent", "outcome": ok | missing | superseded | already_active, "reason"?,
    "generation"?, "embedding_model"?, "extractor"?}. Ingest 'ok' marks the document 'ingesting'.

    `extractor` (RAG P1) names the worker extractor of a worker-extracted document, only for a run
    whose input has `embed_split: true` (created by an API that knows worker formats): the workflow
    then runs eap.kb_extract_chunk instead of eap.kb_chunk. A run recorded before (no key in this
    step's checkpointed output) or without the flag keeps eap.kb_chunk.

    Or {"traceparent", "stopped": True, "status", "error"} when the run is already terminal
    (loops D12: mark_running refused it): nothing is read, marked or embedded, and the workflow
    returns without an event. A checkpoint recorded before the key existed has no `stopped`."""
    rt = get_runtime()
    store = _store()
    params = json.loads(input_json)

    def work() -> dict[str, Any]:
        # The API inserts the row first; an idempotent safety net, as for tool runs.
        rt.run_store.create_kb_ingest_run(tenant_id, kb_id, input_json, UUID(run_id))
        # False: the run is terminal and is never revived (loops D12), so it must not run.
        started = rt.run_store.mark_running(tenant_id, UUID(run_id))
        if started is not False:
            rt.run_store.heartbeat(tenant_id, UUID(run_id))
        run = rt.run_store.get_run(tenant_id, UUID(run_id)) or {}
        out: dict[str, Any] = {"traceparent": run.get("traceparent")}
        if started is False:
            return {**out, "stopped": True, "status": run.get("status"), "error": run.get("error")}
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
        extractor = _worker_extractor(params, source)
        if extractor is not None:
            base["extractor"] = extractor
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


def _extraction_failure(exc: Any) -> dict[str, Any]:
    """A worker extraction failure (eap_knowledge.ExtractionFailed) as a text-free failure dict: the
    run fails `extraction_failed` (INGEST_FAILURE_CODES is unchanged) with the reason's fixed,
    user-safe message, and `reason` carries the extraction code (`document_encrypted`, ...). Never
    the exception's text or detail, which may name parser internals."""
    return {**_failure("extraction_failed", "ExtractionFailed", exc.user_message), "reason": exc.code}


# The extraction code that ends an ingest as an EMPTY document rather than a failure (RAG P1
# quality fix): the document is 'ready' with 0 chunks, and its `extracted` event carries
# `empty_reason` so Studio and the API show why.
EMPTY_REASON = "no_extractable_text"


def _finish_empty(
    run_id: str, tenant_id: str, kb_id: str, document_id: str, doc_version: int, extractor: str,
    base: dict[str, Any], key: str, limit: int, result: Any = None,
) -> dict[str, Any]:
    """kb_extract_chunk's outcome for a version with no extractable text, on a document with no
    active chunks: the `extracted` event (counts only, `empty_reason`), no rows, and the usual "ok"
    counts (all 0). The workflow then embeds nothing and finalize switches to the empty version:
    status 'ready', chunk_count 0, no error. Idempotent: the event is keyed, and a re-execution
    (there are no rows to find) parses again and writes nothing new."""
    from eap_schemas.knowledge import ExtractedEventData

    rt = get_runtime()
    store = _store()
    document = result.document if result is not None else None
    data = ExtractedEventData(
        extractor=extractor,
        extractor_version=_extractor_version(extractor),
        sections=len(document.sections) if document is not None else 0,
        chars=document.extracted_chars if document is not None else 0,
        pages=result.pages if result is not None else None,
        warnings=(len(document.warnings) or None) if document is not None else None,
        empty_reason=EMPTY_REASON,
    ).model_dump(mode="json")
    rt.event_log.append(tenant_id, UUID(str(run_id)), key, "extracted", data)
    progress = store.embedding_progress(
        tenant_id, kb_id, base["generation"], document_id=document_id, doc_version=doc_version
    )
    return {
        **base,
        "status": "ok",
        "total": progress.total,
        "embedded": progress.embedded,
        "pending_tokens": progress.pending_tokens,
        "limit": limit,
        "inserted": 0,
    }


def _extracted_event_data(extractor: str, result: Any, chunks: list[Any]) -> dict[str, Any]:
    from eap_schemas.knowledge import MAX_IMAGE_ONLY_PAGES_LISTED, ExtractedEventData

    document = result.document
    conditions = sum(1 for s in document.sections if s.condition)
    return ExtractedEventData(
        extractor=extractor,
        extractor_version=_extractor_version(extractor),
        sections=len(document.sections),
        chars=document.extracted_chars,
        pages=result.pages,
        image_only_pages=tuple(result.image_only_pages[:MAX_IMAGE_ONLY_PAGES_LISTED]) or None,
        conditions=conditions or None,
        warnings=len(document.warnings) or None,
    ).model_dump(mode="json")


def _extractor_version(extractor: str) -> int:
    from eap_knowledge import EXTRACTOR_VERSIONS

    return EXTRACTOR_VERSIONS[extractor]


def _chunk_extract_metadata(extractor: str, chunks: list[Any]) -> list[dict[str, Any]]:
    """Each chunk's `metadata.eap` record (eap_schemas.knowledge.ChunkExtractMetadata)."""
    from eap_schemas.knowledge import ChunkExtractMetadata

    version = _extractor_version(extractor)
    return [
        ChunkExtractMetadata(
            extractor=extractor, extractor_version=version, page=c.page, page_end=c.page_end, condition=c.condition
        ).model_dump(mode="json")
        for c in chunks
    ]


@DBOS.step(name="eap.kb_extract_chunk", should_retry=_transient, **STEP_RETRY)
async def kb_extract_chunk_step(
    run_id: str, tenant_id: str, kb_id: str, document_id: str, doc_version: int, extractor: str
) -> dict[str, Any]:
    """eap.kb_chunk for a worker-extracted document (RAG P1): parse the version in the sandbox child,
    chunk it, upsert its rows with `metadata.eap`, then write the `extracted` event (once per run and
    version: `extract:{doc_version}`, first write wins). Same result shape as kb_chunk_step.

    A re-execution that finds this run's `extracted` event AND the version's rows in the generation
    (the step committed, then crashed before its checkpoint) returns the same `total`, `embedded`
    and `pending_tokens` without parsing, with `inserted` 0, exactly as a re-executed eap.kb_chunk
    (whose ON CONFLICT DO NOTHING insert also counts 0); `inserted` only feeds the idle-round
    count, so this costs at most one idle round. A crash before the event re-parses once (the
    upsert inserts nothing new; the event is written once). A retry run after an extractor upgrade
    replaces the version's older rows (upsert_chunks). Extraction failures (sandbox codes) are checkpointed as failure dicts, never retried:
    they are deterministic. Database errors propagate (DBOS retries 40P01/40001/lost connections)."""
    from eap_knowledge import ExtractionFailed, InvalidChunkingConfig, chunk
    from pydantic import ValidationError

    from eap_worker import extract_sandbox

    rt = get_runtime()
    store = _store()
    limit = max_ingest_tokens()
    key = extract_event_key(doc_version)

    def read() -> dict[str, Any]:
        source = store.get_ingest_source(tenant_id, document_id, doc_version)
        if source is None or source["document"]["kb_id"] != str(kb_id):
            kb_gone = store.get_knowledge_base(tenant_id, kb_id) is None
            return {"outcome": _cancelled("knowledge_base_deleted" if kb_gone else "document_deleted")}
        generation, model = source["generation"], source["embedding_model"]
        base = {"generation": generation, "embedding_model": model}
        if source["already_active"]:
            return {"outcome": {**base, "status": "already_active"}}
        if source["superseded"]:
            return {"outcome": {**base, "status": "superseded"}}
        if rt.event_log.get_event(tenant_id, run_id, key) is not None:
            progress = store.embedding_progress(
                tenant_id, kb_id, generation, document_id=document_id, doc_version=doc_version
            )
            if progress.total > 0:  # committed by an earlier execution of this step: no parse
                return {"base": base, "reuse": True}
        return {"base": base, "source": source}

    state = await asyncio.to_thread(read)
    if "outcome" in state:
        return state["outcome"]
    base = state["base"]
    inserted = 0
    if not state.get("reuse"):
        source = state.pop("source")
        doc = source["document"]
        metadata = doc["metadata"] or {}  # the version's snapshot, as eap.kb_chunk uses
        try:
            config = ChunkingConfig.model_validate(source["chunking"])
        except (InvalidChunkingConfig, ValidationError):
            return _failure("extraction_failed", "InvalidChunkingConfig", "the knowledge base's chunking config is invalid")
        # A version may become an empty one (no extractable text) only while the document has no
        # searchable text to lose: never switch away from an active version with chunks.
        # PDFs never become empty: a PDF with no text has pages (images) that a later image extractor
        # can read, so it stays failed and a re-extract selects it.
        may_be_empty = extractor != "pdf" and not (doc["active_version"] is not None and (doc["chunk_count"] or 0) > 0)
        try:
            result = await extract_sandbox.extract_in_sandbox(
                extractor,
                doc["content"],
                title=doc["title"] or "",
                source_url=doc["source_url"],
                options=config.extract.model_dump(mode="json") if config.extract is not None else None,
                max_tokens=config.max_tokens,
            )
        except ExtractionFailed as exc:
            log.info("kb_ingest run %s extraction failed: %s", run_id, exc.code)
            if exc.code == EMPTY_REASON and may_be_empty:
                return await asyncio.to_thread(_finish_empty, run_id, tenant_id, kb_id, document_id, doc_version,
                                               extractor, base, key, limit)
            return _extraction_failure(exc)
        del source, doc
        crash_point("kb_extract_sandbox")
        try:
            chunks = await asyncio.to_thread(chunk, result.document, config)
        except InvalidChunkingConfig:
            return _failure("extraction_failed", "InvalidChunkingConfig", "the knowledge base's chunking config is invalid")
        if result.document.extracted_chars == 0 or not chunks:
            if may_be_empty:
                # A toctree- or include-only rst page, an empty YAML file: ready with 0 chunks, the
                # `extracted` event saying why. Not a failure.
                return await asyncio.to_thread(_finish_empty, run_id, tenant_id, kb_id, document_id, doc_version,
                                               extractor, base, key, limit, result)
            # Fail closed: never switch to an empty version (that would delete the active one's chunks).
            return _extraction_failure(ExtractionFailed(EMPTY_REASON))
        event_data = _extracted_event_data(extractor, result, chunks)
        records = _chunk_extract_metadata(extractor, chunks)

        def write() -> dict[str, Any] | int:
            try:
                upserted = store.upsert_chunks(
                    tenant_id, document_id, doc_version, base["generation"], chunks,
                    metadata=metadata, extract_metadata=records,
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
            rt.event_log.append(tenant_id, UUID(str(run_id)), key, "extracted", event_data)
            return upserted.inserted

        written = await asyncio.to_thread(write)
        if isinstance(written, dict):
            return written
        inserted = written
        crash_point("kb_extract_chunk")

    def finish() -> dict[str, Any]:
        generation = base["generation"]
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
            "inserted": inserted,
        }

    return await asyncio.to_thread(finish)


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


def _gone_outcome(exc: BaseException, mode: str) -> dict[str, Any]:
    if isinstance(exc, KnowledgeBaseNotFound):
        return _cancelled("knowledge_base_deleted")
    if mode == "reindex":  # a reindex generation no longer pending: cancelled (or switched)
        return _cancelled("reindex_cancelled")
    return {"status": "stale"}  # the active generation switched under an ingest: chunk again


def _recorded_batch(rt: Any, tenant_id: str, run_id: str, key: str, mode: str, embedding_model: str,
                    embedded_before: int, total: int) -> dict[str, Any] | None:
    """The outcome a committed batch recorded (its event), or None. Committed before a crash (after
    the commit, before the checkpoint): no provider call, nothing written twice."""
    done = rt.event_log.get_event(tenant_id, run_id, key)
    if done is not None and done.type == _embeddings.EMBEDDING_EVENT_TYPE:
        # The batch was embedded and paid for, but the store refused the vectors: replay the refusal.
        return refusal_outcome(str(done.data.get("refused", "")), mode, embedding_model, embedded_before, total)
    if done is not None:
        d = done.data
        return {
            "status": "embedded", "embedded": int(d.get("embedded", embedded_before)),
            "total": int(d.get("total", total)), "written": int(d.get("written", 0)),
            "filled": int(d.get("filled", 0)), "tokens": int(d.get("approx_tokens", 0)), "replayed": True,
        }
    return None


def _prepare_batch(
    rt: Any, store: KnowledgeStore, *, run_id: str, tenant_id: str, kb_id: str, mode: str, generation: int,
    embedding_model: str, key: str, embedded_before: int, total: int, scope: dict[str, Any],
) -> dict[str, Any]:
    """Select the batch and fill it from the cache. {"status": "pending", "batch", "filled"} or a
    final outcome (replayed, done, stale, cancelled)."""
    recorded = _recorded_batch(rt, tenant_id, run_id, key, mode, embedding_model, embedded_before, total)
    if recorded is not None:
        return recorded
    try:
        batch = store.next_unembedded_batch(
            tenant_id, kb_id, generation, **scope, max_chunks=_batch_chunks(), max_tokens=DEFAULT_BATCH_TOKENS
        )
        if not batch:
            return {"status": "done", "embedded": embedded_before, "total": total, "written": 0, "filled": 0, "tokens": 0}
        filled = store.fill_cached_embeddings(tenant_id, kb_id, generation, [c.id for c in batch])
    except (KnowledgeBaseNotFound, GenerationNotWritable) as exc:
        return _gone_outcome(exc, mode)
    return {"status": "pending", "batch": [c for c in batch if c.id not in filled], "filled": len(filled)}


def _batch_texts(batch: list[Any]) -> tuple[list[str], dict[str, int], int]:
    """(texts, slot by embed_text_hash, approx tokens): identical texts in one batch embedded once."""
    texts: list[str] = []
    slot: dict[str, int] = {}
    tokens = 0
    for c in batch:
        if c.embed_text_hash not in slot:
            slot[c.embed_text_hash] = len(texts)
            texts.append(c.embed_text)
            tokens += c.approx_tokens
    return texts, slot, tokens


async def _call_provider(
    rt: Any, *, run_id: str, tenant_id: str, key: str, embedding_model: str, texts: list[str],
) -> tuple[Any, dict[str, Any] | None]:
    """(the EmbeddingCall, None) or (None, a text-free failure dict). Retryable provider errors are
    retried here (EMBED_ATTEMPTS, RATE_LIMIT_ATTEMPTS for a rate limit); nothing is re-raised."""
    for attempt in range(max(EMBED_ATTEMPTS, RATE_LIMIT_ATTEMPTS)):
        try:
            call = await _embeddings.embed_documents(
                rt.settings, tenant_id=tenant_id, run_id=run_id, request_key=key, model_id=embedding_model, texts=texts
            )
            return call, None
        except Exception as exc:  # noqa: BLE001 - classified here: a text-free failure dict, never re-raised
            limited = is_rate_limited(exc)
            if is_retryable(exc) and attempt + 1 < (RATE_LIMIT_ATTEMPTS if limited else EMBED_ATTEMPTS):
                log_failure(
                    log, "kb ingest embedding attempt failed; retrying", exc,
                    code="embedding_rate_limited" if limited else "embedding_retry",
                    run_id=run_id, tenant_id=tenant_id,
                )
                await asyncio.sleep(embed_retry_delay(exc, attempt))
                continue
            failure = _embedding_failure(exc, embedding_model)
            log_failure(log, "kb ingest embedding failed", exc, code=failure["code"], run_id=run_id, tenant_id=tenant_id)
            return None, failure
    raise AssertionError("unreachable")  # pragma: no cover - the last attempt returns


def _record_refusal(
    rt: Any, *, run_id: str, tenant_id: str, mode: str, embedding_model: str, index: int,
    event_data: dict[str, Any], usage: dict[str, Any], reason: str, embedded: int, total: int,
) -> dict[str, Any]:
    """The provider call was made (and is charged) but its vectors are not stored: record its usage
    as an `embedding` event under the batch's key, `{phase: refused, refused: <reason>}`, with its
    run_model_requests row in ONE transaction (first write wins, so a re-execution never records it
    twice). The event also makes a re-execution replay this refusal without another provider call."""
    key = batch_request_key(index)
    rt.event_log.append(
        tenant_id, UUID(str(run_id)), key, _embeddings.EMBEDDING_EVENT_TYPE,
        {**event_data, "phase": "refused", "batch": index, "refused": reason}, usage=usage,
    )
    return refusal_outcome(reason, mode, embedding_model, embedded, total)


def _write_batch(
    rt: Any, store: KnowledgeStore, *, run_id: str, tenant_id: str, kb_id: str, mode: str, generation: int,
    embedding_model: str, index: int, embedded_before: int, total: int, vectors: dict[str, Any], filled: int,
    tokens: int, event_data: dict[str, Any], usage: dict[str, Any], requests: int,
) -> dict[str, Any]:
    """ONE transaction: the vectors, the usage row and the batch's `ingest_progress` event
    (write_embeddings); or, when the store refuses the vectors, the usage with an `embedding`
    refusal event. Database errors propagate (the step's DBOS retry)."""
    key = batch_request_key(index)
    expected = embedded_before + filled + len(vectors)
    data = {
        **event_data,
        "phase": "embedding",
        "batch": index,
        "chunks": len(vectors),
        "filled": filled,
        "written": len(vectors),
        "approx_tokens": tokens,
        "embedded": expected,
        "total": total,
        "message": progress_message(expected, total),
    }

    def refused(reason: str) -> dict[str, Any]:
        return _record_refusal(rt, run_id=run_id, tenant_id=tenant_id, mode=mode, embedding_model=embedding_model,
                               index=index, event_data=event_data, usage=usage, reason=reason,
                               embedded=embedded_before + filled, total=total)

    try:
        result = store.write_embeddings(
            tenant_id, kb_id, generation, vectors, embedding_model=embedding_model, run_id=run_id,
            event_key=key, event_type=INGEST_PROGRESS, event_data=data, usage=usage,
        )
    except KnowledgeBaseNotFound:
        return refused("knowledge_base_deleted")
    except GenerationNotWritable:
        return refused("reindex_cancelled" if mode == "reindex" else "stale")
    except GenerationModelMismatch:
        return refused("stale" if mode == "ingest" else "reindex_cancelled")
    except EmbeddingDimensionMismatch:
        return refused("embedding_dimension_mismatch")
    return {
        "status": "embedded", "embedded": embedded_before + filled + result.written, "total": total,
        "written": result.written, "filled": filled, "tokens": tokens, "requests": requests,
    }


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
    Used by runs enqueued WITHOUT `embed_split` (before P1.C5); new runs use eap.kb_embed_call and
    eap.kb_embed_write, so a database retry of the write never calls the provider again.

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
    prepared = await asyncio.to_thread(
        lambda: _prepare_batch(
            rt, store, run_id=run_id, tenant_id=tenant_id, kb_id=kb_id, mode=mode, generation=generation,
            embedding_model=embedding_model, key=key, embedded_before=embedded_before, total=total, scope=scope,
        )
    )
    if prepared["status"] != "pending":
        return prepared
    batch, filled = prepared["batch"], prepared["filled"]
    if not batch:  # all from the cache: nothing to embed, no event, no usage
        return {
            "status": "embedded", "embedded": embedded_before + filled, "total": total,
            "written": 0, "filled": filled, "tokens": 0,
        }
    texts, slot, tokens = _batch_texts(batch)
    call, failure = await _call_provider(
        rt, run_id=run_id, tenant_id=tenant_id, key=key, embedding_model=embedding_model, texts=texts
    )
    if failure is not None:
        return {**failure, "embedded": embedded_before + filled, "total": total}
    crash_point("kb_batch_before_commit", event_key=key)
    vectors = {c.id: call.vectors[slot[c.embed_text_hash]] for c in batch}
    result = await asyncio.to_thread(
        lambda: _write_batch(
            rt, store, run_id=run_id, tenant_id=tenant_id, kb_id=kb_id, mode=mode, generation=generation,
            embedding_model=embedding_model, index=index, embedded_before=embedded_before, total=total,
            vectors=vectors, filled=filled, tokens=tokens, event_data=call.event_data, usage=call.usage,
            requests=call.requests,
        )
    )
    if result["status"] == "embedded":
        crash_point("kb_batch_after_commit", event_key=key)
    return result


# --------------------------------------------------------------------------- the embed split (P1.C5)

EMBED_SPLIT_KEY = "embed_split"
# The call step's checkpoint holds the batch's vectors as float32 bytes (what pgvector stores):
# at most EAP_KB_EMBED_BATCH_CHUNKS (capped at MAX_BATCH_CHUNKS) distinct texts x 1536 dimensions x
# 4 bytes, so 64 x 1536 = 384 KiB raw (DBOS stores the pickle base64-encoded, about 4/3 of that).
VECTOR_FORMAT = "f32"


def pack_vectors(vectors: Any) -> tuple[bytes, int]:
    """(little-endian float32 bytes, dimensions) of equal-length vectors."""
    rows = [list(v) for v in vectors]
    dims = len(rows[0]) if rows else 0
    if any(len(r) != dims for r in rows):
        raise ValueError("vectors of different lengths")
    packed = array("f", (x for r in rows for x in r))
    if sys.byteorder != "little":  # pragma: no cover - every supported host is little-endian
        packed.byteswap()
    return packed.tobytes(), dims


def unpack_vectors(blob: bytes, dims: int) -> list[list[float]]:
    values = array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":  # pragma: no cover
        values.byteswap()
    if dims <= 0:
        return []
    return [values[i:i + dims].tolist() for i in range(0, len(values), dims)]


@DBOS.step(name="eap.kb_embed_call", should_retry=_transient, **STEP_RETRY)
async def kb_embed_call_step(
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
    """The first half of a batch (runs with `embed_split`): select it, fill it from the cache, and
    make the ONE provider call (retrying retryable provider errors itself, like eap.kb_embed_batch).
    Database work happens only before the call, so a DBOS retry of this step (40P01, 40001, a lost
    connection) never follows a provider call; after the call the step only returns.

    Returns a final outcome (as eap.kb_embed_batch: replayed, done, stale, cancelled, failed, or
    embedded from the cache alone), or {"status": "called", "chunk_ids", "slots" (the vector of each
    chunk), "vectors" (float32 bytes), "dimensions", "usage", "event_data", "requests", "filled",
    "tokens"}, which the workflow hands to eap.kb_embed_write. No chunk text."""
    rt = get_runtime()
    store = _store()
    key = batch_request_key(index)
    scope = _scope(mode, document_id, doc_version)
    prepared = await asyncio.to_thread(
        lambda: _prepare_batch(
            rt, store, run_id=run_id, tenant_id=tenant_id, kb_id=kb_id, mode=mode, generation=generation,
            embedding_model=embedding_model, key=key, embedded_before=embedded_before, total=total, scope=scope,
        )
    )
    if prepared["status"] != "pending":
        return prepared
    batch, filled = prepared["batch"], prepared["filled"]
    if not batch:  # all from the cache: nothing to embed, no event, no usage
        return {
            "status": "embedded", "embedded": embedded_before + filled, "total": total,
            "written": 0, "filled": filled, "tokens": 0,
        }
    texts, slot, tokens = _batch_texts(batch)
    call, failure = await _call_provider(
        rt, run_id=run_id, tenant_id=tenant_id, key=key, embedding_model=embedding_model, texts=texts
    )
    if failure is not None:
        return {**failure, "embedded": embedded_before + filled, "total": total}
    try:
        blob, dims = pack_vectors(call.vectors)
    except ValueError:
        # Vectors of different lengths (a misbehaving provider): the call was charged, so its usage
        # is recorded with the refusal, as the write's EmbeddingDimensionMismatch would.
        return await asyncio.to_thread(
            lambda: _record_refusal(
                rt, run_id=run_id, tenant_id=tenant_id, mode=mode, embedding_model=embedding_model, index=index,
                event_data=dict(call.event_data), usage=dict(call.usage), reason="embedding_dimension_mismatch",
                embedded=embedded_before + filled, total=total,
            )
        )
    crash_point("kb_embed_after_call", event_key=key)
    return {
        "status": "called",
        "chunk_ids": [str(c.id) for c in batch],
        "slots": [slot[c.embed_text_hash] for c in batch],
        "vectors": blob,
        "vector_format": VECTOR_FORMAT,
        "dimensions": dims,
        "usage": dict(call.usage),
        "event_data": dict(call.event_data),
        "requests": int(call.requests),
        "filled": filled,
        "tokens": tokens,
    }


@DBOS.step(name="eap.kb_embed_write", should_retry=_transient, **STEP_RETRY)
async def kb_embed_write_step(
    run_id: str,
    tenant_id: str,
    kb_id: str,
    mode: str,
    generation: int,
    embedding_model: str,
    index: int,
    embedded_before: int,
    total: int,
    called: dict[str, Any],
) -> dict[str, Any]:
    """The second half of a batch (runs with `embed_split`): ONE transaction with the vectors of
    `called` (eap.kb_embed_call's checkpointed output), the usage row and the `ingest_progress`
    event, both under `embed:batch:{index}`; or the refusal path (usage with an `embedding` event,
    phase refused). Never calls the provider: a DBOS retry (40P01, 40001, a lost connection) or a
    re-execution after a crash writes from the same checkpointed vectors. A write already committed
    (its event exists) answers its recorded counts. Returns the eap.kb_embed_batch outcome shape."""
    rt = get_runtime()
    store = _store()
    key = batch_request_key(index)
    crash_point("kb_batch_before_commit", event_key=key)

    def work() -> dict[str, Any]:
        recorded = _recorded_batch(rt, tenant_id, run_id, key, mode, embedding_model, embedded_before, total)
        if recorded is not None:
            return recorded
        rows = unpack_vectors(called["vectors"], int(called["dimensions"]))
        vectors = {cid: rows[i] for cid, i in zip(called["chunk_ids"], called["slots"], strict=True)}
        return _write_batch(
            rt, store, run_id=run_id, tenant_id=tenant_id, kb_id=kb_id, mode=mode, generation=generation,
            embedding_model=embedding_model, index=index, embedded_before=embedded_before, total=total,
            vectors=vectors, filled=int(called["filled"]), tokens=int(called["tokens"]),
            event_data=called["event_data"], usage=called["usage"], requests=int(called["requests"]),
        )

    result = await asyncio.to_thread(work)
    if result["status"] == "embedded" and not result.get("replayed"):
        crash_point("kb_batch_after_commit", event_key=key)
    return result


async def embed_batch(
    split: bool, run_id: str, tenant_id: str, kb_id: str, mode: str, generation: int, embedding_model: str,
    index: int, embedded_before: int, total: int, document_id: str | None, doc_version: int | None,
) -> dict[str, Any]:
    """Workflow code: one batch, with the split steps (`embed_split` runs) or eap.kb_embed_batch."""
    if not split:
        return await kb_embed_batch_step(
            run_id, tenant_id, kb_id, mode, generation, embedding_model, index, embedded_before, total,
            document_id, doc_version,
        )
    called = await kb_embed_call_step(
        run_id, tenant_id, kb_id, mode, generation, embedding_model, index, embedded_before, total,
        document_id, doc_version,
    )
    if called["status"] != "called":
        return called
    return await kb_embed_write_step(
        run_id, tenant_id, kb_id, mode, generation, embedding_model, index, embedded_before, total, called,
    )


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
    message: str | None = None,
) -> dict[str, Any]:
    """Record the failure on the document ('failed', "<code>: N of M sections embedded") or the
    knowledge base ('reindex_failed'). The active version or generation stays searchable. Returns
    the counts ({"embedded", "total"}; zeros when nothing was chunked).

    With `message` (RAG P1: a sandbox extraction failure's fixed, user-safe message) the document's
    error is "<code>: <message>" instead, e.g. "extraction_failed: The PDF is encrypted with a
    password." Old runs never pass it, so their recorded behaviour is unchanged."""
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
        error = f"{code}: {message}" if message else f"{code}: {progress_message(progress.embedded, progress.total)}"
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
        try:
            params = json.loads(input_json)
            start = await start_ingest_step(run_id, tenant_id, kb_id, input_json)
        except Exception as exc:  # noqa: BLE001 - a run that cannot start still ends failed, never 'running'
            return await _fail(run_id, tenant_id, kb_id, _ctx_of(input_json), exc)
        if start.get("stopped"):
            # Terminal before it started (loops D12): no event, no document change, no embedding.
            log.warning("kb_ingest run %s not started: the run is already %s", run_id, start.get("status"))
            return {"status": start.get("status") or "failed", "error": start.get("error"), "error_code": RUN_ENDED}
    with run_span(run_id, tenant_id, "", "", traceparent=start.get("traceparent")) as span:
        span.set_attribute("eap.run_kind", "kb_ingest")
        span.set_attribute("eap.knowledge_base_id", kb_id)
        log.info("kb_ingest run %s started: tenant=%s kb=%s", run_id, tenant_id, kb_id)
        result = await _ingest_body(run_id, tenant_id, kb_id, params, start)
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
        self.embed_split = False  # P1.C5: runs.input.embed_split (new runs); else eap.kb_embed_batch
        self.extractor: str | None = None  # P1.A3: eap.start_ingest's `extractor` -> eap.kb_extract_chunk


def _ctx_of(input_json: str) -> _Ctx:
    """The run's context from its input, tolerating a malformed one (the failure path's use)."""
    try:
        params = json.loads(input_json)
        if "reindex" in params:
            ctx = _Ctx("reindex", None, None)
            ctx.generation = int(params["reindex"])
            return ctx
        return _Ctx("ingest", str(params["document_id"]), int(params["doc_version"]))
    except Exception:  # noqa: BLE001 - only ids for the failure record
        return _Ctx("ingest", None, None)


def _cancel(reason: str) -> IngestFailed:
    return IngestFailed(INGEST_CANCELLED, "IngestCancelled", _CANCEL_MESSAGES.get(reason, "the ingest was cancelled"))


def _raise_failure(out: dict[str, Any]) -> None:
    if out["status"] == "failed":
        raise IngestFailed(out["code"], out["error_type"], out["message"], out.get("reason"))
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
    ctx.embed_split = params.get(EMBED_SPLIT_KEY) is True
    started: dict[str, Any] = {"kind": "kb_ingest", "mode": mode, "knowledge_base_id": kb_id}
    if mode == "reindex":
        started["generation"] = int(params["reindex"])
    else:
        started.update(document_id=ctx.document_id, doc_version=ctx.doc_version)
    heartbeat = asyncio.create_task(_heartbeat(run_id, tenant_id))
    try:
        # Inside the try: a failed run_started write (after DBOS's retries) still fails the run.
        await append_event_step(run_id, tenant_id, "start", "run_started", started)
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
    ctx.extractor = start.get("extractor") if ctx.mode == "ingest" else None
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
            if ctx.mode == "ingest" and ctx.extractor is not None:
                prep = await kb_extract_chunk_step(
                    run_id, tenant_id, kb_id, str(ctx.document_id), int(ctx.doc_version or 0), ctx.extractor
                )
            elif ctx.mode == "ingest":
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
                    out = await embed_batch(
                        ctx.embed_split, run_id, tenant_id, kb_id, ctx.mode, int(ctx.generation or 0), model,
                        index, embedded, total, ctx.document_id, ctx.doc_version,
                    )
                    _raise_failure(out)
                    if out["status"] == "stale":
                        # A refused batch may have recorded usage under this index's request key:
                        # the next round's batches must use new keys, or their usage rows conflict.
                        index += 1
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
            reason = getattr(cur, "reason", None)
            return IngestFailed(
                str(getattr(cur, "code", "internal_error")), str(getattr(cur, "error_type", "IngestFailed")),
                str(getattr(cur, "message", "the ingest failed")), reason if isinstance(reason, str) else None,
            )
    if any(_sqlstate(cur) in ("40P01", "40001") for cur in chain):
        return IngestFailed("internal_error", "DeadlockDetected", "the ingest kept conflicting with other writes; retry it")
    return IngestFailed("internal_error", exception_type(exc), "the ingest failed with an internal error")


async def _fail(run_id: str, tenant_id: str, kb_id: str, ctx: _Ctx, exc: BaseException) -> dict[str, Any]:
    """End the run failed: mark the document / knowledge base (best effort: a failure to mark never
    keeps the run 'running'), then the durable run_failed event and the status."""
    failure = _classify(exc)
    log_failure(
        log, "kb_ingest run failed", exc, code=failure.code, error_type=failure.error_type,
        run_id=run_id, tenant_id=tenant_id,
    )
    counts: dict[str, Any] = {}
    if failure.code != INGEST_CANCELLED and (ctx.mode == "reindex" or ctx.document_id is not None):
        try:
            if failure.reason is not None:  # a sandbox extraction failure: its fixed message on the document
                counts = await kb_mark_failed_step(
                    run_id, tenant_id, kb_id, ctx.mode, failure.code, ctx.generation, ctx.document_id,
                    ctx.doc_version, failure.message,
                )
            else:
                counts = await kb_mark_failed_step(
                    run_id, tenant_id, kb_id, ctx.mode, failure.code, ctx.generation, ctx.document_id, ctx.doc_version
                )
        except Exception as mark_exc:  # noqa: BLE001 - the run must still end failed
            log_failure(log, "kb_ingest mark-failed failed", mark_exc, code="kb_mark_failed_failed",
                        run_id=run_id, tenant_id=tenant_id)
            counts = {}
    data: dict[str, Any] = {"status": "failed", "error": failure.error, "error_code": failure.code}
    if failure.reason is not None:
        data["extraction_code"] = failure.reason
    if counts:
        data.update(
            embedded=counts["embedded"], total=counts["total"],
            message=progress_message(counts["embedded"], counts["total"]),
        )
    await append_event_step(run_id, tenant_id, "end", "run_failed", data)
    await finish_run_step(run_id, tenant_id, "failed", None, failure.error)
    return {"status": "failed", "error": failure.error, "error_code": failure.code}
