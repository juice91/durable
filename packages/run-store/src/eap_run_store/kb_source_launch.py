"""Starting a knowledge-base source sync: the one function the API route and the `kb.source.sync` tool share.

`launch_sync` validates the request, records the run (kind `kb_source_sync`) and its `kb_source_syncs`
row, then enqueues the DBOS workflow `eap.kb_source_sync(run_id, tenant_id, source_id, request_json)`
with workflow id = run id. Insert-before-enqueue, like the kb_ingest hand-off: a crash between the
insert and the enqueue is recovered by repeating the call with the same `run_id`, which enqueues again
(DBOS returns the existing workflow of that id), a failed enqueue leaves the run failed
(`ENQUEUE_FAILED_ERROR`) and a retry re-queues it, and a finished run is answered as recorded.

The launcher is anything with `launch_kb_source_sync(*, run_id, tenant_id, source_id, request_json,
traceparent=None)`: the API's `RunLauncher`, or the worker's own DBOS client. Nothing here knows HTTP or DBOS.
Errors are `SourceLaunchError` (a stable `code` and an HTTP-like `status`, never a provider or database text)
and `LaunchFailed` (the enqueue failed: the route answers 503).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any, Protocol

from sqlalchemy.engine import Engine

from eap_run_store.kb_sources import SourceStore, SyncStore
from eap_run_store.store import TERMINAL_RUN_STATUSES, RunIdConflict, RunStore

log = logging.getLogger("eap_run_store.kb_source_launch")

MODES = ("targeted", "incremental", "full")
MAX_PART_NUMBERS = 100
PART_NUMBER = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
ENQUEUE_FAILED_DETAIL = "Run recorded but could not be started; retry the same request (same run_id)"


class SourceLaunchError(Exception):
    """A request the launch refuses: `code` is stable, `status` the HTTP status a route answers with."""

    def __init__(self, code: str, status: int, message: str = "") -> None:
        self.code, self.status, self.message = code, status, message or code
        super().__init__(code, status, self.message)


class LaunchFailed(Exception):
    """The enqueue failed after the run was recorded (the route answers 503 with the run id)."""

    def __init__(self, run_id: str, detail: str = ENQUEUE_FAILED_DETAIL) -> None:
        self.run_id, self.detail = run_id, detail
        super().__init__(run_id, detail)


class SyncLauncher(Protocol):
    def launch_kb_source_sync(
        self, *, run_id: uuid.UUID, tenant_id: str, source_id: str, request_json: str,
        traceparent: str | None = None,
    ) -> None: ...


def tool_run_id(tenant_id: str, source_id: str, parent_run_id: str | None, tool_call_id: str) -> uuid.UUID:
    """The child run id of the tool path: one per (tenant, source, calling run, tool call)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"kb-source-sync:{tenant_id}:{source_id}:{parent_run_id}:{tool_call_id}")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def validate_request(mode: str, part_numbers: list[str] | tuple[str, ...] | None) -> dict[str, Any]:
    """The canonical request `{mode, part_numbers}`; SourceLaunchError 422 for anything the worker would refuse."""
    if mode not in MODES:
        raise SourceLaunchError("invalid_request", 422, "mode must be targeted, incremental or full")
    parts = list(part_numbers or [])
    if mode != "targeted":
        if parts:
            raise SourceLaunchError("part_numbers_not_allowed", 422, "part_numbers are only for a targeted sync")
        return {"mode": mode, "part_numbers": []}
    if not parts:
        raise SourceLaunchError("invalid_request", 422, "a targeted sync needs part_numbers")
    if len(parts) > MAX_PART_NUMBERS:
        raise SourceLaunchError("invalid_request", 422, f"at most {MAX_PART_NUMBERS} part_numbers")
    kept: list[str] = []
    for item in parts:
        if not isinstance(item, str) or not PART_NUMBER.match(item):
            raise SourceLaunchError("invalid_request", 422, "invalid part number")
        if item not in kept:
            kept.append(item)
    return {"mode": mode, "part_numbers": kept}


def request_hash(source_id: str, request: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({"source_id": str(source_id), **request}).encode("utf-8")).hexdigest()


