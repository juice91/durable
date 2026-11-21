"""Authenticated, tenant-agnostic platform tool catalog, and tool test runs.

`POST /tools/{tool_id}/runs` (the Studio tool test console) records a tenant-scoped run of kind
'tool' and enqueues `eap.tool_run` for it, per the run hand-off contract: the worker executes the
one tool call in a short durable workflow with the same allowlist and side-effect rules as agent
runs, and records the usual run events. The run is then read through GET /runs/{run_id}, its
/events and its /stream like any agent run.
"""


from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.agents import ToolCatalogDep, ToolRunServiceDep
from eap_api.services.tools import MAX_TOOL_INPUT_BYTES, safe_errors as _safe_errors  # noqa: F401 - re-exported

router = APIRouter(prefix="/tools", tags=["tools"], route_class=GuardedJsonRoute)


@router.get("")
def list_tools(
    principal: Annotated[Principal, Depends(current_principal)],
    service: ToolCatalogDep,
) -> list[dict]:
    """List platform tool declarations and their current allowlist state."""
    del principal
    return service.list()


@router.get("/{tool_id}")
def get_tool(
    tool_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
    service: ToolCatalogDep,
) -> dict:
    """Get one platform tool declaration."""
    del principal
    return service.get(tool_id)


class ToolRunCreate(BaseModel):
    """Body of POST /tools/{tool_id}/runs. `run_id` (optional, client-generated) makes a retry idempotent."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any]
    run_id: uuid.UUID | None = None


async def _tool_run_body(request: Request) -> ToolRunCreate:
    """Parse and validate the body here rather than through FastAPI's request validation, whose
    default 422 includes each rejected input (the global handler stays as it is for other routes).
    Malformed JSON, a wrong type and an extra field all answer 422 with loc/msg/type only."""
    raw = await request.body()
    try:
        return ToolRunCreate.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_safe_errors(exc, ["body"]))


@router.post(
    "/{tool_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail},
        503: {"model": ErrorDetail},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": ToolRunCreate.model_json_schema()}},
        }
    },
)
def start_tool_run(
    tool_id: str,
    principal: Annotated[Principal, Depends(current_principal)],  # before the body: 401 wins over 422
    body: Annotated[ToolRunCreate, Depends(_tool_run_body)],
    service: ToolRunServiceDep,
) -> JSONResponse:
    """Run one tool call as a durable, tenant-scoped run; 202 with the run id.

    404 unknown tool; 422 tool not in EAP_ALLOWED_TOOLS (the same status the platform uses for an
    agent or workflow that names a disallowed tool or model); 422 body or input rejected (FastAPI-
    style error list of loc/msg/type, never the rejected values); 409 `run_id` already used by a
    different request. A retry with the same `run_id` and input returns 202 again and re-enqueues
    (DBOS attaches it to the existing workflow).

    A failed hand-off answers 503. The run this request created (or re-queued) is then marked
    failed with ENQUEUE_FAILED_ERROR, if it is still queued, so it is not left queued with no
    workflow; a retry with the same run_id and input re-queues it and enqueues again. If DBOS did
    accept the enqueue that raised, the workflow runs anyway and its status replaces `failed`.
    """
    started = service.start_run(principal.tenant_id, tool_id, body.input, body.run_id)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=started)
