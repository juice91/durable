"""Run routes: start a run, list runs, read one, read its events, stream it (SSE with replay).

Runs and events live in Postgres (the run store, JSONB + row locks); on any other database these
routes answer 503. Every lookup is tenant-scoped: another tenant's or an unknown id is a 404.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse

from eap_api.auth import Principal, current_principal
from eap_api.deps import (  # noqa: F401  (require_run_storage: re-exported)
    get_delta_source,
    get_stream_limiter,
    require_run_storage,
)
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail, RunCreate
from eap_api.service_deps.runs import RunServiceDep, SpanServiceDep
from eap_api.service_errors import to_http_exception
from eap_api.services import runs as run_core
from eap_api.services.errors import ServiceError
from eap_api.services.runs import MAX_CURSOR_LENGTH, RunService, output_json as _output_json, run_json as _run_json  # noqa: F401
from eap_api.sse import (
    DeltaSource,
    StreamLimiter,
    StreamSettings,
    event_json,
    get_stream_settings,
    release_stream_slot,
    run_event_stream,
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]

router = APIRouter(
    tags=["runs"], responses={404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}}, route_class=GuardedJsonRoute
)


def _http(call: Any, *args: Any) -> Any:
    """Run a service function for the routes that still call these helpers (conversations, AG-UI): its
    refusal becomes the HTTPException they always raised."""
    try:
        return call(*args)
    except ServiceError as exc:
        raise to_http_exception(exc) from None


def recovery_for(launcher: Any, run_ids: list[str]) -> dict[str, int]:
    return run_core.recovery_for(launcher, run_ids)


def _get_run_or_404(run_store: Any, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
    return _http(run_core.get_run_or_404, run_store, tenant_id, run_id)


def lock_agent(db: Any, tenant_id: str, agent_id: uuid.UUID | str) -> Any:
    """The tenant's agent locked FOR SHARE, or 404 (see services.runs.lock_agent)."""
    return _http(run_core.lock_agent, db, tenant_id, agent_id)


def launch_recorded_run(
    db: Any, launcher: Any, run_store: Any, tenant_id: str, run: dict[str, Any], created: bool
) -> dict[str, Any]:
    """Hand the stored run to the orchestrator (see services.runs.launch_recorded_run); 503 / 409 on refusal."""
    return _http(run_core.launch_recorded_run, db, launcher, run_store, tenant_id, run, created)


def started_response(run: dict[str, Any], created: bool, extra: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content={
            "run_id": str(run["id"]),
            "status": run["status"],
            "display_status": run["display_status"],
            "agent_id": str(run["agent_id"]),
            "version": run["agent_version"],
            "draft": run.get("draft_spec") is not None,
            "model_id": run.get("model_id"),
            "model_overridden": bool(run.get("model_overridden") or False),
            "trace_id": run["trace_id"],
            **(extra or {}),
        },
    )


@router.post("/agents/{agent_id}/runs", status_code=status.HTTP_201_CREATED, responses={409: {"model": ErrorDetail}})
def start_run(
    agent_id: uuid.UUID,
    body: RunCreate,
    principal: PrincipalDep,
    service: RunServiceDep,
) -> JSONResponse:
    """Insert the run idempotently (client run_id), then enqueue its workflow (durability rule 1).

    201 when this call created the run, 200 when the same tenant already has it. The enqueue is
    attempted in both cases, so a crash between insert and enqueue heals when the client retries;
    DBOS attaches a repeated workflow_id to the existing workflow. A run without a conversation;
    conversation turns start through POST /conversations/{id}/runs.

    The request's trace id and traceparent are stored on the run; the worker's run span joins that
    trace, and the response carries `trace_id`.
    """
    run, created = service.start_run(
        principal.tenant_id, agent_id, run_id=body.run_id, input_text=body.input, version=body.version,
        request_context=body.request_context, model=body.model, model_settings=body.model_settings,
    )
    return started_response(run, created)