def launch_sync(
    engine: Engine,
    launcher: SyncLauncher,
    *,
    tenant_id: str,
    source_id: str,
    mode: str,
    part_numbers: list[str] | tuple[str, ...] | None,
    run_id: uuid.UUID | str,
    parent_run_id: str | None = None,
    created_by: str | None = None,
    traceparent: str | None = None,
    trace_id: str | None = None,
) -> dict[str, str]:
    """Validate, record and enqueue a sync; `{run_id, status: "queued"}` (the recorded status for a replay of a
    finished run). `created_by` is for the log only. SourceLaunchError (source_not_found 404, source_disabled 409,
    part_numbers_not_allowed / invalid_request 422, run_id_conflict 409), LaunchFailed."""
    run_uuid = uuid.UUID(str(run_id))
    source = SourceStore(engine).get(tenant_id, str(source_id))
    if source is None:
        raise SourceLaunchError("source_not_found", 404, "source not found")
    if not source.enabled:
        raise SourceLaunchError("source_disabled", 409, "the source is disabled")
    request = validate_request(mode, part_numbers)
    digest = request_hash(source.id, request)
    runs, syncs = RunStore(engine), SyncStore(engine)
    try:
        _, created = runs.create_kb_source_sync_run(
            tenant_id, run_uuid, source.id, request, trace_id=trace_id, traceparent=traceparent
        )
    except RunIdConflict:
        raise SourceLaunchError("run_id_conflict", 409, "run_id is already in use") from None
    run = runs.get_run(tenant_id, run_uuid)
    if run is None:  # pragma: no cover - deleted between insert and read
        raise SourceLaunchError("run_id_conflict", 409, "run_id is already in use")
    if not created:
        existing = syncs.get_by_run(tenant_id, str(run_uuid))
        same = (
            run.get("kind") == "kb_source_sync"
            and (existing is None or (existing.request_hash == digest and existing.source_id == source.id))
            and str(run.get("knowledge_base_id")) == str(source.knowledge_base_id)
        )
        if not same:
            raise SourceLaunchError("run_id_conflict", 409, "run_id is already used by a different request")
    try:
        syncs.create(
            tenant_id, str(uuid.uuid5(uuid.NAMESPACE_URL, f"kb-source-sync-row:{run_uuid}")), source.id,
            str(run_uuid), request["mode"], digest, parent_run_id=parent_run_id,
        )
    except ValueError:
        raise SourceLaunchError("run_id_conflict", 409, "run_id is already in use") from None
    failed_handoff = run["status"] == "failed" and run.get("error") is not None and _is_enqueue_failed(run["error"])
    if not created and run["status"] in TERMINAL_RUN_STATUSES and not failed_handoff:
        return {"run_id": str(run_uuid), "status": run["status"]}
    owns = created or runs.requeue_after_enqueue_failure(tenant_id, run_uuid)
    try:
        launcher.launch_kb_source_sync(
            run_id=run_uuid, tenant_id=tenant_id, source_id=source.id, request_json=_canonical(request),
            traceparent=run.get("traceparent"),
        )
    except Exception as exc:  # noqa: BLE001
        # Type only: the error can quote the DBOS database URL.
        log.error("kb source sync enqueue failed (%s): run=%s tenant=%s", type(exc).__name__, run_uuid, tenant_id)
        if owns:
            try:
                runs.mark_enqueue_failed(tenant_id, run_uuid)
            except Exception as mark_exc:  # noqa: BLE001
                log.error("marking enqueue failure failed (%s): run=%s", type(mark_exc).__name__, run_uuid)
        raise LaunchFailed(str(run_uuid)) from None
    log.info("kb source sync %s queued: tenant=%s source=%s mode=%s by=%s", run_uuid, tenant_id, source.id,
             request["mode"], created_by)
    return {"run_id": str(run_uuid), "status": "queued"}


def _is_enqueue_failed(error: str) -> bool:
    from eap_run_store.store import ENQUEUE_FAILED_ERROR

    return error == ENQUEUE_FAILED_ERROR


# ------------------------------------------------------------------------------------------ views


def checkpoint_summary(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """What a caller may see of a source's checkpoint: how many segments it has walked and whether a cursor is
    stored. Never the cursor itself."""
    checkpoint = checkpoint or {}
    segments = checkpoint.get("segments")
    return {
        "segments": segments if isinstance(segments, int) and not isinstance(segments, bool) else 0,
        "has_cursor": isinstance(checkpoint.get("cursor"), dict) and bool(checkpoint.get("cursor")),
    }


def source_view(source: Any) -> dict[str, Any]:
    """A `kb_sources` row as the API and `kb.source.get` return it (non-secret config only)."""
    synced = source.last_successful_sync_at
    return {
        "id": source.id,
        "knowledge_base_id": source.knowledge_base_id,
        "connection_id": source.connection_id,
        "kind": source.kind,
        "provider": source.provider,
        "name": source.name,
        "enabled": source.enabled,
        "config": dict(source.config),
        "last_successful_sync_at": synced.isoformat() if synced is not None else None,
        "last_checkpoint_summary": checkpoint_summary(source.last_checkpoint),
        "last_source_revision": source.last_source_revision,
        "created_at": source.created_at.isoformat(),
        "updated_at": source.updated_at.isoformat(),
    }
