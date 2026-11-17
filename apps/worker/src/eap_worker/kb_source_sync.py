"""Knowledge-base source sync: the native DBOS workflow `eap.kb_source_sync` (design: Native DBOS
workflow sequence).

    eap.kb_source_sync(run_id, tenant_id, source_id, request_json)   -> {status, error_code, error, report}

`request_json`: `{"mode": targeted|incremental|full, "part_numbers": [<= 100] (targeted only),
"max_segments"?, "max_seconds"?, "page_budget"? (1..25), "call_budget"? (provider calls per segment),
"restart"? (full / incremental: start a new walk instead of resuming the stored cursor)}`.

Root workflow (control flow only; clock, network and database live in steps):

    eap.kb_source_start        validate, load the immutable source / connection / config snapshot and the
                               limits (checkpointed: a replay never re-reads them), create the
                               `kb_source_syncs` row, mark the run running
    repeat n = 1, 2, ...:
      child workflow `{run_id}:seg:{n}` (eap.kb_source_segment) running one bounded step
      eap.kb_source_sync_segment  resolve secrets INSIDE the step, run `adapter.sync_segment` for
                               10-25 pages or <= 20 targeted parts, upsert through the stores, record and
                               hand off documents (bytes never leave the step); returns the next cursor,
                               counters and bounded errors only
      eap.kb_source_checkpoint   persist cursor + counters to `kb_source_syncs` and `kb_sources`, check the
                               cancel event, decide continue / stop
    eap.kb_source_finalize     report event, run completed, `last_successful_sync_at` (complete only)
    eap.kb_source_mark_failed  deterministic failure -> `failed`; unknown authentication -> `needs_attention`

Completed segment children are never repeated on replay (DBOS returns their recorded result); a
segment that crashed replays its step with hash-guarded upserts, so nothing changes twice. A cancel
request stops the run BEFORE the next segment; a started segment finishes its bounded step. Step
names are frozen (in-flight replay).

Security: the secrets of the connection are resolved by `WorkerSecretResolver` inside the segment step
(and the connection check) only; adapters return no values and every error message is scrubbed of the
values the step resolved (`secret_variants`). Events, step outputs, the run row and the sync row hold
counters, keys, codes and messages of at most 200 characters. Crash points (eap_worker.crash):
`kb_source_segment_written` (after the upserts, before the step checkpoint), `kb_source_document_handed_off`
(after a document's hand-off, before its ledger row), `kb_source_before_finalize`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from dbos import DBOS, SetWorkflowID

from eap_kb_sources import (
    ConnectionSnapshot,
    DocumentPolicy,
    DocumentState,
    EntityRef,
    SegmentCounters,
    SourceError,
    SyncSegmentRequest,
    adapter_for,
    document_key,
    error_entry,
)
from eap_kb_sources.adapter import MAX_CURSOR_BYTES, MAX_SEGMENT_ERRORS, MAX_TARGETED_PARTS, MODES, secret_name
from eap_kb_sources.errors import (
    CONNECTION_DISABLED,
    CONNECTION_NOT_FOUND,
    ENQUEUE_FAILED,
    FATAL_CODES,
    INTERNAL_ERROR,
    INVALID_DOCUMENT,
    INVALID_REQUEST,
    KB_CAPACITY,
    NEEDS_ATTENTION_CODES,
    PROVIDER_ERROR,
    RESPONSE_TOO_LARGE,
    RUN_CANCELLED,
    SECRET_MISSING,
    SOURCE_NOT_FOUND,
)
from eap_knowledge import UnsupportedMimeType
from eap_knowledge.formats import PDF_SIGNATURE_WINDOW, format_for_mime, has_pdf_signature
from eap_observability import dbos_trace_attributes, exception_type, log_context, log_failure
from eap_run_store import RunNotFound
from eap_run_store import kb_ingest_handoff as handoff
from eap_run_store.kb_sources import ConnectionStore, EntityStore, RecordStore, SourceStore, SyncStore
from eap_run_store.knowledge import KnowledgeBaseLimitExceeded, KnowledgeStore
from eap_run_store.store import CANCEL_EVENT_KEY
from eap_schemas import KnowledgeBaseNotFound
from eap_schemas.knowledge import kb_ingest_partition_key
from eap_secrets.redaction import redact, secret_variants
from eap_worker.config import KB_INGEST_QUEUE_NAME
from eap_worker.crash import crash_point
from eap_worker.kb_ingest import STEP_RETRY, _transient
from eap_worker.runtime import get_runtime
from eap_worker.secret_refs import secret_store
from eap_worker.workflow import _heartbeat

log = logging.getLogger("eap_worker.kb_source_sync")

WORKFLOW_NAME = "eap.kb_source_sync"
SEGMENT_WORKFLOW_NAME = "eap.kb_source_segment"
START_STEP = "eap.kb_source_start"
SEGMENT_STEP = "eap.kb_source_sync_segment"
CHECKPOINT_STEP = "eap.kb_source_checkpoint"
FINALIZE_STEP = "eap.kb_source_finalize"
MARK_FAILED_STEP = "eap.kb_source_mark_failed"
STEP_NAMES = (START_STEP, SEGMENT_STEP, CHECKPOINT_STEP, FINALIZE_STEP, MARK_FAILED_STEP)
KB_INGEST_WORKFLOW = "eap.kb_ingest"

TARGETED_SEGMENT_PARTS = 20
DEFAULT_LIMITS = {"max_segments": 400, "max_seconds": 6 * 3600, "page_budget": 10, "call_budget": 200}
LIMIT_CAPS = {"max_segments": 2_000, "max_seconds": 24 * 3600, "page_budget": 25, "call_budget": 1_000}
PART_NUMBER = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
DEFAULT_TEXT_BYTES = 5 * 1024 * 1024  # EAP_KB_MAX_DOCUMENT_BYTES, the API's text cap
SYNC_ID_NAMESPACE = uuid.UUID("6f1c2f7e-5e0b-4f7a-9d56-3a2f5b0c9d11")

_EXTENSION_MIME = {
    ".pdf": "application/pdf", ".md": "text/markdown", ".markdown": "text/markdown", ".html": "text/html",
    ".htm": "text/html", ".txt": "text/plain",
}


# --------------------------------------------------------------------------------------- the request


class InvalidRequest(ValueError):
    """The run request is not acceptable: the run ends `failed` with `invalid_request`."""


@dataclass(frozen=True)
class SyncRequest:
    mode: str
    part_numbers: tuple[str, ...]
    max_segments: int
    max_seconds: int
    page_budget: int
    call_budget: int
    restart: bool = False  # full / incremental: drop the stored walk position and start a new walk


def _limit(data: dict[str, Any], name: str) -> int:
    value = data.get(name)
    if value is None:
        return DEFAULT_LIMITS[name]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRequest(f"invalid {name}")
    return min(value, LIMIT_CAPS[name])


def parse_request(request_json: str) -> SyncRequest:
    try:
        data = json.loads(request_json)
    except (TypeError, ValueError):
        raise InvalidRequest("invalid request: not JSON") from None
    if not isinstance(data, dict):
        raise InvalidRequest("invalid request: not an object")
    mode = data.get("mode")
    if mode not in MODES:
        raise InvalidRequest("invalid mode: use targeted, incremental or full")
    parts: list[str] = []
    raw = data.get("part_numbers") or []
    if not isinstance(raw, list):
        raise InvalidRequest("part_numbers must be a list")
    if mode != "targeted" and raw:
        raise InvalidRequest("part_numbers are only for a targeted sync")
    if mode == "targeted":
        if not raw:
            raise InvalidRequest("a targeted sync needs part_numbers")
        if len(raw) > MAX_TARGETED_PARTS:
            raise InvalidRequest(f"at most {MAX_TARGETED_PARTS} part_numbers")
        for item in raw:
            if not isinstance(item, str) or not PART_NUMBER.match(item):
                raise InvalidRequest("invalid part number")
            if item not in parts:
                parts.append(item)
    limits = {name: _limit(data, name) for name in DEFAULT_LIMITS}
    restart = data.get("restart", False)
    if not isinstance(restart, bool):
        raise InvalidRequest("restart must be true or false")
    if restart and mode == "targeted":
        raise InvalidRequest("restart is only for a full or incremental sync")
    return SyncRequest(mode=mode, part_numbers=tuple(parts), restart=restart, **limits)


# --------------------------------------------------------------------------------------- resources


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkerSecretResolver:
    """`SecretResolver` over the tenant's secret store. Remembers the values it resolved so the step
    can scrub them from anything an adapter reports; the values never leave this object."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant = tenant_id
        self._values: list[str] = []
        self._store: Any = None

    def resolve(self, ref: str) -> str:
        try:
            if self._store is None:
                self._store = secret_store(get_runtime().engine)
            value = self._store.resolve(self._tenant, secret_name(ref))
        except Exception:  # noqa: BLE001 - SecretsError (no platform key) or a read failure: no detail
            raise SourceError(SECRET_MISSING, "the connection's secrets cannot be read") from None
        if value is None:
            raise SourceError(SECRET_MISSING, "a secret of the connection is not stored")
        self._values.append(value)
        return value

    def scrub(self, entries: list[dict[str, str]]) -> list[dict[str, str]]:
        if not self._values:
            return entries
        return redact(entries, secret_variants(self._values))


