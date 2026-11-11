"""Run routes: start a run, read it, read its events, stream it (SSE with replay).

Runs and events live in Postgres (eap_run_store, JSONB + row locks); on any other database these
routes answer 503. Every lookup is tenant-scoped: another tenant's or an unknown id is a 404.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel, AgentVersionModel
from eap_api.schemas import ErrorDetail, RunCreate
from eap_api.sse import DeltaSource, StreamSettings, event_json, get_delta_source, get_stream_settings, run_event_stream
from eap_run_store import EventLog, RunIdConflict, RunStore

log = logging.getLogger("eap_api")

SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_run_storage(request: Request) -> tuple[RunStore, EventLog]:
    stores = getattr(request.app.state, "run_stores", None)
    if stores is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runs require a Postgres database (EAP_DATABASE_URL)",
        )
    return stores


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
    DBOS attaches a repeated workflow_id to the existing workflow.
    """
    run_store, _ = stores
    tenant_id = principal.tenant_id
    agent = (
        db.query(AgentModel).filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id).first()
    )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    if body.version is None:
        latest = (
            db.query(func.max(AgentVersionModel.version)).filter(AgentVersionModel.agent_id == str(agent_id)).scalar()
        )
        if latest is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent has no published version")
        version_num = latest
    else:
        version_num = body.version
    if _version_row(db, agent_id, version_num) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")

    try:
        run_id, created = run_store.create_run(tenant_id, agent_id, version_num, body.input, body.run_id)
    except RunIdConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
    run = _get_run_or_404(run_store, tenant_id, run_id)

    if not created and (
        run["agent_id"] != str(agent_id)
        or run["input"] != body.input
        or (body.version is not None and run["agent_version"] != body.version)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
        )

    # Always launch what the stored row says, so a retry starts exactly the run that was recorded.
    version_row = _version_row(db, agent_id, run["agent_version"])
    assert version_row is not None  # versions are immutable and never deleted
    try:
        launcher.launch(
            run_id=run_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            version=run["agent_version"],
            spec_json=json.dumps(version_row.spec),
            input_text=run["input"],
        )
    except Exception:
        log.exception("enqueue failed for run %s", run_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Run recorded but could not be started; retry the same request (same run_id)",
        )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content={
            "run_id": str(run_id),
            "status": run["status"],
            "display_status": run["display_status"],
            "agent_id": str(agent_id),
            "version": run["agent_version"],
        },
    )


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

    return StreamingResponse(
        run_event_stream(
            run_id=str(run_id),
            after_seq=after_seq,
            load_events=lambda after: event_log.list_events(tenant_id, run_id, after_seq=after),
            load_run=lambda: run_store.get_run(tenant_id, run_id),
            delta_source=delta_source,
            settings=settings,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
