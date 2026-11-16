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

Feedback (W2.2, 0019_trace_feedback): `POST | GET /traces/{trace_id}/feedback`; a trace id is the run id.

Response: `{run, spans, totals, truncated, limits}`; `run` is the GET /runs/{id} shape. See
eap_run_store.trace for the span shape, ids, statuses, timing and the payload bounds.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.routes.runs import StoresDep, _run_json
from eap_api.models import AgentVersionModel
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowVersionModel
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

SessionDep = Annotated[Session, Depends(get_session)]
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


# ---------------------------------------------------------------------------- feedback (W2.2)


class FeedbackCreate(BaseModel):
    """Body of POST /traces/{trace_id}/feedback."""

    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=FEEDBACK_COMMENT_MAX_CHARS)
    score: float | None = Field(default=None, ge=-FEEDBACK_SCORE_LIMIT, le=FEEDBACK_SCORE_LIMIT, allow_inf_nan=False)
    span_id: str | None = Field(default=None, min_length=1, max_length=FEEDBACK_SPAN_ID_MAX_CHARS)


def _feedback_store(request: Request, stores: StoresDep) -> TraceFeedbackStore:
    store = getattr(request.app.state, "trace_feedback_store", None)
    if store is None:
        run_store, _ = stores
        store = request.app.state.trace_feedback_store = TraceFeedbackStore(run_store.engine)
    return store


FeedbackStoreDep = Annotated[TraceFeedbackStore, Depends(_feedback_store)]


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