def _snapshot(tenant_id: str, ctx: dict[str, Any]) -> ConnectionSnapshot:
    c = ctx["connection"]
    return ConnectionSnapshot(
        tenant_id=tenant_id, connection_id=c["id"], name=c["name"], provider=ctx["provider"],
        config=c["config"], secret_refs=c["secret_refs"], base_url=c.get("base_url"),
    )


def _stored_cursor(source: Any) -> dict[str, Any]:
    cursor = (source.last_checkpoint or {}).get("cursor")
    return cursor if isinstance(cursor, dict) else {}


# ------------------------------------------------------------------------------------- the facade


def _mime_for(url: str, data: bytes) -> str:
    path = url.split("?", 1)[0].lower()
    for extension, mime in _EXTENSION_MIME.items():
        if path.endswith(extension):
            return mime
    if has_pdf_signature(data[: PDF_SIGNATURE_WINDOW + 4]):
        return "application/pdf"
    return "text/plain"


def _text_cap() -> int:
    try:
        return int(os.environ.get("EAP_KB_MAX_DOCUMENT_BYTES") or DEFAULT_TEXT_BYTES)
    except ValueError:
        return DEFAULT_TEXT_BYTES


def prepare_document(data: bytes, url: str, mime_type: str | None, max_bytes: int) -> tuple[str, str, str | None]:
    """(content, mime type, content_sha256) as the knowledge base stores it: UTF-8 text, or base64 for
    a PDF with the sha256 of its bytes (the existing kb_documents contract). SourceError for anything
    the knowledge base would refuse."""
    mime = mime_type or _mime_for(url, data)
    try:
        spec = format_for_mime(mime)
    except UnsupportedMimeType:
        raise SourceError(INVALID_DOCUMENT, "unsupported document type") from None
    if not data:
        raise SourceError(INVALID_DOCUMENT, "the document is empty")
    if len(data) > max_bytes:
        raise SourceError(RESPONSE_TOO_LARGE, "the document is over the byte cap")
    if spec.mime_type == "application/pdf":
        if not has_pdf_signature(data):
            raise SourceError(INVALID_DOCUMENT, "the bytes are not a PDF")
        return base64.b64encode(data).decode("ascii"), spec.mime_type, hashlib.sha256(data).hexdigest()
    if len(data) > _text_cap():
        raise SourceError(RESPONSE_TOO_LARGE, "the document is over the text cap")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceError(INVALID_DOCUMENT, "the document is not UTF-8 text") from None
    if "\x00" in content or not content.strip():
        raise SourceError(INVALID_DOCUMENT, "the document has no text")
    return content, spec.mime_type, None


