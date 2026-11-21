"""`GET /spans`: the flat, materialised span list behind the Traces page's Spans tab.

Rows are written by the worker when a top-level agent, tool or workflow run finishes (eap_worker
spans_step, from the same derivation as `GET /runs/{id}/trace`); runs finished before that have none.
Newest first by `(start, span_id)`, cursor-paged, tenant-scoped.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.runs import SpanServiceDep
from eap_api.services.runs import MAX_CURSOR_LENGTH
from eap_api.services.spans import SPAN_KINDS

router = APIRouter(tags=["spans"], responses={422: {"model": ErrorDetail}}, route_class=GuardedJsonRoute)
PrincipalDep = Annotated[Principal, Depends(current_principal)]


@router.get("/spans")
def list_spans(
    principal: PrincipalDep,
    service: SpanServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[
        str | None,
        Query(max_length=MAX_CURSOR_LENGTH, description="Opaque cursor: the previous page's next_before, verbatim"),
    ] = None,
    span_kind: Annotated[Literal[*SPAN_KINDS] | None, Query()] = None,
    source: Annotated[Literal["platform", "external"] | None, Query(description="Only the platform's own spans or only external OTLP spans; absent = both")] = None,
    agent_id: uuid.UUID | None = None,
    run_status: Annotated[list[str] | None, Query(alias="status")] = None,
    kind: Annotated[Literal["agent", "tool", "workflow", "kb_ingest", "eval", "kb_source_sync"] | None, Query()] = None,
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
    model, source, trace_id, service_name}], next_before}`; `source` is `platform` or `external` (spans
    sent over OTLP, see POST /otlp/v1/traces; their `trace_id` is the client's and their `run_id`
    a derived id no run has), newest span first. `from` / `to` bound the span's `start` to `[from, to)`;
    `span_kind` is the span's kind (run, model_request, tool_call, ...); `q` matches the span name
    (case-insensitive). The other filters are `GET /runs`' and apply to the span's run: `status`
    (any of, at most 10), `kind` (the run's kind), `tool_id`, `workflow_id`, `tags`, `metadata=key=value`,
    and `agent_id` / `model` / `provider` (the run used that model; `model` and `provider` run under the
    metrics statement timeout, 503 when it fires). `next_before` is the opaque cursor when a full
    page came back (pass it back verbatim), else null; one this API did not issue is a 422."""
    return JSONResponse(content=service.list_spans(
        principal.tenant_id, limit=limit, before=before, span_kind=span_kind, source=source, agent_id=agent_id,
        run_status=run_status, kind=kind, tool_id=tool_id, workflow_id=workflow_id, created_from=created_from,
        created_to=created_to, model=model, provider=provider, tags=tags, metadata=metadata, q=q,
    ))
