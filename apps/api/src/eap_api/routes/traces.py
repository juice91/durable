"""Built-in trace view data (P0.7): `GET /runs/{run_id}/trace`.

The trace is derived from the run's durable events and usage rows (the run store's trace module): one span
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
the run store's trace module for the span shape, ids, statuses, timing and the payload bounds.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.runs import SpanServiceDep, TraceServiceDep
from eap_api.services.runs import MAX_CURSOR_LENGTH
from eap_api.services.traces import (
    FEEDBACK_COMMENT_MAX_CHARS,
    FEEDBACK_SCORE_LIMIT,
    FEEDBACK_SPAN_ID_MAX_CHARS,
)

router = APIRouter(
    tags=["traces"], responses={404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}}, route_class=GuardedJsonRoute
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]


@router.get("/runs/{run_id}/trace")
def get_run_trace(run_id: uuid.UUID, principal: PrincipalDep, service: TraceServiceDep) -> JSONResponse:
    """The run's span tree with tokens and cost. 404 for an unknown run or another tenant's; 503
    when reading it hits the statement timeout."""
    return JSONResponse(content=service.read_trace(principal.tenant_id, run_id))


# ---------------------------------------------------------------------------- external traces (OTLP)

TRACE_ID_PATTERN = r"^[0-9a-f]{32}$"


@router.get("/traces")
def list_external_traces(
    principal: PrincipalDep,
    service: SpanServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH, description="The previous page's next_before, verbatim")] = None,
    source: Annotated[Literal["external"], Query(description="Only external OTLP traces are listed here; platform traces are runs (GET /runs)")] = "external",
    service_name: Annotated[str | None, Query(min_length=1, max_length=128, description="The resource service.name", alias="service")] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200, description="Substring of a span name")] = None,
    trace_status: Annotated[Literal["ok", "error"] | None, Query(alias="status")] = None,
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> JSONResponse:
    """The tenant's external traces (sent over OTLP), newest first, one item per trace id:
    `{items: [{trace_id, source, name, service_name, start, end, span_count, status}], next_before}`.
    `status` is `error` when any span of the trace errored. 422 for a `before` this API did not issue."""
    return JSONResponse(content=service.list_external_traces(
        principal.tenant_id, limit=limit, before=before, service=service_name, q=q, trace_status=trace_status,
        created_from=created_from, created_to=created_to,
    ))


@router.get("/traces/{trace_id}")
def get_external_trace(
    trace_id: Annotated[str, Path(pattern=TRACE_ID_PATTERN)], principal: PrincipalDep, service: SpanServiceDep,
) -> JSONResponse:
    """One external trace: `{trace_id, source, service_name, resource_attributes, spans: [{span_id,
    parent_id, name, start, end, status, attributes}], run_id, truncated}`. `run_id` is the platform run
    named by a span's `pydanticos.run_id` attribute when that run is the tenant's, else null. 404 for an
    unknown trace or another tenant's."""
    return JSONResponse(content=service.read_external_trace(principal.tenant_id, trace_id))


# ---------------------------------------------------------------------------- feedback (W2.2)


class FeedbackCreate(BaseModel):
    """Body of POST /traces/{trace_id}/feedback."""

    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=FEEDBACK_COMMENT_MAX_CHARS)
    score: float | None = Field(default=None, ge=-FEEDBACK_SCORE_LIMIT, le=FEEDBACK_SCORE_LIMIT, allow_inf_nan=False)
    span_id: str | None = Field(default=None, min_length=1, max_length=FEEDBACK_SPAN_ID_MAX_CHARS)


@router.post("/traces/{trace_id}/feedback", status_code=status.HTTP_201_CREATED)
def add_trace_feedback(
    trace_id: uuid.UUID, body: FeedbackCreate, principal: PrincipalDep, service: TraceServiceDep,
) -> JSONResponse:
    """Record feedback on a trace (a run) or, with `span_id`, on one of its spans: a thumbs up or
    down, an optional comment and numeric score, with who and when. 404 for an unknown trace or
    another tenant's; 422 for a span that is not in the trace."""
    record = service.add_feedback(
        principal.tenant_id, principal.user_id, trace_id, rating=body.rating, comment=body.comment,
        score=body.score, span_id=body.span_id,
    )
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=record)


@router.get("/traces/{trace_id}/feedback")
def list_trace_feedback(
    trace_id: uuid.UUID, principal: PrincipalDep, service: TraceServiceDep,
    span_id: Annotated[str | None, Query(min_length=1, max_length=FEEDBACK_SPAN_ID_MAX_CHARS)] = None,
) -> JSONResponse:
    """The trace's feedback, newest first (`span_id` narrows to one span). 404 for an unknown trace
    or another tenant's."""
    return JSONResponse(content=service.list_feedback(principal.tenant_id, trace_id, span_id))
