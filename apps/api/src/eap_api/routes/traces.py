"""Built-in trace view data (P0.7): `GET /runs/{run_id}/trace`.

The trace is derived from the run's durable events and usage rows (eap_run_store.trace): one span
per run, model request, tool call, processor result and workflow step, with a workflow's child
agent runs nested under their step, plus the RAG and memory spans of RM.19 (retrieve, embedding,
memory_recall, memory_index, observer, reflector, working_memory, ingest) and AC.17's agent
capability spans (fallback_attempt under its model request, tool_approval under its tool call,
output_retry, eval_case with its child run and judge requests). A run waiting for a tool approval
has span status `waiting` and `display_status` `waiting_approval`. Tenant-scoped like every run route: an unknown run, or another
tenant's, is a 404 (its child runs are read under the caller's tenant only). The DBOS and HTTP
spans stay in Grafana (the run's `trace_id`).

External traces (sent over OTLP, POST /otlp/v1/traces): `GET /traces` lists them, `GET /traces/{trace_id}`
reads one (32 hex trace id, as the client sent it); both tenant-scoped.

Feedback (W2.2, 0019_trace_feedback): `POST | GET /traces/{trace_id}/feedback`; a trace id is the run id.

Response: `{run, spans, totals, truncated, limits}`; `run` is the GET /runs/{id} shape. See
eap_run_store.trace for the span shape, ids, statuses, timing and the payload bounds.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from eap_api.deps import SessionDep, get_trace_feedback_store
from eap_api.auth import Principal, current_principal
from eap_api.routes.runs import SpanStoreDep, StoresDep, _run_json, _utc
from eap_api.models import AgentVersionModel
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowVersionModel
from eap_run_store.cursor import MAX_CURSOR_LENGTH
from eap_run_store.spans import InvalidSpanCursor, SpanCursor
from eap_run_store.trace import TraceQueryTimeout, TraceReader
from eap_run_store.trace_feedback import (
    FEEDBACK_COMMENT_MAX_CHARS,
    FEEDBACK_SCORE_LIMIT,
    FEEDBACK_SPAN_ID_MAX_CHARS,
    TraceFeedbackStore,
)
from eap_api.request_guard import GuardedJsonRoute

TIMEOUT_DETAIL = "Trace query timed out; retry later"

router = APIRouter(
    tags=["traces"], responses={404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}}, route_class=GuardedJsonRoute
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _read_trace(run_id: uuid.UUID, principal: Principal, stores: Any, db: Session) -> dict[str, Any]:
    run_store, _ = stores
    tenant_id = principal.tenant_id

    def workflow_spec(workflow_id: str, version: int) -> dict[str, Any] | None:
        row = (
            db.query(WorkflowVersionModel.spec)
            .filter(
                WorkflowVersionModel.tenant_id == tenant_id,
                WorkflowVersionModel.workflow_id == str(workflow_id),
                WorkflowVersionModel.version == version,
            )
            .first()
        )
        return dict(row.spec) if row is not None else None

    def agent_spec(agent_id: str, version: int) -> dict[str, Any] | None:
        # RM.19: a retrieve span's knowledge base refs (top_k, min_score); read only for runs that
        # called retrieve, under the caller's tenant.
        row = (
            db.query(AgentVersionModel.spec)
            .filter(
                AgentVersionModel.tenant_id == tenant_id,
                AgentVersionModel.agent_id == str(agent_id),
                AgentVersionModel.version == version,
            )
            .first()
        )
        return dict(row.spec) if row is not None else None

    try:
        trace = TraceReader(run_store.engine).read(
            tenant_id, run_id, workflow_spec=workflow_spec, agent_spec=agent_spec
        )
    except TraceQueryTimeout:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=TIMEOUT_DETAIL) from None
    if trace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    chat = run_store.chat_usage(tenant_id, [trace["run"]["id"]])
    trace["run"] = _run_json(trace["run"], chat.get(str(trace["run"]["id"])))
    return trace


@router.get("/runs/{run_id}/trace")
def get_run_trace(run_id: uuid.UUID, principal: PrincipalDep, stores: StoresDep, db: SessionDep) -> JSONResponse:
    """The run's span tree with tokens and cost. 404 for an unknown run or another tenant's; 503
    when reading it hits the statement timeout."""
    return JSONResponse(content=_read_trace(run_id, principal, stores, db))


# ---------------------------------------------------------------------------- external traces (OTLP)

TRACE_ID_PATTERN = r"^[0-9a-f]{32}$"
RUN_ID_ATTRIBUTE = "pydanticos.run_id"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@router.get("/traces")
def list_external_traces(
    principal: PrincipalDep,
    spans: SpanStoreDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH, description="The previous page's next_before, verbatim")] = None,
    source: Annotated[Literal["external"], Query(description="Only external OTLP traces are listed here; platform traces are runs (GET /runs)")] = "external",
    service: Annotated[str | None, Query(min_length=1, max_length=128, description="The resource service.name")] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200, description="Substring of a span name")] = None,
    trace_status: Annotated[Literal["ok", "error"] | None, Query(alias="status")] = None,
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> JSONResponse:
    """The tenant's external traces (sent over OTLP), newest first, one item per trace id:
    `{items: [{trace_id, source, name, service_name, start, end, span_count, status}], next_before}`.
    `status` is `error` when any span of the trace errored. 422 for a `before` this API did not issue."""
    start = _utc(created_from) if created_from is not None else None
    end = _utc(created_to) if created_to is not None else None
    if start is not None and end is not None and start >= end:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="from must be before to")
    cursor = None
    if before is not None:
        try:
            cursor = SpanCursor.decode(before)
        except InvalidSpanCursor:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="before is not a valid cursor; pass the previous page's next_before verbatim",
            ) from None
    filters = {"service": service, "q": q.strip() if q and q.strip() else None, "status": trace_status,
               "from": start, "to": end}
    items, next_before = spans.list_traces(principal.tenant_id, {k: v for k, v in filters.items() if v is not None}, limit, cursor)
    return JSONResponse(content={
        "items": [
            {"trace_id": i["trace_id"], "source": "external", "name": i["name"], "service_name": i["service_name"],
             "start": _iso(i["start"]), "end": _iso(i["end"]), "span_count": i["span_count"], "status": i["status"]}
            for i in items
        ],
        "next_before": next_before,
    })


@router.get("/traces/{trace_id}")
def get_external_trace(
    trace_id: Annotated[str, Path(pattern=TRACE_ID_PATTERN)], principal: PrincipalDep, spans: SpanStoreDep, stores: StoresDep,
) -> JSONResponse:
    """One external trace: `{trace_id, source, service_name, resource_attributes, spans: [{span_id,
    parent_id, name, start, end, status, attributes}], run_id, truncated}`. `run_id` is the platform run
    named by a span's `pydanticos.run_id` attribute when that run is the tenant's, else null. 404 for an
    unknown trace or another tenant's."""
    trace = spans.read_external_trace(principal.tenant_id, trace_id)
    if trace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found")
    run_store, _ = stores
    run_id = None
    for span in trace["spans"]:
        candidate = span["attributes"].get(RUN_ID_ATTRIBUTE)
        try:
            parsed = uuid.UUID(candidate) if isinstance(candidate, str) else None
        except ValueError:
            parsed = None
        if parsed is not None and run_store.get_run(principal.tenant_id, parsed) is not None:
            run_id = str(parsed)
            break
    return JSONResponse(content={
        "trace_id": trace["trace_id"], "source": "external", "service_name": trace["service_name"],
        "resource_attributes": trace["resource_attributes"],
        "spans": [{**{k: v for k, v in s.items() if k != "service_name"}, "start": _iso(s["start"]), "end": _iso(s["end"])}
                  for s in trace["spans"]],
        "run_id": run_id, "truncated": trace["truncated"],
    })