@router.get("/runs", description=inspect.cleandoc(RunService.list_runs.__doc__ or ""))
def list_runs(
    principal: PrincipalDep,
    service: RunServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[
        str | None,
        Query(max_length=MAX_CURSOR_LENGTH, description="Opaque cursor: the previous page's next_before, verbatim"),
    ] = None,
    agent_id: uuid.UUID | None = None,
    run_status: Annotated[list[str] | None, Query(alias="status")] = None,
    kind: Annotated[Literal["agent", "tool", "workflow", "kb_ingest", "eval", "kb_source_sync"] | None, Query()] = None,
    tool_id: Annotated[str | None, Query(max_length=64)] = None,
    workflow_id: uuid.UUID | None = None,
    knowledge_base_id: uuid.UUID | None = None,
    parent_run_id: uuid.UUID | None = None,
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
    model: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    provider: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    tags: Annotated[list[str] | None, Query(description="Run tags, repeated or comma-separated; all must match")] = None,
    metadata: Annotated[
        list[str] | None, Query(description="key=value pairs of the request context, repeated; all must match")
    ] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200, description="Free text")] = None,
) -> JSONResponse:
    return JSONResponse(content=service.list_runs(
        principal.tenant_id, limit=limit, before=before, agent_id=agent_id, run_status=run_status, kind=kind,
        tool_id=tool_id, workflow_id=workflow_id, knowledge_base_id=knowledge_base_id,
        parent_run_id=parent_run_id, created_from=created_from, created_to=created_to, model=model,
        provider=provider, tags=tags, metadata=metadata, q=q,
    ))


@router.get("/runs/histogram")
def runs_histogram(
    principal: PrincipalDep,
    service: SpanServiceDep,
    agent_id: uuid.UUID | None = None,
    run_status: Annotated[list[str] | None, Query(alias="status")] = None,
    kind: Annotated[Literal["agent", "tool", "workflow", "kb_ingest", "eval", "kb_source_sync"] | None, Query()] = None,
    tool_id: Annotated[str | None, Query(max_length=64)] = None,
    workflow_id: uuid.UUID | None = None,
    knowledge_base_id: uuid.UUID | None = None,
    parent_run_id: uuid.UUID | None = None,
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
    model: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    provider: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    tags: Annotated[list[str] | None, Query()] = None,
    metadata: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
) -> JSONResponse:
    """Runs per bucket over the `GET /runs` filters (same meaning), for the Traces page strip:
    `{interval, buckets: [{start, runs, failed}]}`, one bucket per UTC hour (a range under 72 hours)
    or day, zero-filled from the bucket holding `from` up to `to`. The range is `[from, to)`,
    default the last 7 days; `from` must be before `to` and the range at most 366 days (422)."""
    return JSONResponse(content=service.runs_histogram(
        principal.tenant_id, agent_id=agent_id, run_status=run_status, kind=kind, tool_id=tool_id,
        workflow_id=workflow_id, knowledge_base_id=knowledge_base_id, parent_run_id=parent_run_id,
        created_from=created_from, created_to=created_to, model=model, provider=provider, tags=tags,
        metadata=metadata, q=q,
    ))


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, principal: PrincipalDep, service: RunServiceDep) -> JSONResponse:
    return JSONResponse(content=service.read_run_json(principal.tenant_id, run_id))


@router.get("/runs/{run_id}/events")
def list_run_events(
    run_id: uuid.UUID, principal: PrincipalDep, service: RunServiceDep, after_seq: int = -1
) -> JSONResponse:
    events = service.list_events(principal.tenant_id, run_id, after_seq)
    return JSONResponse(content=[event_json(e) for e in events])


@router.get("/runs/{run_id}/stream", response_class=StreamingResponse, responses={400: {"model": ErrorDetail}})
def stream_run(
    run_id: uuid.UUID,
    principal: PrincipalDep,
    service: RunServiceDep,
    settings: Annotated[StreamSettings, Depends(get_stream_settings)],
    limiter: Annotated[StreamLimiter, Depends(get_stream_limiter)],
    delta_source: Annotated[DeltaSource, Depends(get_delta_source)],
    after_seq: int = -1,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """SSE: replay after `Last-Event-ID` (wins) or `?after_seq=`, then live events and deltas."""
    tenant_id = principal.tenant_id
    service.require_run(tenant_id, run_id)
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
                load_events=lambda after: service.events_after(tenant_id, run_id, after),
                load_run=lambda: service.get_run(tenant_id, run_id),
                delta_source=delta_source,
                settings=settings,
            ),
            lease,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