class WorkerStoreFacade:
    """The adapter's writes (`SourceStoreFacade`), tenant- and source-scoped by construction.

    Raw records go to `kb_source_records` by hash; entities, identifiers and parameters to the
    structured store; a document to the knowledge base through the shared hand-off
    (`eap_run_store.kb_ingest_handoff`: the API's upload logic) under the deterministic key
    `source:<source_id>:<entity_type>:<external_id>:<relationship>`, with its ledger row
    (`kb_entity_documents`) recording the hash, the provider's validators and the knowledge-base document.
    Same bytes: no new version, no run. Everything is keyed, so a re-executed segment changes nothing."""

    def __init__(self, tenant_id: str, source_id: str, kb_id: str, run_id: str, policy: DocumentPolicy) -> None:
        self.tenant_id, self.source_id, self.kb_id, self.run_id, self.policy = tenant_id, source_id, kb_id, run_id, policy
        rt = get_runtime()
        self._rt = rt
        self._records = RecordStore(rt.engine)
        self._entities = EntityStore(rt.engine)
        self._kb = KnowledgeStore(rt.engine)

    def upsert_record(
        self, external_id: str, record_type: str, payload: dict[str, Any], *, source_updated_at: datetime | None = None
    ) -> Literal["inserted", "updated", "unchanged"]:
        return self._records.upsert(
            self.tenant_id, self.source_id, external_id, record_type, payload,
            source_updated_at=source_updated_at, observed_at=_now(),
        )

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
        entity = self._entities.upsert_entity(
            self.tenant_id, self.source_id, entity_type=entity_type, canonical_name=external_id,
            lifecycle_status=lifecycle_status, attributes=attributes,
        )
        self._entities.set_identifiers(self.tenant_id, entity.id, list(identifiers))
        self._entities.replace_parameters(self.tenant_id, entity.id, list(parameters))
        return EntityRef(entity.id, external_id, entity_type)

    def _key(self, entity: EntityRef, relationship: str, url: str) -> str:
        return document_key(self.source_id, entity.entity_type, entity.external_id, relationship, url)

    def document_state(self, entity: EntityRef, relationship: str, url: str) -> DocumentState | None:
        doc = self._entities.get_document(self.tenant_id, entity.entity_id, self._key(entity, relationship, url))
        if doc is None or doc.status != "queued":
            return None
        return DocumentState(content_hash=doc.content_hash, etag=doc.etag, last_modified=doc.last_modified, status=doc.status)

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
    ) -> Literal["queued", "unchanged"]:
        key = self._key(entity, relationship, url)
        ledger, tenant = self._entities, self.tenant_id
        ledger.upsert_document(
            tenant, entity.entity_id, document_key=key, relationship=relationship, source_url=url,
            source_revision=source_revision,
        )
        try:
            content, mime, digest = prepare_document(data, url, mime_type, self.policy.max_bytes)
        except SourceError as exc:
            ledger.mark_document_failed(tenant, entity.entity_id, key, exc.code)
            raise
        run = self._rt.run_store.get_run(tenant, UUID(self.run_id)) or {}
        metadata = {
            "source": "kb_source", "source_id": self.source_id, "external_id": entity.external_id[:256],
            "relationship": relationship,
        }
        try:
            outcome = handoff.ingest_document(
                self._kb, self._rt.run_store, _launch_kb_ingest, tenant, self.kb_id,
                document_key=key, title=(title or f"{entity.external_id} {relationship}")[:200], content=content,
                mime_type=mime, content_sha256=digest, source_url=url[:2000], metadata=metadata,
                traceparent=run.get("traceparent"),
            )
        except KnowledgeBaseLimitExceeded:
            ledger.mark_document_failed(tenant, entity.entity_id, key, KB_CAPACITY)
            raise SourceError(KB_CAPACITY, "the knowledge base is at its document limit") from None
        except KnowledgeBaseNotFound:
            raise SourceError(SOURCE_NOT_FOUND, "the knowledge base no longer exists") from None
        except handoff.HandOffFailed:
            ledger.mark_document_failed(tenant, entity.entity_id, key, ENQUEUE_FAILED)
            raise SourceError(ENQUEUE_FAILED, "the ingest could not be started") from None
        except handoff.InvalidDocument:
            ledger.mark_document_failed(tenant, entity.entity_id, key, INVALID_DOCUMENT)
            raise SourceError(INVALID_DOCUMENT, "the knowledge base refused the document") from None
        document, queued = outcome.document, not outcome.unchanged
        if outcome.unchanged and document.get("status") == "pending" and document.get("last_run_id"):
            # Stored by a hand-off that died before it created its run row (it recorded the run id on the
            # document in the same transaction): start the run the version is waiting for. Idempotent.
            if self._rt.run_store.get_run(tenant, UUID(str(document["last_run_id"]))) is None:
                row, created = handoff.create_ingest_run(
                    self._rt.run_store, tenant, self.kb_id,
                    handoff.ingest_input(document["id"], document["latest_version"]), document["last_run_id"],
                    traceparent=run.get("traceparent"),
                )
                handoff.hand_off(self._rt.run_store, _launch_kb_ingest, tenant, row, created)
                queued = True
        crash_point("kb_source_document_handed_off", event_key=key)
        ledger.mark_document_queued(
            tenant, entity.entity_id, key, content_hash=document["content_hash"], etag=etag,
            last_modified=last_modified, source_revision=source_revision, kb_document_id=document["id"],
        )
        return "queued" if queued else "unchanged"


