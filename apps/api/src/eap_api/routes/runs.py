"""Run routes: start a run, list runs, read one, read its events, stream it (SSE with replay).

Runs and events live in Postgres (eap_run_store, JSONB + row locks); on any other database these
routes answer 503. Every lookup is tenant-scoped: another tenant's or an unknown id is a 404.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from eap_observability import current_trace_context, log_failure

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel, AgentVersionModel
from eap_api.schemas import ErrorDetail, RunCreate
from eap_api.sse import (
    DeltaSource,
    StreamLimiter,
    StreamSettings,
    event_json,
    get_delta_source,
    get_stream_limiter,
    get_stream_settings,
    release_stream_slot,
    run_event_stream,
)
from eap_run_store import EventLog, InvalidRunCursor, RunCursor, RunIdConflict, RunStore
from eap_run_store.cursor import MAX_CURSOR_LENGTH

log = logging.getLogger("eap_api")

SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_run_storage(request: Request) -> tuple[RunStore, EventLog]:
    return request.app.state.run_stores


StoresDep = Annotated[tuple[RunStore, EventLog], Depends(require_run_storage)]

router = APIRouter(tags=["runs"], responses={404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}})


def _run_json(run: dict[str, Any]) -> dict[str, Any]:
    """The API's run shape: `run_id`/`version` match the POST response; timestamps as ISO strings."""
    iso = lambda v: v.isoformat() if v is not None else None  # noqa: E731
    return {
        "run_id": str(run["id"]),
        "agent_id": str(run["agent_id"]),
        "version": run["agent_version"],
        "status": run["status"],
        "display_status": run["display_status"],
        "input": run["input"],
        "output": run["output"],
        "error": run["error"],
        "heartbeat_at": iso(run["heartbeat_at"]),
        "created_at": iso(run["created_at"]),
        "updated_at": iso(run["updated_at"]),
        "conversation_id": run.get("conversation_id"),
        "turn": run.get("turn"),
        "trace_id": run.get("trace_id"),
    }


def _get_run_or_404(run_store: RunStore, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
    run = run_store.get_run(tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


def _version_row(db: Session, agent_id: uuid.UUID, version: int) -> AgentVersionModel | None:
    return (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == str(agent_id), AgentVersionModel.version == version)
        .first()
    )


def resolve_version(db: Session, agent_id: uuid.UUID | str, requested: int | None) -> int:
    """The requested published version, or the latest one when `requested` is None (409 if none, 404 if unknown)."""
    if requested is None:
        latest = db.query(func.max(AgentVersionModel.version)).filter(AgentVersionModel.agent_id == str(agent_id)).scalar()
        if latest is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent has no published version")
        return latest
    if _version_row(db, agent_id, requested) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return requested


def check_payload_size(db: Session, agent_id: uuid.UUID | str, version: int, input_text: str) -> None:
    version_row = _version_row(db, agent_id, version)
    assert version_row is not None
    payload_bytes = len(json.dumps(version_row.spec).encode("utf-8")) + len(input_text.encode("utf-8"))
    max_payload_bytes = int(os.environ.get("EAP_MAX_RUN_PAYLOAD_BYTES", str(256 * 1024)))
    if payload_bytes > max_payload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Run payload exceeds the {max_payload_bytes}-byte enqueue limit",
        )


def launch_recorded_run(db: Session, launcher: RunLauncher, tenant_id: str, run: dict[str, Any]) -> None:
    """Enqueue exactly what the stored row says (hand-off contract), so a retry starts the recorded run.

    Called whether or not this request created the row: DBOS attaches a repeated workflow_id to the
    existing workflow, so a crash between insert and enqueue heals when the client retries.
    """
    version_row = _version_row(db, run["agent_id"], run["agent_version"])
    assert version_row is not None  # versions are immutable and never deleted
    try:
        launcher.launch(
            run_id=uuid.UUID(str(run["id"])),
            tenant_id=tenant_id,
            agent_id=uuid.UUID(str(run["agent_id"])),
            version=run["agent_version"],
            spec_json=json.dumps(version_row.spec),
            input_text=run["input"],
            traceparent=run.get("traceparent"),
        )
    except Exception as exc:
        # Type and code only: the error can quote the DBOS database URL or the enqueued payload.
        log_failure(
            log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
            run_id=run["id"], tenant_id=tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Run recorded but could not be started; retry the same request (same run_id)",
        )
    log.info("run %s queued: tenant=%s agent=%s version=%s", run["id"], tenant_id, run["agent_id"], run["agent_version"])


def started_response(run: dict[str, Any], created: bool, extra: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content={
            "run_id": str(run["id"]),
            "status": run["status"],
            "display_status": run["display_status"],
            "agent_id": str(run["agent_id"]),
            "version": run["agent_version"],
            "trace_id": run["trace_id"],
            **(extra or {}),
        },
    )


