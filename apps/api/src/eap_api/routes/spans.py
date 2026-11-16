"""`GET /spans`: the flat, materialised span list behind the Traces page's Spans tab.

Rows are written by the worker when a top-level agent, tool or workflow run finishes (eap_worker
spans_step, from the same derivation as `GET /runs/{id}/trace`); runs finished before that have none.
Newest first by `(start, span_id)`, cursor-paged, tenant-scoped.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import (
    RUN_LIST_TIMEOUT_DETAIL,
    SpanStoreDep,
    _list_param,
    _status_filter,
    _utc,
    metadata_filter,
)
from eap_api.schemas import ErrorDetail
from eap_run_store import MetricsQueryTimeout
from eap_run_store.cursor import MAX_CURSOR_LENGTH
from eap_run_store.spans import InvalidSpanCursor, SpanCursor
from eap_run_store.trace import SPAN_KINDS

router = APIRouter(tags=["spans"], responses={422: {"model": ErrorDetail}}, route_class=GuardedJsonRoute)
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@router.get("/spans")
def list_spans(
    principal: PrincipalDep,
    spans: SpanStoreDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[
        str | None,
        Query(max_length=MAX_CURSOR_LENGTH, description="Opaque cursor: the previous page's next_before, verbatim"),
    ] = None,
    span_kind: Annotated[Literal[*SPAN_KINDS] | None, Query()] = None,
    agent_id: uuid.UUID | None = None,
    run_status: Annotated[list[str] | None, Query(alias="status")] = None,
    kind: Annotated[Literal["agent", "tool", "workflow", "kb_ingest", "eval"] | None, Query()] = None,
    tool_id: Annotated[str | None, Query(max_length=64)] = None,
    workflow_id: uuid.UUID | None = None,
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
    model: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    provider: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    tags: Annotated[list[str] | None, Query()] = None,
    metadata: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200, description="Substring of the span name")] = None,
) -> JSONResponse:
    """`{items: [{run_id, span_id, parent_id, kind, name, start, end, status, agent_id, workflow_id,
    model}], next_before}`, newest span first. `from` / `to` bound the span's `start` to `[from, to)`;
    `span_kind` is the span's kind (run, model_request, tool_call, ...); `q` matches the span name
    (case-insensitive). The other filters are `GET /runs`' and apply to the span's run: `status`
    (any of, at most 10), `kind` (the run's kind), `tool_id`, `workflow_id`, `tags`, `metadata=key=value`,
    and `agent_id` / `model` / `provider` (the run used that model; `model` and `provider` run under the
    metrics statement timeout, 503 when it fires). `next_before` is the opaque cursor when a full
    page came back (pass it back verbatim), else null; one this API did not issue is a 422."""
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
            )
    filters = {
        "from": start, "to": end, "span_kind": span_kind, "q": q.strip() if q and q.strip() else None,
        "agent_id": agent_id, "status": _status_filter(run_status), "kind": kind, "tool_id": tool_id,
        "workflow_id": workflow_id, "model": model, "provider": provider,
        "tags": _list_param(tags, "tags", split=True), "metadata": metadata_filter(metadata),
    }
    try:
        items, next_before = spans.list(
            principal.tenant_id, {k: v for k, v in filters.items() if v is not None}, limit, cursor
        )
    except MetricsQueryTimeout:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=RUN_LIST_TIMEOUT_DETAIL)
    return JSONResponse(content={
        "items": [{**i, "run_id": str(i["run_id"]), "start": _iso(i["start"]), "end": _iso(i["end"])} for i in items],
        "next_before": next_before,
    })
