"""Run routes: start a run, list runs, read one, read its events, stream it (SSE with replay).

Runs and events live in Postgres (eap_run_store, JSONB + row locks); on any other database these
routes answer 503. Every lookup is tenant-scoped: another tenant's or an unknown id is a 404.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from eap_observability import current_trace_context, log_failure

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.run_options import (
    DRAFT_HANDOFF_VERSION,
    check_spec_payload_size,
    resolve_run_options,
    same_options,
    version_row as _version_row,
)
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
from eap_run_store import (
    ENQUEUE_FAILED_ERROR,
    EventLog,
    InvalidRunCursor,
    MetricsQueryTimeout,
    RunCursor,
    RunIdConflict,
    RunStore,
)
from eap_run_store.cursor import MAX_CURSOR_LENGTH

log = logging.getLogger("eap_api")

SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_run_storage(request: Request) -> tuple[RunStore, EventLog]:
    return request.app.state.run_stores


StoresDep = Annotated[tuple[RunStore, EventLog], Depends(require_run_storage)]

router = APIRouter(
    tags=["runs"], responses={404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}}, route_class=GuardedJsonRoute
)


def _run_json(run: dict[str, Any]) -> dict[str, Any]:
    """The API's run shape: `run_id`/`version` match the POST response; timestamps as ISO strings.

    `kind` is 'agent', 'tool', 'workflow' or 'kb_ingest' (a knowledge base ingest or reindex run:
    `knowledge_base_id` set, `input` its JSON mode, see eap_api.launcher). An agent run has `agent_id`/`version` and `tool_id`
    null; a tool run has `tool_id`, `agent_id`/`version` null, and `input`/`output` as JSON text; a
    workflow run has `workflow_id`/`workflow_version`, input/output as JSON text. A workflow's agent
    step runs as an agent run with `parent_run_id`/`parent_step_id` set (null otherwise)."""
    iso = lambda v: v.isoformat() if v is not None else None  # noqa: E731
    return {
        "run_id": str(run["id"]),
        "kind": run.get("kind") or "agent",
        "agent_id": str(run["agent_id"]) if run["agent_id"] is not None else None,
        "version": run["agent_version"],
        "tool_id": run.get("tool_id"),
        "workflow_id": run.get("workflow_id"),
        "workflow_version": run.get("workflow_version"),
        # The knowledge base of a kb_ingest run, or of a tool run of the knowledge base search tester.
        "knowledge_base_id": str(run["knowledge_base_id"]) if run.get("knowledge_base_id") is not None else None,
        "parent_run_id": run.get("parent_run_id"),
        "parent_step_id": run.get("parent_step_id"),
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
        # Usage and cost (P0.1). Totals are null until the run finishes, and for runs recorded
        # before usage existed ("not recorded"); cost_usd is a decimal string, null when any of the
        # run's model requests had no price.
        "model_id": run.get("model_id"),
        "model_overridden": bool(run.get("model_overridden") or False),
        "input_tokens": run.get("input_tokens"),
        "output_tokens": run.get("output_tokens"),
        "cost_usd": _decimal_str(run.get("cost_usd")),
        "started_at": iso(run.get("started_at")),
        "finished_at": iso(run.get("finished_at")),
        # Run options (P0.8): a draft test run has `version` null and `draft` true; the validated
        # request context ({} when none).
        "draft": run.get("draft_spec") is not None,
        "request_context": run.get("request_context") or {},
    }


def _decimal_str(value: Any) -> str | None:
    """A Numeric(20, 10) value as a plain decimal string ("0.0001230000"), or None."""
    return None if value is None else format(value, "f")


def _get_run_or_404(run_store: RunStore, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
    run = run_store.get_run(tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


def check_payload_size(db: Session, agent_id: uuid.UUID | str, version: int, input_text: str) -> None:
    version_row = _version_row(db, agent_id, version)
    assert version_row is not None
    check_spec_payload_size(version_row.spec, input_text)


def launch_recorded_run(
    db: Session, launcher: RunLauncher, run_store: RunStore, tenant_id: str, run: dict[str, Any], created: bool
) -> dict[str, Any]:
    """Hand the stored run to the orchestrator; returns the run as it stands afterwards.

    Enqueues exactly what the stored row says (hand-off contract), so a retry starts the recorded
    run, whether or not this request created the row: DBOS attaches a repeated workflow_id to the
    existing workflow, so a crash between insert and enqueue heals when the client retries.

    Same semantics as tool runs (POST /tools/{id}/runs). This request owns the hand-off of a row it
    created, or of one an earlier failed hand-off left failed and it re-queues now. A failed
    enqueue answers 503, and an owned run still `queued` is marked failed with ENQUEUE_FAILED_ERROR
    so it is not left queued with no workflow; a retry with the same run_id and input re-queues it
    and enqueues again. If DBOS did accept the enqueue that raised, the workflow runs anyway and
    its start step (mark_running) clears that error. A conversation turn is only re-queued while it
    is the latest turn: once a later turn has started, the retry answers 409.
    """
    run_id = uuid.UUID(str(run["id"]))
    owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)
    if not owns_handoff and run["conversation_id"] is not None:
        current = run_store.get_run(tenant_id, run_id) or run
        if current["status"] == "failed" and current["error"] == ENQUEUE_FAILED_ERROR:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This turn could not be started and a later turn has started since; start a new turn",
            )
    if run.get("draft_spec") is not None:
        # A draft test run: always its recorded snapshot, never the live draft (P0.8).
        spec, handoff_version = run["draft_spec"], DRAFT_HANDOFF_VERSION
    else:
        version_row = _version_row(db, run["agent_id"], run["agent_version"])
        assert version_row is not None  # versions are immutable and never deleted
        spec, handoff_version = version_row.spec, run["agent_version"]
    try:
        launcher.launch(
            run_id=run_id,
            tenant_id=tenant_id,
            agent_id=uuid.UUID(str(run["agent_id"])),
            version=handoff_version,
            spec_json=json.dumps(spec),
            input_text=run["input"],
            traceparent=run.get("traceparent"),
        )
    except Exception as exc:
        # Type and code only: the error can quote the DBOS database URL or the enqueued payload.
        log_failure(
            log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
            run_id=run["id"], tenant_id=tenant_id,
        )
        if owns_handoff:
            try:
                run_store.mark_enqueue_failed(tenant_id, run_id)
            except Exception as mark_exc:  # noqa: BLE001 - the 503 below is the answer either way
                log_failure(
                    log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                    level=logging.ERROR, run_id=run["id"], tenant_id=tenant_id,
                )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Run recorded but could not be started; retry the same request (same run_id)",
        )
    run = run_store.get_run(tenant_id, run_id) or run
    if run["error"] == ENQUEUE_FAILED_ERROR:
        # A concurrent request that owned the hand-off failed and marked the run failed, but this
        # enqueue succeeded: the workflow exists, and its start step clears that error.
        run = {**run, "status": "queued", "display_status": "queued", "error": None}
    log.info("run %s queued: tenant=%s agent=%s version=%s", run["id"], tenant_id, run["agent_id"], run["agent_version"])
    return run


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

    # A retry of a recorded run is compared with the row (below) and relaunched as recorded; only a
    # new run validates its options (a draft run against the live draft, then snapshotted).
    run = run_store.get_run(tenant_id, body.run_id)
    created = False
    if run is None:
        options = resolve_run_options(
            db, tenant_id, agent,
            version=body.version, request_context=body.request_context, model=body.model, input_text=body.input,
        )
        # The request span (FastAPI instrumentation) is current here; (None, None) when telemetry is off.
        trace_id, traceparent = current_trace_context()
        try:
            run_id, created = run_store.create_run(
                tenant_id, agent_id, options.target.version, body.input, body.run_id,
                trace_id=trace_id, traceparent=traceparent, **options.store_kwargs(),
            )
        except RunIdConflict:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
        run = _get_run_or_404(run_store, tenant_id, run_id)

    if not created and (
        run["agent_id"] != str(agent_id)
        or run["input"] != body.input
        or run["conversation_id"] is not None
        or not same_options(db, run, version=body.version, request_context=body.request_context, model=body.model)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
        )

    run = launch_recorded_run(db, launcher, run_store, tenant_id, run, created)
    return started_response(run, created)


MAX_STATUS_FILTERS = 10
RUN_LIST_TIMEOUT_DETAIL = "Run list query timed out; narrow the time range or the filters"


def _status_filter(values: list[str] | None) -> list[str] | None:
    """`?status=a&status=b,c` as a de-duplicated list, None when absent (422 past MAX_STATUS_FILTERS)."""
    if not values:
        return None
    statuses = list(dict.fromkeys(s.strip() for v in values for s in v.split(",") if s.strip()))
    if len(statuses) > MAX_STATUS_FILTERS or any(len(s) > 32 for s in statuses):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"at most {MAX_STATUS_FILTERS} status values of at most 32 characters are allowed",
        )
    return statuses or None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
    run_status: Annotated[list[str] | None, Query(alias="status")] = None,
    kind: Annotated[Literal["agent", "tool", "workflow", "kb_ingest"] | None, Query()] = None,
    tool_id: Annotated[str | None, Query(max_length=64)] = None,
    workflow_id: uuid.UUID | None = None,
    parent_run_id: uuid.UUID | None = None,
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
    model: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    provider: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> JSONResponse:
    """The tenant's runs, newest first (the Studio Traces and Logs pages), ordered by
    (created_at, run id) descending. `next_before` is an opaque cursor naming the last item's
    position when a full page came back (pass it back verbatim as `before`), else null. A cursor
    this API did not issue is a 422. `kind` ('agent' | 'tool' | 'workflow' | 'kb_ingest'), `tool_id` and
    `workflow_id` filter. A workflow's child agent runs are listed only with `parent_run_id`.

    Metrics drill-down (P0.6), same meaning as in GET /metrics/*: `status` may be repeated or
    comma-separated (any of them; at most 10), `from` / `to` bound `created_at` to `[from, to)`
    (ISO 8601, UTC without an offset; `from` must be before `to`), `model` (`provider:name`) and
    `provider` keep runs that used that model, including through a workflow's child runs."""
    statuses = _status_filter(run_status)
    start = _utc(created_from) if created_from is not None else None
    end = _utc(created_to) if created_to is not None else None
    if start is not None and end is not None and start >= end:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="from must be before to")
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
    try:
        runs = run_store.list_runs(
            principal.tenant_id, limit=limit, before=cursor, agent_id=agent_id, status=statuses, kind=kind,
            tool_id=tool_id, workflow_id=workflow_id, parent_run_id=parent_run_id, created_from=start,
            created_to=end, model=model, provider=provider,
        )
    except MetricsQueryTimeout:
        # Only with `model` / `provider`: that filter runs under the metrics statement timeout.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=RUN_LIST_TIMEOUT_DETAIL)
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
