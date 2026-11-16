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
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eap_api.approvals import decide
from eap_api.auth import Principal, current_principal
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.request_guard import guarded_json_route
from eap_api.routes.runs import StoresDep
from eap_api.routes.workflow_runs import _parse_body
from eap_api.schemas import ErrorDetail
from eap_run_store import ApprovalStore, InvalidRunCursor
from eap_run_store.cursor import MAX_CURSOR_LENGTH

# The comment a person may add to a tool decision; a denial's comment reaches the model.
MAX_TOOL_COMMENT_CHARS = 500
# The body is {"comment"?}: 500 characters of at most 4 UTF-8 bytes each, plus slack.
MAX_DECISION_BODY_BYTES = 8 * 1024
MAX_DECISION_BODY_DEPTH = 8
# A tool call id is chosen by the model provider (`call_…`, `toolu_…`, `pyd_ai_…`): printable ASCII
# without spaces, at most 256 characters, checked before any query or event key.
TOOL_CALL_ID_PATTERN = r"^[\x21-\x7e]+$"
MAX_TOOL_CALL_ID_CHARS = 256
# An inbox cursor carries a provider-chosen tool call id, so it may run longer than a run cursor
# (eap_run_store.approvals.ApprovalCursor.decode accepts up to 4x).
MAX_APPROVAL_CURSOR_CHARS = 4 * MAX_CURSOR_LENGTH

RUN_NOT_FOUND_DETAIL = "Run not found"
TOOL_CALL_NOT_FOUND_DETAIL = "Tool call not found"
ALREADY_DECIDED_DETAIL = "tool call is already decided"
NOT_WAITING_DETAIL = "tool call is not waiting for approval"
APPROVAL_NOT_FOUND_DETAIL = "Approval not found"
INVALID_CURSOR_DETAIL = "cursor is not valid; pass the previous page's next_cursor verbatim"

ApprovalRoute = guarded_json_route(max_body_bytes=MAX_DECISION_BODY_BYTES, max_depth=MAX_DECISION_BODY_DEPTH)

router = APIRouter(tags=["approvals"], route_class=ApprovalRoute)

PrincipalDep = Annotated[Principal, Depends(current_principal)]
LauncherDep = Annotated[RunLauncher, Depends(get_run_launcher)]


def get_approval_store(request: Request, stores: StoresDep) -> ApprovalStore:
    """The ApprovalStore on the run stores' engine, made once per app."""
    store = getattr(request.app.state, "approval_store", None)
    if store is None:
        run_store, _ = stores
        store = request.app.state.approval_store = ApprovalStore(run_store.engine)
    return store


ApprovalStoreDep = Annotated[ApprovalStore, Depends(get_approval_store)]


class ToolApprovalDecision(BaseModel):
    """Body of .../tool-approvals/{tool_call_id}/approve|deny (optional; empty means no comment)."""

    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=MAX_TOOL_COMMENT_CHARS)


async def _decision_body(request: Request) -> ToolApprovalDecision:
    return await _parse_body(request, ToolApprovalDecision, allow_empty=True)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def approval_json(row: dict[str, Any]) -> dict[str, Any]:
    """One inbox item. `approval_key` is the tool call id (kind tool) or the step id (kind step),
    also given as `tool_call_id` / `step_id`. `root_run_id` is the workflow run for a workflow
    agent step's child run, else the run itself."""
    tool = row["kind"] == "tool"
    return {
        "run_id": str(row["run_id"]),
        "kind": row["kind"],
        "approval_key": row["approval_key"],
        "tool_call_id": row["approval_key"] if tool else None,
        "step_id": None if tool else row["approval_key"],
        "root_run_id": str(row["root_run_id"]),
        "agent_id": row["agent_id"],
        "workflow_id": row["workflow_id"],
        "tool_name": row["tool_name"],
        "args": row["args"],
        "prompt": row["prompt"],
        "status": row["status"],
        "requested_at": _iso(row["requested_at"]),
        "deadline": _iso(row["deadline"]),
        "decided_at": _iso(row["decided_at"]),
        "decision": row["decision"],
        "decided_by": row["decided_by"],
        "run_status": row["run_status"],
    }


def _step_extras(event_log: Any, tenant_id: str, row: dict[str, Any], *, detail: bool) -> dict[str, Any]:
    """What a workflow step's item adds from its events: `resume_schema` (an input step: the
    schema the person fills in; every item), and for a decided one (`detail`, the single read) the
    audit of the decision: `comment` and `resume_data` (what was submitted) beside who and when."""
    if row["kind"] != "step":
        return {}
    waiting = event_log.get_event(tenant_id, row["run_id"], f"step:{row['approval_key']}:waiting")
    schema = (waiting.data or {}).get("resume_schema") if waiting is not None and isinstance(waiting.data, dict) else None
    extras: dict[str, Any] = {"resume_schema": schema if isinstance(schema, dict) else None}
    if detail:
        decision = event_log.get_event(tenant_id, row["run_id"], f"step:{row['approval_key']}:decision")
        data = decision.data if decision is not None and isinstance(decision.data, dict) else {}
        extras["comment"] = data.get("comment")
        extras["resume_data"] = data.get("resume_data")
    return extras


