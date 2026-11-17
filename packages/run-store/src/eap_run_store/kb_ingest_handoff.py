"""The shared knowledge-base ingest hand-off: write a document version, create its `kb_ingest` run
and enqueue it, with the claim and orphan recovery rules the upload route has always had.

Two entry points use this one implementation: the API's `PUT /knowledge-bases/{kb}/documents/{key}`
(HTTP in, a `RunLauncher` out) and the worker's knowledge-base source sync (`eap.kb_source_sync`,
a DBOS client out). Nothing here knows HTTP, FastAPI, DBOS or OpenTelemetry: the caller passes
`launch_kb_ingest` (a callable with `RunLauncher.launch_kb_ingest`'s keyword arguments) and the
trace ids, and maps the exceptions below to its own surface (the route answers 404, 409, 422, 503).

`ingest_document` is the core of the upload: nothing is written for a replay of an earlier request
(same `run_id`), an unchanged document is a no-op, a new or changed one is stored with its run id
recorded in the same transaction, claimed, recorded as a `kb_ingest` run and enqueued. A retry
re-enqueues a run whose hand-off failed or died (DBOS returns the existing workflow of that id).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from eap_run_store.knowledge import KnowledgeBaseLimitExceeded, KnowledgeStore, content_hash
from eap_run_store.store import ENQUEUE_FAILED_ERROR, TERMINAL_RUN_STATUSES, RunIdConflict, RunStore
from eap_schemas import KnowledgeBaseNotFound

log = logging.getLogger("eap_run_store.kb_ingest_handoff")

# A document left 'pending' whose recorded run does not exist is orphaned (its request crashed, or
# its run_id clashed after the write) only after this long: a younger one belongs to a request that
# is still between its document write and its run insert.
ORPHAN_AFTER_SECONDS = 60
ENQUEUE_FAILED_DETAIL = "Run recorded but could not be started; retry the same request (same run_id)"
RUN_ID_IN_USE = "run_id is already in use"
RUN_ID_DIFFERENT_REQUEST = "run_id is already used by a different run request"

# P1.C5: new kb_ingest runs embed with the split steps (eap.kb_embed_call, then eap.kb_embed_write),
# so a database retry of the write never calls the provider again. Runs enqueued before (no flag)
# keep eap.kb_embed_batch, so in-flight workflows recover unchanged.
EMBED_SPLIT_KEY = "embed_split"

LaunchKbIngest = Callable[..., None]


class HandOffFailed(Exception):
    """The enqueue of a recorded run failed: the route answers 503 {"detail", "run_id"}."""

    def __init__(self, run_id: str, detail: str = ENQUEUE_FAILED_DETAIL) -> None:
        self.run_id = run_id
        self.detail = detail
        super().__init__(run_id, detail)


class RunIdInUse(Exception):
    """`run_id` belongs to another tenant, another document or another kind of run (409)."""

    def __init__(self, detail: str = RUN_ID_IN_USE) -> None:
        self.detail = detail
        super().__init__(detail)


class InvalidDocument(ValueError):
    """The store refused the document (metadata its validator rejects): the route answers 422."""


# ------------------------------------------------------------------------------------ run inputs


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ingest_input(document_id: str, doc_version: int) -> str:
    """The `input` of a document ingest run (and the enqueued `input_json`)."""
    return canonical_json({"document_id": str(document_id), "doc_version": int(doc_version), EMBED_SPLIT_KEY: True})


def reindex_input(generation: int) -> str:
    """The `input` of a reindex run (and the enqueued `input_json`)."""
    return canonical_json({"reindex": int(generation), EMBED_SPLIT_KEY: True})


def same_ingest_input(stored: Any, input_json: str) -> bool:
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


def is_run_of(run: dict[str, Any] | None, kb_id: str, input_json: str) -> bool:
    return (
        run is not None
        and run.get("kind") == "kb_ingest"
        and str(run.get("knowledge_base_id")) == str(kb_id)
        and same_ingest_input(run.get("input"), input_json)
    )


def run_input(run: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(run.get("input") or "")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_document_run(run: dict[str, Any], kb_id: str, document_id: str) -> bool:
    """A kb_ingest run of any version of this document."""
    return (
        run.get("kind") == "kb_ingest"
        and str(run.get("knowledge_base_id")) == str(kb_id)
        and str(run_input(run).get("document_id")) == str(document_id)
    )


def enqueue_failed(run: dict[str, Any] | None) -> bool:
    return run is not None and run["status"] == "failed" and run["error"] == ENQUEUE_FAILED_ERROR


def run_view(run: dict[str, Any]) -> dict[str, Any]:
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


# --------------------------------------------------------------------------- claims and orphans


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def stale_queued(run: dict[str, Any]) -> bool:
    """Still 'queued' ORPHAN_AFTER_SECONDS after it was created: possibly never enqueued (its request
    died between the run insert and the enqueue). Handing it off again is idempotent."""
    created = _aware(run.get("created_at"))
    if run.get("status") != "queued" or created is None:
        return False
    return (datetime.now(timezone.utc) - created).total_seconds() > ORPHAN_AFTER_SECONDS


def orphaned(document: dict[str, Any]) -> bool:
    """'pending' for more than ORPHAN_AFTER_SECONDS (call only when its recorded run is missing)."""
    updated = _aware(document.get("updated_at"))
    if document.get("status") != "pending" or updated is None:
        return False
    return (datetime.now(timezone.utc) - updated).total_seconds() > ORPHAN_AFTER_SECONDS


def renew_claim(store: KnowledgeStore, tenant_id: str, document: dict[str, Any], run_id: uuid.UUID | str) -> bool:
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


def resolve_client_run_id(
    store: KnowledgeStore, run_store: RunStore, tenant_id: str, run_id: uuid.UUID | None
) -> dict[str, Any] | None:
    """Resolve a client run_id BEFORE anything is written: None when unused, the tenant's run when
    it exists, RunIdInUse when another tenant holds it."""
    if run_id is None:
        return None
    with store.engine.connect() as conn:
        owner = conn.execute(text("SELECT tenant_id FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar()
    if owner is None:
        return None
    if owner != tenant_id:
        raise RunIdInUse()
    return run_store.get_run(tenant_id, run_id)


def latest_run(run_store: RunStore, tenant_id: str, document: dict[str, Any]) -> dict[str, Any] | None:
    """The document's recorded ingest run when it exists and ingests the latest version, else None."""
    last_run_id = document.get("last_run_id")
    if not last_run_id:
        return None
    try:
        run = run_store.get_run(tenant_id, uuid.UUID(str(last_run_id)))
    except ValueError:
        return None
    if not is_run_of(run, document["kb_id"], ingest_input(document["id"], document["latest_version"])):
        return None
    return run