@router.post("/agents/{agent_id}/runs", status_code=status.HTTP_201_CREATED, responses={409: {"model": ErrorDetail}})
def start_run(
    agent_id: uuid.UUID,
    body: RunCreate,
    principal: PrincipalDep,
    db: SessionDep,
    stores: StoresDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)],
) -> JSONResponse:
    """Insert the run idempotently (client run_id), then enqueue its workflow (durability rule 1).

    201 when this call created the run, 200 when the same tenant already has it. The enqueue is
    attempted in both cases, so a crash between insert and enqueue heals when the client retries;
    DBOS attaches a repeated workflow_id to the existing workflow. A run without a conversation;
    conversation turns start through POST /conversations/{id}/runs.

    The request's trace id and traceparent are stored on the run; the worker's run span joins that
    trace, and the response carries `trace_id`.
    """
    run_store, _ = stores
    tenant_id = principal.tenant_id
    agent = (
        db.query(AgentModel).filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id).first()
    )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    version_num = resolve_version(db, agent_id, body.version)
    check_payload_size(db, agent_id, version_num, body.input)
    # The request span (FastAPI instrumentation) is current here; (None, None) when telemetry is off.
    trace_id, traceparent = current_trace_context()

    try:
        run_id, created = run_store.create_run(
            tenant_id, agent_id, version_num, body.input, body.run_id,
            trace_id=trace_id, traceparent=traceparent,
        )
    except RunIdConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
    run = _get_run_or_404(run_store, tenant_id, run_id)

    if not created and (
        run["agent_id"] != str(agent_id)
        or run["input"] != body.input
        or (body.version is not None and run["agent_version"] != body.version)
        or run["conversation_id"] is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
        )

    launch_recorded_run(db, launcher, tenant_id, run)
    return started_response(run, created)


@router.get("/runs")
def list_runs(
    principal: PrincipalDep,
    stores: StoresDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[
        str | None,
        Query(max_length=MAX_CURSOR_LENGTH, description="Opaque cursor: the previous page's next_before, verbatim"),
    ] = None,
    agent_id: uuid.UUID | None = None,
    run_status: Annotated[str | None, Query(alias="status")] = None,
) -> JSONResponse:
    """The tenant's runs, newest first (the Studio Traces and Logs pages), ordered by
    (created_at, run id) descending. `next_before` is an opaque cursor naming the last item's
    position when a full page came back (pass it back verbatim as `before`), else null. A cursor
    this API did not issue is a 422."""
    run_store, _ = stores
    cursor = None
    if before is not None:
        try:
            cursor = RunCursor.decode(before)
        except InvalidRunCursor:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="before is not a valid cursor; pass the previous page's next_before verbatim",
            )
    runs = run_store.list_runs(principal.tenant_id, limit=limit, before=cursor, agent_id=agent_id, status=run_status)
    next_before = RunCursor.after(runs[-1]).encode() if len(runs) == limit else None
    return JSONResponse(content={"items": [_run_json(r) for r in runs], "next_before": next_before})


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, principal: PrincipalDep, stores: StoresDep) -> JSONResponse:
    run_store, _ = stores
    return JSONResponse(content=_run_json(_get_run_or_404(run_store, principal.tenant_id, run_id)))


@router.get("/runs/{run_id}/events")
def list_run_events(
    run_id: uuid.UUID, principal: PrincipalDep, stores: StoresDep, after_seq: int = -1
) -> JSONResponse:
    run_store, event_log = stores
    _get_run_or_404(run_store, principal.tenant_id, run_id)
    events = event_log.list_events(principal.tenant_id, run_id, after_seq=after_seq)
    return JSONResponse(content=[event_json(e) for e in events])


@router.get("/runs/{run_id}/stream", response_class=StreamingResponse, responses={400: {"model": ErrorDetail}})
def stream_run(
    run_id: uuid.UUID,
    principal: PrincipalDep,
    stores: StoresDep,
    settings: Annotated[StreamSettings, Depends(get_stream_settings)],
    limiter: Annotated[StreamLimiter, Depends(get_stream_limiter)],
    delta_source: Annotated[DeltaSource, Depends(get_delta_source)],
    after_seq: int = -1,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """SSE: replay after `Last-Event-ID` (wins) or `?after_seq=`, then live events and deltas."""
    run_store, event_log = stores
    tenant_id = principal.tenant_id
    _get_run_or_404(run_store, tenant_id, run_id)
    if last_event_id is not None and last_event_id.strip():
        try:
            after_seq = int(last_event_id.strip())
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Last-Event-ID must be an integer seq")

    lease = limiter.acquire(f"{principal.tenant_id}:{principal.user_id}")
    if lease is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Concurrent SSE stream limit reached; retry after another stream closes",
        )

    return StreamingResponse(
        release_stream_slot(
            run_event_stream(
                run_id=str(run_id),
                after_seq=after_seq,
                load_events=lambda after: event_log.list_events(tenant_id, run_id, after_seq=after),
                load_run=lambda: run_store.get_run(tenant_id, run_id),
                delta_source=delta_source,
                settings=settings,
            ),
            lease,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