# ---------------------------------------------------------------------------------- kb_ingest enqueue

_client_lock = threading.Lock()
_client: Any = None


def _dbos_client() -> Any:
    global _client
    with _client_lock:
        if _client is None:
            from dbos import DBOSClient

            settings = get_runtime().settings
            _client = DBOSClient(
                system_database_url=settings.dbos_system_database_url,
                dbos_system_schema=settings.dbos_system_schema,
                lazy=True,
                retry_connection_errors=False,
            )
        return _client


def _launch_kb_ingest(
    *, run_id: UUID, tenant_id: str, knowledge_base_id: str, input_json: str, traceparent: str | None = None
) -> None:
    """Enqueue `eap.kb_ingest(run_id, tenant_id, kb_id, input_json)` on the partitioned queue, workflow
    id = run id, exactly as the API's launcher does (a repeated enqueue attaches to the existing workflow)."""
    options: dict[str, Any] = {
        "queue_name": KB_INGEST_QUEUE_NAME,
        "workflow_name": KB_INGEST_WORKFLOW,
        "workflow_id": str(run_id),
        "app_version": get_runtime().settings.app_version,
        "queue_partition_key": kb_ingest_partition_key(tenant_id, str(knowledge_base_id)),
    }
    attributes = dbos_trace_attributes(traceparent)
    if attributes is not None:
        options["attributes"] = attributes
    _dbos_client().enqueue(options, str(run_id), tenant_id, str(knowledge_base_id), input_json)


# ------------------------------------------------------------------------------------------- steps


def _sync_store() -> SyncStore:
    return SyncStore(get_runtime().engine)


