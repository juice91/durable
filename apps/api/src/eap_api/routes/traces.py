"""Built-in trace view data (P0.7): `GET /runs/{run_id}/trace`.

The trace is derived from the run's durable events and usage rows (eap_run_store.trace): one span
per run, model request, tool call, processor result and workflow step, with a workflow's child
agent runs nested under their step, plus the RAG and memory spans of RM.19 (retrieve, embedding,
memory_recall, memory_index, observer, reflector, working_memory, ingest). Tenant-scoped like every run route: an unknown run, or another
tenant's, is a 404 (its child runs are read under the caller's tenant only). The DBOS and HTTP
spans stay in Grafana (the run's `trace_id`).

Response: `{run, spans, totals, truncated, limits}`; `run` is the GET /runs/{id} shape. See
eap_run_store.trace for the span shape, ids, statuses, timing and the payload bounds.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.routes.runs import StoresDep, _run_json
from eap_api.models import AgentVersionModel
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowVersionModel
from eap_run_store.trace import TraceQueryTimeout, TraceReader

TIMEOUT_DETAIL = "Trace query timed out; retry later"

router = APIRouter(tags=["traces"], responses={404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}})

SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]


@router.get("/runs/{run_id}/trace")
def get_run_trace(run_id: uuid.UUID, principal: PrincipalDep, stores: StoresDep, db: SessionDep) -> JSONResponse:
    """The run's span tree with tokens and cost. 404 for an unknown run or another tenant's; 503
    when reading it hits the statement timeout."""
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
    return JSONResponse(content=trace)