# ---------------------------------------------------------------------------- feedback (W2.2)


class FeedbackCreate(BaseModel):
    """Body of POST /traces/{trace_id}/feedback."""

    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=FEEDBACK_COMMENT_MAX_CHARS)
    score: float | None = Field(default=None, ge=-FEEDBACK_SCORE_LIMIT, le=FEEDBACK_SCORE_LIMIT, allow_inf_nan=False)
    span_id: str | None = Field(default=None, min_length=1, max_length=FEEDBACK_SPAN_ID_MAX_CHARS)


FeedbackStoreDep = Annotated[TraceFeedbackStore, Depends(get_trace_feedback_store)]


@router.post("/traces/{trace_id}/feedback", status_code=status.HTTP_201_CREATED)
def add_trace_feedback(
    trace_id: uuid.UUID, body: FeedbackCreate, principal: PrincipalDep, stores: StoresDep, db: SessionDep,
    store: FeedbackStoreDep,
) -> JSONResponse:
    """Record feedback on a trace (a run) or, with `span_id`, on one of its spans: a thumbs up or
    down, an optional comment and numeric score, with who and when. 404 for an unknown trace or
    another tenant's; 422 for a span that is not in the trace."""
    run_store, _ = stores
    if run_store.get_run(principal.tenant_id, trace_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found")
    if body.span_id is not None:
        trace = _read_trace(trace_id, principal, stores, db)
        if body.span_id not in {span["id"] for span in trace["spans"]}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="span_id is not a span of this trace")
    record = store.add(
        principal.tenant_id, trace_id, rating=body.rating, created_by=principal.user_id,
        span_id=body.span_id, score=body.score, comment=(body.comment or "").strip() or None,
    )
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=record.as_json())


@router.get("/traces/{trace_id}/feedback")
def list_trace_feedback(
    trace_id: uuid.UUID, principal: PrincipalDep, stores: StoresDep, store: FeedbackStoreDep,
    span_id: Annotated[str | None, Query(min_length=1, max_length=FEEDBACK_SPAN_ID_MAX_CHARS)] = None,
) -> JSONResponse:
    """The trace's feedback, newest first (`span_id` narrows to one span). 404 for an unknown trace
    or another tenant's."""
    run_store, _ = stores
    if run_store.get_run(principal.tenant_id, trace_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trace not found")
    items = store.list(principal.tenant_id, trace_id, span_id=span_id)
    return JSONResponse(content={"items": [r.as_json() for r in items]})