def _load_context(tenant_id: str, source_id: str) -> dict[str, Any]:
    """The immutable snapshot, or SourceError: the failure code of a source that cannot run."""
    rt = get_runtime()
    source = SourceStore(rt.engine).get(tenant_id, source_id)
    if source is None or not source.enabled:
        raise SourceError(SOURCE_NOT_FOUND, "the source was not found or is disabled")
    connection = ConnectionStore(rt.engine).get(tenant_id, source.connection_id)
    if connection is None:
        raise SourceError(CONNECTION_NOT_FOUND, "the source's connection was not found")
    if not connection.enabled:
        raise SourceError(CONNECTION_DISABLED, "the source's connection is disabled")
    adapter = adapter_for(source.kind, source.provider)  # source_adapter_not_found
    try:
        adapter.validate_config({**dict(connection.config), **dict(source.config)})
    except ValueError as exc:
        raise SourceError(INVALID_REQUEST, f"invalid configuration: {str(exc)[:150]}") from None
    return {
        "kind": source.kind, "provider": source.provider, "kb_id": source.knowledge_base_id,
        "connection": {
            "id": connection.id, "name": connection.name, "config": dict(connection.config),
            "secret_refs": dict(connection.credential_refs), "base_url": connection.base_url,
        },
        "source_config": dict(source.config), "document_policy": dict(source.config.get("document_policy") or {}),
        "cursor": _stored_cursor(source),
    }


@DBOS.step(name=START_STEP, should_retry=_transient, **STEP_RETRY)
async def start_step(run_id: str, tenant_id: str, source_id: str, request_json: str, request: dict[str, Any]) -> dict[str, Any]:
    """The immutable snapshot and limits, then the sync row and `running`. `{"fatal": code, "message"}`
    when the source cannot run (nothing is created), `{"stopped": True}` when the run is already terminal.
    The snapshot is non-secret (config and secret REFS)."""

    def work() -> dict[str, Any]:
        rt = get_runtime()
        try:
            ctx = _load_context(tenant_id, source_id)
        except SourceError as exc:
            return {"fatal": exc.code, "message": exc.message}
        try:
            started = rt.run_store.mark_running(tenant_id, UUID(run_id))
        except RunNotFound:
            return {"fatal": SOURCE_NOT_FOUND, "message": "the run was not found"}
        if started is False:
            run = rt.run_store.get_run(tenant_id, UUID(run_id)) or {}
            return {"stopped": True, "status": run.get("status"), "error": run.get("error")}
        rt.run_store.heartbeat(tenant_id, UUID(run_id))
        store = _sync_store()
        if store.get_by_run(tenant_id, run_id) is None:
            store.create(
                tenant_id, str(uuid.uuid5(SYNC_ID_NAMESPACE, f"{tenant_id}:{run_id}")), source_id, run_id,
                request["mode"], hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
            )
        run = rt.run_store.get_run(tenant_id, UUID(run_id)) or {}
        cancel = rt.event_log.get_event(tenant_id, run_id, CANCEL_EVENT_KEY)
        return {
            "ctx": ctx, "traceparent": run.get("traceparent"), "started_at": _now().isoformat(),
            "cancel": cancel is not None and cancel.type == "cancel_requested",
        }

    return await asyncio.to_thread(work)


@DBOS.step(name=SEGMENT_STEP, should_retry=_transient, **STEP_RETRY)
async def segment_step(
    run_id: str, tenant_id: str, source_id: str, n: int, ctx: dict[str, Any], mode: str, cursor: dict[str, Any],
    part_numbers: list[str], page_budget: int, call_budget: int,
) -> dict[str, Any]:
    """One bounded segment. Returns only `{"next_cursor", "complete", "counters", "errors", "calls_used"}`,
    or `{"fatal": code, "message", "counters", "errors"}` (deterministic, checkpointed, never raised)."""
    counters = SegmentCounters()
    secrets = WorkerSecretResolver(tenant_id)
    try:
        adapter = adapter_for(ctx["kind"], ctx["provider"])
        policy = DocumentPolicy.from_json(ctx["document_policy"])
        facade = await asyncio.to_thread(WorkerStoreFacade, tenant_id, source_id, ctx["kb_id"], run_id, policy)
        request = SyncSegmentRequest(
            source_id=source_id, mode=mode, cursor=cursor, part_numbers=tuple(part_numbers), page_budget=page_budget,
            call_budget=call_budget, config={**ctx["connection"]["config"], **ctx["source_config"]}, document_policy=policy,
        )
        # Adapters are async and may block briefly on the facade's database calls: run the segment as is.
        result = await adapter.sync_segment(request, _snapshot(tenant_id, ctx), secrets, facade)
    except SourceError as exc:
        return {"fatal": exc.code, "message": exc.message, "counters": counters.as_dict(), "errors": []}
    except Exception as exc:  # noqa: BLE001 - anything unexpected from an adapter: provider_error, type only
        return {"fatal": PROVIDER_ERROR, "message": exception_type(exc), "counters": counters.as_dict(), "errors": []}
    crash_point("kb_source_segment_written")
    next_cursor = result.next_cursor
    if next_cursor is not None and len(json.dumps(next_cursor, separators=(",", ":")).encode("utf-8")) > MAX_CURSOR_BYTES:
        return {"fatal": PROVIDER_ERROR, "message": "the adapter cursor is over 8 KiB", "counters": result.counters.as_dict(), "errors": []}
    errors = secrets.scrub([{k: str(v)[:200] for k, v in e.items()} for e in result.errors[:MAX_SEGMENT_ERRORS]])
    return {
        "next_cursor": next_cursor, "complete": bool(result.complete), "counters": result.counters.as_dict(),
        "errors": errors, "calls_used": int(result.calls_used), "sweep_ok": bool(result.sweep_ok),
        "source_revision": (str(result.source_revision)[:200] if result.source_revision else None),
        "budget": result.budget if _small(result.budget) else None,
    }