# ----------------------------------------------------------------------------- run and hand-off


def create_ingest_run(
    run_store: RunStore,
    tenant_id: str,
    kb_id: str,
    input_json: str,
    run_id: uuid.UUID | str,
    *,
    trace_id: str | None = None,
    traceparent: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create (or find, for a retry with the same run_id) the kb_ingest run. RunIdInUse when
    `run_id` is another tenant's or another request's run (callers resolve run_id before writing;
    this is the last guard against a race)."""
    run_uuid = uuid.UUID(str(run_id))
    try:
        _, created = run_store.create_kb_ingest_run(
            tenant_id, kb_id, input_json, run_uuid, trace_id=trace_id, traceparent=traceparent
        )
    except RunIdConflict:
        raise RunIdInUse() from None
    run = run_store.get_run(tenant_id, run_uuid)
    if run is None:  # pragma: no cover - deleted between insert and read
        raise RunIdInUse("Run not found")
    if not created and not is_run_of(run, kb_id, input_json):
        raise RunIdInUse(RUN_ID_DIFFERENT_REQUEST)
    return run, created


def hand_off(
    run_store: RunStore,
    launch_kb_ingest: LaunchKbIngest,
    tenant_id: str,
    run: dict[str, Any],
    created: bool,
    failed_detail: str = ENQUEUE_FAILED_DETAIL,
) -> dict[str, Any]:
    """Enqueue `eap.kb_ingest` for the stored run, exactly as tool runs do: this caller owns the
    hand-off of a row it created, or of one an earlier failed hand-off left failed (re-queued now).
    A failed enqueue marks an owned run failed (ENQUEUE_FAILED_ERROR) and raises HandOffFailed."""
    run_id = uuid.UUID(str(run["id"]))
    owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)
    try:
        launch_kb_ingest(
            run_id=run_id,
            tenant_id=tenant_id,
            knowledge_base_id=str(run["knowledge_base_id"]),
            input_json=run["input"],
            traceparent=run.get("traceparent"),
        )
    except Exception as exc:
        # Type only: the error can quote the DBOS database URL or the enqueued payload.
        log.error("enqueue failed (%s): run=%s tenant=%s", type(exc).__name__, run_id, tenant_id)
        if owns_handoff:
            try:
                run_store.mark_enqueue_failed(tenant_id, run_id)
            except Exception as mark_exc:  # noqa: BLE001 - the failure below is the answer either way
                log.error("marking enqueue failure failed (%s): run=%s", type(mark_exc).__name__, run_id)
        raise HandOffFailed(str(run_id), failed_detail) from None
    run = run_store.get_run(tenant_id, run_id) or run
    view = run_view(run)
    if run["error"] == ENQUEUE_FAILED_ERROR:
        # A concurrent owner's enqueue failed and marked it, but this enqueue succeeded.
        view["status"] = view["display_status"] = "queued"
    log.info("kb_ingest run %s queued: tenant=%s kb=%s", run_id, tenant_id, run["knowledge_base_id"])
    return view


def replay(
    run_store: RunStore, launch_kb_ingest: LaunchKbIngest, tenant_id: str, run: dict[str, Any]
) -> dict[str, Any]:
    """A retry of the request that created `run`: hand it off again unless it has finished. The
    enqueue is idempotent (DBOS returns the existing workflow of this workflow id), and it recovers
    a run whose creating request died before its enqueue. A finished run is answered as recorded."""
    if enqueue_failed(run) or run["status"] not in TERMINAL_RUN_STATUSES:
        return hand_off(run_store, launch_kb_ingest, tenant_id, run, created=False)
    return run_view(run)


# ----------------------------------------------------------------------------------- the upload


@dataclass(frozen=True)
class IngestOutcome:
    """What `ingest_document` did. `run` holds the response keys that name the run: the run view
    for a started or replayed ingest, `{"run_id": None}` for an unchanged document."""

    unchanged: bool
    created: bool
    document: dict[str, Any]
    run: dict[str, Any]


def ingest_document(
    store: KnowledgeStore,
    run_store: RunStore,
    launch_kb_ingest: LaunchKbIngest,
    tenant_id: str,
    kb_id: str,
    *,
    document_key: str,
    title: str,
    content: str,
    mime_type: str,
    content_sha256: str | None = None,
    source_url: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    run_id: uuid.UUID | None = None,
    trace_id: str | None = None,
    traceparent: str | None = None,
) -> IngestOutcome:
    """Store one document version and hand its ingest run off (see the module docstring).

    The caller has validated and extracted the content (the API's `prepare_upload`, the sync's
    own bounds). `content_sha256` (lower-case hex of the document bytes) is passed for a binary
    document stored as base64, whose hash is of the decoded bytes; None hashes the text. Raises
    KnowledgeBaseNotFound, KnowledgeBaseLimitExceeded (2,000 documents), InvalidDocument,
    RunIdInUse and HandOffFailed."""
    existing = resolve_client_run_id(store, run_store, tenant_id, run_id)
    if existing is not None:
        document = store.get_document(tenant_id, kb_id, document_key)
        if (
            document is None
            or not is_document_run(existing, kb_id, document["id"])
            or run_input(existing).get("doc_version") != document["latest_version"]
            or document["content_hash"] != (content_sha256 or content_hash(content))
        ):
            # Not a retry of the request that stored the current version: refuse, write nothing.
            raise RunIdInUse(RUN_ID_DIFFERENT_REQUEST)
        view = replay(run_store, launch_kb_ingest, tenant_id, existing)
        return IngestOutcome(
            False, run_input(existing).get("doc_version") == 1, document, view
        )

    new_run_id = run_id or uuid.uuid4()
    try:
        result = store.put_document(
            tenant_id,
            kb_id,
            document_key=document_key,
            title=title,
            content=content,
            mime_type=mime_type,
            source_url=source_url,
            metadata=metadata,
            run_id=str(new_run_id),  # last_run_id, in the same transaction as the new version
            content_sha256=content_sha256,
        )
    except (KnowledgeBaseNotFound, KnowledgeBaseLimitExceeded):
        raise
    except ValueError as exc:  # metadata the store's validator refuses
        raise InvalidDocument(str(exc)) from exc
    document = result.document

    def start(run_uuid: uuid.UUID | str) -> dict[str, Any]:
        input_json = ingest_input(document["id"], document["latest_version"])
        run, created = create_ingest_run(
            run_store, tenant_id, kb_id, input_json, run_uuid, trace_id=trace_id, traceparent=traceparent
        )
        return hand_off(run_store, launch_kb_ingest, tenant_id, run, created)

    if not result.unchanged:
        if not renew_claim(store, tenant_id, document, new_run_id):
            # Stalled past ORPHAN_AFTER_SECONDS and taken over: the version is stored and its
            # ingest belongs to the request that took over.
            current = store.get_document(tenant_id, kb_id, document_key) or document
            return IngestOutcome(False, result.created, current, {"run_id": current["last_run_id"]})
        view = start(new_run_id)
        return IngestOutcome(False, result.created, document, view)

    latest = latest_run(run_store, tenant_id, document)
    if latest is not None and (enqueue_failed(latest) or stale_queued(latest)):
        # A retry of an upload whose hand-off failed, or whose request died before enqueuing:
        # hand the recorded run off again (idempotent in DBOS).
        view = hand_off(run_store, launch_kb_ingest, tenant_id, latest, created=False)
    elif latest is None and orphaned(document) and store.attach_ingest_run(
        tenant_id,
        document["id"],
        str(new_run_id),
        doc_version=document["latest_version"],
        expected_last_run_id=document["last_run_id"],
        expected_updated_at=document["updated_at"],  # a stalled owner's lease renewal makes this lose
    ):
        # Stored long ago by a request that never created its run: this upload won the
        # compare-and-set on last_run_id and starts the run the version is waiting for.
        view = start(new_run_id)
    else:
        return IngestOutcome(True, False, document, {"run_id": None})
    document = store.get_document(tenant_id, kb_id, document_key) or document
    return IngestOutcome(False, document["latest_version"] == 1, document, view)
