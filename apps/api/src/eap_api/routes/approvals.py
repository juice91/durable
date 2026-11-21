"""The approvals inbox and the tool-approval decisions (agent capabilities milestone, AC.8).

`GET /approvals` lists the tenant's approvals of both kinds from the pending_approvals projection
(0014_approvals): `tool` (an agent run's gated tool call) and `step` (a workflow approval step),
pending (newest request first) or decided (newest decision first), with `kind` and `agent_id`
filters and an opaque cursor.

`GET /approvals/{run_id}/{kind}/{key}` reads one item of either list (the approval Sheet's shared
link), in the same shape, whatever its age; 404 when unknown or another tenant's.

`POST /runs/{run_id}/tool-approvals/{tool_call_id}/approve|deny {comment?}` records the decision of
a gated tool call as the run's `tool:{tool_call_id}:approval:decision` event (tool_approved |
tool_denied), atomically under the run-row lock and only while the call waits, with the inbox row
closed in the same transaction; then wakes the run (DBOS send on `tool_approval:{run_id}`). The
decision event is the source of truth: when the wake-up fails the answer is still 200 with
`"notified": false`, and the run picks the decision up at its next poll. Workflow steps are
decided by POST /workflows/runs/{run_id}/steps/{step_id}/approve|reject (routes/workflow_runs.py);
both go through eap_api.approvals.decide.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import guarded_json_route
from eap_api.routes.workflow_runs import _parse_body
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.approvals import ApprovalServiceDep
from eap_api.services.approvals import (  # noqa: F401 - re-exported: the API's users and tests import them from here
    ALREADY_DECIDED_DETAIL,
    APPROVAL_NOT_FOUND_DETAIL,
    INVALID_CURSOR_DETAIL,
    MAX_APPROVAL_CURSOR_CHARS,
    MAX_APPROVAL_LIMIT,
    NOT_WAITING_DETAIL,
    RUN_NOT_FOUND_DETAIL,
    TOOL_CALL_NOT_FOUND_DETAIL,
    approval_json,
)

# The comment a person may add to a tool decision; a denial's comment reaches the model.
MAX_TOOL_COMMENT_CHARS = 500
# The body is {"comment"?}: 500 characters of at most 4 UTF-8 bytes each, plus slack.
MAX_DECISION_BODY_BYTES = 8 * 1024
MAX_DECISION_BODY_DEPTH = 8
# A tool call id is chosen by the model provider (`call_…`, `toolu_…`, `pyd_ai_…`): printable ASCII
# without spaces, at most 256 characters, checked before any query or event key.
TOOL_CALL_ID_PATTERN = r"^[\x21-\x7e]+$"
MAX_TOOL_CALL_ID_CHARS = 256

ApprovalRoute = guarded_json_route(max_body_bytes=MAX_DECISION_BODY_BYTES, max_depth=MAX_DECISION_BODY_DEPTH)

router = APIRouter(tags=["approvals"], route_class=ApprovalRoute)

PrincipalDep = Annotated[Principal, Depends(current_principal)]


class ToolApprovalDecision(BaseModel):
    """Body of .../tool-approvals/{tool_call_id}/approve|deny (optional; empty means no comment)."""

    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=MAX_TOOL_COMMENT_CHARS)


async def _decision_body(request: Request) -> ToolApprovalDecision:
    return await _parse_body(request, ToolApprovalDecision, allow_empty=True)


@router.get("/approvals", responses={422: {"model": ErrorDetail}})
def list_approvals(
    principal: PrincipalDep,
    service: ApprovalServiceDep,
    approval_status: Annotated[Literal["pending", "decided"], Query(alias="status")] = "pending",
    kind: Annotated[Literal["tool", "step"] | None, Query()] = None,
    agent_id: uuid.UUID | None = None,
    cursor: Annotated[
        str | None,
        Query(max_length=MAX_APPROVAL_CURSOR_CHARS, description="Opaque: the previous page's next_cursor"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_APPROVAL_LIMIT)] = 50,
) -> JSONResponse:
    """The tenant's approvals: {items, next_cursor}. `status=pending` (default): open approvals of
    runs that have not ended, newest request first; `status=decided`: newest decision first
    (decision approved | denied | timed_out; a rejected workflow step is `denied`). `kind`
    (tool | step) and `agent_id` filter. `next_cursor` is null on the last page; a cursor this API
    did not issue is a 422."""
    page = service.list(
        principal.tenant_id,
        status=approval_status,
        kind=kind,
        agent_id=str(agent_id) if agent_id is not None else None,
        cursor=cursor,
        limit=limit,
    )
    return JSONResponse(content=page)


@router.get("/approvals/{run_id}/{kind}/{key}", responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}})
def get_approval(
    run_id: uuid.UUID,
    kind: Annotated[Literal["tool", "step"], Path()],
    key: Annotated[str, Path(min_length=1, max_length=MAX_TOOL_CALL_ID_CHARS, pattern=TOOL_CALL_ID_PATTERN)],
    principal: PrincipalDep,
    service: ApprovalServiceDep,
) -> JSONResponse:
    """One approval, pending or decided, in the inbox item's shape. `key` is the tool call id (kind
    tool) or the step id (kind step). A pending item of a run that has ended keeps `status:
    "pending"` with the run's terminal `run_status` (it can no longer be decided). 404 unknown, or
    another tenant's; 422 a malformed run id, kind or key. A workflow step also has `resume_schema`
    (set for an input step), and `comment` and `resume_data` once decided (the audit of the
    decision, with `decided_by` and `decided_at`)."""
    return JSONResponse(content=service.get(principal.tenant_id, run_id, kind, key))


ToolCallIdPath = Annotated[
    str, Path(min_length=1, max_length=MAX_TOOL_CALL_ID_CHARS, pattern=TOOL_CALL_ID_PATTERN)
]

_DECISION_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    413: {"model": ErrorDetail},
    415: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
}


_DECISION_BODY_DOC = {
    "requestBody": {
        "required": False,
        "content": {"application/json": {"schema": ToolApprovalDecision.model_json_schema()}},
    }
}


@router.post(
    "/runs/{run_id}/tool-approvals/{tool_call_id}/approve",
    responses=_DECISION_RESPONSES,
    openapi_extra=_DECISION_BODY_DOC,
)
def approve_tool_call(
    run_id: uuid.UUID,
    tool_call_id: ToolCallIdPath,
    principal: PrincipalDep,  # before the body: 401 wins over 422
    body: Annotated[ToolApprovalDecision, Depends(_decision_body)],
    service: ApprovalServiceDep,
) -> JSONResponse:
    """Approve a gated tool call the run waits on: 200 {run_id, tool_call_id, decision:
    "approved", decided_by, comment, notified}. 404 unknown (or another tenant's) run, or a call
    that never waited for approval; 409 already decided (by a person or the timeout) or no longer
    waiting (the run ended); 422 a comment over 500 characters or a malformed tool call id. The
    "already decided" 409 also gives the recorded `decision` (approved | denied | timed_out),
    `decided_by` (null for a timeout) and `decided_at`."""
    result = service.decide_tool(principal.tenant_id, principal.user_id, run_id, tool_call_id, "approved", body.comment)
    return JSONResponse(content=result)


@router.post(
    "/runs/{run_id}/tool-approvals/{tool_call_id}/deny",
    responses=_DECISION_RESPONSES,
    openapi_extra=_DECISION_BODY_DOC,
)
def deny_tool_call(
    run_id: uuid.UUID,
    tool_call_id: ToolCallIdPath,
    principal: PrincipalDep,
    body: Annotated[ToolApprovalDecision, Depends(_decision_body)],
    service: ApprovalServiceDep,
) -> JSONResponse:
    """Deny a gated tool call: the tool never runs, the model sees the denial (with the comment)
    and the run continues. Same answers as approve, with decision "denied"."""
    result = service.decide_tool(principal.tenant_id, principal.user_id, run_id, tool_call_id, "denied", body.comment)
    return JSONResponse(content=result)