@DBOS.workflow(name=SEGMENT_WORKFLOW_NAME)
async def segment_workflow(
    run_id: str, tenant_id: str, source_id: str, n: int, ctx: dict[str, Any], mode: str, cursor: dict[str, Any],
    part_numbers: list[str], page_budget: int, call_budget: int,
) -> dict[str, Any]:
    """The child workflow `{run_id}:seg:{n}`: one segment step; returns its bounded result."""
    return await segment_step(run_id, tenant_id, source_id, n, ctx, mode, cursor, part_numbers, page_budget, call_budget)


@DBOS.step(name=CHECKPOINT_STEP, should_retry=_transient, **STEP_RETRY)
async def checkpoint_step(
    run_id: str, tenant_id: str, source_id: str, n: int, cursor: dict[str, Any] | None, counters: dict[str, int],
    errors: list[dict[str, str]], started_at: str, max_segments: int, max_seconds: int, complete: bool,
    idle: bool = False,
) -> dict[str, Any]:
    """Persist the cursor and counters (the sync row and the source), check the cancel event and
    decide: `{"continue": bool, "reason": None|complete|cancelled|budget|max_segments|max_seconds}`.
    `idle`: the segment made no call and found nothing (the provider quota is spent): stop, keep the cursor."""

    def work() -> dict[str, Any]:
        rt = get_runtime()
        _sync_store().update_progress(
            tenant_id, run_id, checkpoint={"cursor": cursor, "segments": n}, counters=counters,
            bounded_errors=errors[:MAX_SEGMENT_ERRORS],
        )
        if cursor is not None:
            SourceStore(rt.engine).set_checkpoint(tenant_id, source_id, {"cursor": cursor}, completed=False)
        rt.event_log.append(
            tenant_id, UUID(run_id), f"sync:segment:{n}", "ingest_progress", {"phase": "sync_segment", "segment": n, **counters}
        )
        cancel = rt.event_log.get_event(tenant_id, run_id, CANCEL_EVENT_KEY)
        now = _now()
        if cancel is not None and cancel.type == "cancel_requested":
            return {"continue": False, "reason": "cancelled"}
        if complete:
            return {"continue": False, "reason": "complete"}
        if idle:
            return {"continue": False, "reason": "budget"}
        if n >= max_segments:
            return {"continue": False, "reason": "max_segments"}
        if (now - datetime.fromisoformat(started_at)).total_seconds() > max_seconds:
            return {"continue": False, "reason": "max_seconds"}
        return {"continue": True, "reason": None}

    return await asyncio.to_thread(work)


def _small(value: Any) -> bool:
    return value is None or len(json.dumps(value, separators=(",", ":")).encode("utf-8")) <= 1024


def _report(state: dict[str, Any], status: str, now: datetime) -> dict[str, Any]:
    c = state["counters"]
    return {
        "source_id": state["source_id"], "mode": state["mode"], "status": status,
        "discovered": c["discovered"], "inserted": c["inserted"], "updated": c["updated"], "unchanged": c["unchanged"],
        "documents_discovered": c["documents_discovered"], "documents_queued": c["documents_queued"], "failed": c["failed"],
        "checkpoint": {"cursor": state["cursor"], "segments": state["segments"]} if state["cursor"] is not None else {},
        "errors": state["errors"][:MAX_SEGMENT_ERRORS], "complete": state["complete"], "stop_reason": state["stop_reason"],
        "calls_used": state["calls_used"], "started_at": state["started_at"], "completed_at": now.isoformat(),
        "removed": state.get("removed", 0), "budget": state.get("budget") or {},
        "source_revision": state.get("source_revision"),
    }