@router.get("/approvals", responses={422: {"model": ErrorDetail}})
def list_approvals(
    principal: PrincipalDep,
    store: ApprovalStoreDep,
    stores: StoresDep,
    approval_status: Annotated[Literal["pending", "decided"], Query(alias="status")] = "pending",
    kind: Annotated[Literal["tool", "step"] | None, Query()] = None,
    agent_id: uuid.UUID | None = None,
    cursor: Annotated[
        str | None,
        Query(max_length=MAX_APPROVAL_CURSOR_CHARS, description="Opaque: the previous page's next_cursor"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=ApprovalStore.MAX_LIMIT)] = 50,
) -> JSONResponse:
    """The tenant's approvals: {items, next_cursor}. `status=pending` (default): open approvals of
    runs that have not ended, newest request first; `status=decided`: newest decision first
    (decision approved | denied | timed_out; a rejected workflow step is `denied`). `kind`
    (tool | step) and `agent_id` filter. `next_cursor` is null on the last page; a cursor this API
    did not issue is a 422."""
    try:
        rows, next_cursor = store.list(
            principal.tenant_id,
            status=approval_status,
            kind=kind,
            agent_id=str(agent_id) if agent_id is not None else None,
            cursor=cursor,
            limit=limit,
        )
    except InvalidRunCursor:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=INVALID_CURSOR_DETAIL)
    _, event_log = stores
    items = [{**approval_json(r), **_step_extras(event_log, principal.tenant_id, r, detail=True)} for r in rows]
    return JSONResponse(content={"items": items, "next_cursor": next_cursor})


@router.get("/approvals/{run_id}/{kind}/{key}", responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}})
def get_approval(
    run_id: uuid.UUID,
    kind: Annotated[Literal["tool", "step"], Path()],
    key: Annotated[str, Path(min_length=1, max_length=MAX_TOOL_CALL_ID_CHARS, pattern=TOOL_CALL_ID_PATTERN)],
    principal: PrincipalDep,
    store: ApprovalStoreDep,
    stores: StoresDep,
) -> JSONResponse:
    """One approval, pending or decided, in the inbox item's shape. `key` is the tool call id (kind
    tool) or the step id (kind step). A pending item of a run that has ended keeps `status:
    "pending"` with the run's terminal `run_status` (it can no longer be decided). 404 unknown, or
    another tenant's; 422 a malformed run id, kind or key. A workflow step also has `resume_schema`
    (set for an input step), and `comment` and `resume_data` once decided (the audit of the
    decision, with `decided_by` and `decided_at`)."""
    row = store.get(principal.tenant_id, str(run_id), kind, key)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=APPROVAL_NOT_FOUND_DETAIL)
    _, event_log = stores
    return JSONResponse(content={**approval_json(row), **_step_extras(event_log, principal.tenant_id, row, detail=True)})


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


def _decide_tool(
    decision: Literal["approved", "denied"],
    run_id: uuid.UUID,
    tool_call_id: str,
    principal: Principal,
    body: ToolApprovalDecision,
    stores: Any,
    launcher: RunLauncher,
) -> JSONResponse:
    _, event_log = stores
    result = decide(
        event_log, launcher, tenant_id=principal.tenant_id, run_id=run_id, kind="tool", key=tool_call_id,
        decision=decision, decided_by=principal.user_id, comment=body.comment,
    )
    if result.outcome == "run_not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=RUN_NOT_FOUND_DETAIL)
    if result.outcome == "exists":
        # Who got there first: a person ({decision, decided_by}) or the timeout (decision
        # "timed_out", decided_by null), and when.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": ALREADY_DECIDED_DETAIL, **(result.recorded_decision() or {})},
        )
    if result.outcome == "not_waiting":
        if not result.waiting:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=TOOL_CALL_NOT_FOUND_DETAIL)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_WAITING_DETAIL)
    return JSONResponse(
        content={
            "run_id": str(run_id),
            "tool_call_id": tool_call_id,
            "decision": decision,
            "decided_by": principal.user_id,
            "comment": body.comment,
            "notified": result.notified,
        }
    )


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
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Approve a gated tool call the run waits on: 200 {run_id, tool_call_id, decision:
    "approved", decided_by, comment, notified}. 404 unknown (or another tenant's) run, or a call
    that never waited for approval; 409 already decided (by a person or the timeout) or no longer
    waiting (the run ended); 422 a comment over 500 characters or a malformed tool call id. The
    "already decided" 409 also gives the recorded `decision` (approved | denied | timed_out),
    `decided_by` (null for a timeout) and `decided_at`."""
    return _decide_tool("approved", run_id, tool_call_id, principal, body, stores, launcher)


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
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Deny a gated tool call: the tool never runs, the model sees the denial (with the comment)
    and the run continues. Same answers as approve, with decision "denied"."""
    return _decide_tool("denied", run_id, tool_call_id, principal, body, stores, launcher)