@DBOS.step(name=FINALIZE_STEP, should_retry=_transient, **STEP_RETRY)
async def finalize_step(run_id: str, tenant_id: str, source_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """The report (an `end` event and the run's output), the sync row, and the source's
    `last_successful_sync_at` when the sync is complete. A run that stopped early (budget, max
    segments, seconds) completes with `complete=false` and its cursor."""
    crash_point("kb_source_before_finalize")

    def work() -> dict[str, Any]:
        rt = get_runtime()
        st = dict(state)
        counters = st["counters"]
        if st["complete"] and st["mode"] == "full" and st.get("sweep_ok", True) and st.get("walk_started_at"):
            # A COMPLETE full walk: what it did not observe is gone from the catalogue. The key set is
            # `observed_at >= walk start` (no list anywhere); an empty walk removes nothing.
            try:
                st["removed"] = RecordStore(rt.engine).mark_removed_unobserved_since(
                    tenant_id, source_id, datetime.fromisoformat(st["walk_started_at"])
                )
            except ValueError:
                st["removed"] = 0
        report = _report(st, "completed", _now())
        output = json.dumps(report, sort_keys=True, separators=(",", ":"))
        if st["cursor"] is not None or st["complete"]:
            SourceStore(rt.engine).set_checkpoint(
                tenant_id, source_id, {"cursor": st["cursor"], "segments": st["segments"]},
                last_source_revision=st.get("source_revision") if st["complete"] else None,
                completed=bool(st["complete"]),
            )
        _sync_store().finish(
            tenant_id, run_id, "completed", counters=counters, bounded_errors=st["errors"][:MAX_SEGMENT_ERRORS],
            completed_at=_now(),
        )
        try:
            rt.run_store.finish_with_end(tenant_id, UUID(run_id), "completed", output, None, end_data={"report": report})
        except RunNotFound:
            log.warning("kb_source_sync run %s: no run row to finish for tenant %s", run_id, tenant_id)
        return report

    return await asyncio.to_thread(work)


@DBOS.step(name=MARK_FAILED_STEP, should_retry=_transient, **STEP_RETRY)
async def mark_failed_step(
    run_id: str, tenant_id: str, source_id: str, code: str, message: str, state: dict[str, Any]
) -> dict[str, Any]:
    """A deterministic failure ends the run `failed`; unknown authentication (`connection_auth_failed`,
    `secret_missing`) ends it `needs_attention`; a cancel ends it `failed` with `run_cancelled`. The
    cursor of the last finished segment stays on the source."""

    def work() -> dict[str, Any]:
        rt = get_runtime()
        status = "needs_attention" if code in NEEDS_ATTENTION_CODES else "failed"
        report = _report(state, status, _now())
        report["error_code"] = code
        output = json.dumps(report, sort_keys=True, separators=(",", ":"))
        try:
            _sync_store().finish(
                tenant_id, run_id, status, counters=state["counters"],
                bounded_errors=state["errors"][:MAX_SEGMENT_ERRORS], completed_at=_now(),
            )
        except Exception as exc:  # noqa: BLE001 - no sync row (the run never started): the run still ends
            log_failure(log, "sync row not finished", exc, code="sync_row", run_id=run_id, tenant_id=tenant_id)
        try:
            rt.run_store.finish_with_end(
                tenant_id, UUID(run_id), status, output, message, end_data={"report": report, "error_code": code}
            )
        except RunNotFound:
            log.warning("kb_source_sync run %s: no run row to fail for tenant %s", run_id, tenant_id)
        return report

    return await asyncio.to_thread(work)


# ------------------------------------------------------------------------------------- the workflow


def _empty_state(source_id: str, mode: str | None, started_at: str) -> dict[str, Any]:
    return {
        "source_id": source_id, "mode": mode, "counters": SegmentCounters().as_dict(), "errors": [], "cursor": None,
        "segments": 0, "complete": False, "stop_reason": None, "calls_used": 0, "started_at": started_at,
        "walk_started_at": None, "sweep_ok": True, "removed": 0, "budget": None, "source_revision": None,
    }


@DBOS.workflow(name=WORKFLOW_NAME)
async def kb_source_sync(run_id: str, tenant_id: str, source_id: str, request_json: str) -> dict[str, Any]:
    with log_context(tenant_id=tenant_id, run_id=run_id):
        state = _empty_state(source_id, None, "")
        try:
            request = parse_request(request_json)
        except InvalidRequest as exc:
            return await _fail(run_id, tenant_id, source_id, INVALID_REQUEST, str(exc), state)
        state["mode"] = request.mode
        try:
            begin = await start_step(
                run_id, tenant_id, source_id, request_json,
                {"mode": request.mode},
            )
        except Exception as exc:  # noqa: BLE001
            log_failure(log, "kb_source_sync could not start", exc, code=INTERNAL_ERROR, run_id=run_id, tenant_id=tenant_id)
            return await _fail(run_id, tenant_id, source_id, INTERNAL_ERROR, "the sync could not start", state)
        if begin.get("stopped"):
            return {"status": begin.get("status") or "failed", "error": begin.get("error"), "error_code": "run_ended"}
        state["started_at"] = begin.get("started_at") or _now().isoformat()
        if begin.get("fatal"):
            return await _fail(run_id, tenant_id, source_id, begin["fatal"], begin.get("message", ""), state)
        heartbeat = asyncio.create_task(_heartbeat(run_id, tenant_id))
        try:
            return await _drive(run_id, tenant_id, source_id, request, begin, state)
        except Exception as exc:  # noqa: BLE001 - the run always ends with a recorded outcome
            log_failure(log, "kb_source_sync failed", exc, code=INTERNAL_ERROR, run_id=run_id, tenant_id=tenant_id)
            return await _fail(run_id, tenant_id, source_id, INTERNAL_ERROR, "the sync failed with an internal error", state)
        finally:
            heartbeat.cancel()


async def _fail(run_id: str, tenant_id: str, source_id: str, code: str, message: str, state: dict[str, Any]) -> dict[str, Any]:
    try:
        report = await mark_failed_step(run_id, tenant_id, source_id, code, message, state)
    except Exception:  # noqa: BLE001 - the answer below is the outcome either way
        report = {}
    status = "needs_attention" if code in NEEDS_ATTENTION_CODES else "failed"
    return {"status": status, "error_code": code, "error": message, "report": report}


async def _drive(
    run_id: str, tenant_id: str, source_id: str, request: SyncRequest, begin: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    ctx = begin["ctx"]
    targeted = request.mode == "targeted"
    stored: dict[str, Any] = dict(ctx["cursor"])
    # A targeted sync never moves the walk's cursor; only the daily quota counts (inside it) are carried.
    cursor: dict[str, Any] = {k: v for k, v in stored.items() if k == "daily_budget"} if targeted else dict(stored)
    state["cursor"] = stored or None
    walk_started = None
    if not targeted:
        # The walk position lives in the adapter's cursor; the worker adds two reserved keys while a walk is
        # unfinished: `walk_started_at` (the removal sweep's boundary) and `walk_mode`. Resume the stored
        # walk of THIS mode; `restart` or a walk of the other mode starts a new one (the quota counts stay).
        budget_only = {k: v for k, v in stored.items() if k == "daily_budget"}
        if request.restart or (stored.get("walk_mode") not in (None, request.mode)):
            cursor, walk_started = budget_only, begin.get("started_at")
        elif stored.get("walk_mode") == request.mode and stored.get("walk_started_at"):
            walk_started = stored["walk_started_at"]
        else:
            walk_started = begin.get("started_at")
        state["walk_started_at"] = walk_started
    parts = list(request.part_numbers)
    n = 0
    stop: str | None = "cancelled" if begin.get("cancel") else None
    while stop is None:
        n += 1
        slice_ = parts[(n - 1) * TARGETED_SEGMENT_PARTS : n * TARGETED_SEGMENT_PARTS] if targeted else []
        with SetWorkflowID(f"{run_id}:seg:{n}"):
            handle = await DBOS.start_workflow_async(
                segment_workflow, run_id, tenant_id, source_id, n, ctx, request.mode, cursor, slice_,
                request.page_budget, request.call_budget,
            )
        out = await handle.get_result()
        for name, value in out["counters"].items():
            state["counters"][name] += value
        room = MAX_SEGMENT_ERRORS - len(state["errors"])
        if room > 0:
            state["errors"].extend(out["errors"][:room])
        state["segments"] = n
        if out.get("fatal"):
            return await _fail(run_id, tenant_id, source_id, out["fatal"], out.get("message", ""), state)
        state["calls_used"] += out["calls_used"]
        complete = (n * TARGETED_SEGMENT_PARTS >= len(parts)) if targeted else bool(out["complete"])
        if out["next_cursor"] is not None:
            if targeted:
                if "daily_budget" in out["next_cursor"]:
                    stored = {**stored, "daily_budget": out["next_cursor"]["daily_budget"]}
                    cursor = {"daily_budget": out["next_cursor"]["daily_budget"]}
            else:
                cursor = out["next_cursor"]
                if not complete:
                    cursor = {**cursor, "walk_started_at": walk_started, "walk_mode": request.mode}
                stored = cursor
            state["cursor"] = stored
        state["complete"] = complete
        state["sweep_ok"] = bool(out.get("sweep_ok", True))
        state["budget"] = out.get("budget") or state["budget"]
        if complete and not targeted:
            state["source_revision"] = out.get("source_revision")
        decision = await checkpoint_step(
            run_id, tenant_id, source_id, n, state["cursor"], state["counters"], state["errors"],
            state["started_at"], request.max_segments, request.max_seconds, complete,
            not complete and out["calls_used"] == 0 and out["counters"]["discovered"] == 0 and out["counters"]["failed"] == 0,
        )
        if not decision["continue"]:
            stop = decision["reason"]
    state["stop_reason"] = None if stop == "complete" else stop
    state["complete"] = stop == "complete"
    if stop == "cancelled":
        return await _fail(run_id, tenant_id, source_id, RUN_CANCELLED, "the sync was cancelled", state)
    report = await finalize_step(run_id, tenant_id, source_id, state)
    return {"status": "completed", "error_code": None, "error": None, "report": report}
