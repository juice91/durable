"""Authenticated, tenant-agnostic platform tool catalog, and tool test runs.

`POST /tools/{tool_id}/runs` (the Studio tool test console) records a tenant-scoped run of kind
'tool' and enqueues `eap.tool_run` for it, per the run hand-off contract: the worker executes the
one tool call in a short durable workflow with the same allowlist and side-effect rules as agent
runs, and records the usual run events. The run is then read through GET /runs/{run_id}, its
/events and its /stream like any agent run.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from eap_api.auth import Principal, current_principal
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import StoresDep
from eap_api.schemas import ErrorDetail
from eap_observability import current_trace_context, log_failure
from eap_run_store import ENQUEUE_FAILED_ERROR, RunIdConflict
from eap_schemas.tools import TOOL_DECLARATIONS, ToolDeclaration

log = logging.getLogger("eap_api")

# Same bound as an agent run's input text (RunCreate): the canonical JSON of the tool input.
MAX_TOOL_INPUT_BYTES = 32 * 1024

router = APIRouter(prefix="/tools", tags=["tools"], route_class=GuardedJsonRoute)


def _allowed_tool_ids() -> frozenset[str] | None:
    """Parse the same optional platform allowlist used by the worker."""
    configured = os.environ.get("EAP_ALLOWED_TOOLS")
    if configured is None:
        return None
    return frozenset(tool_id.strip() for tool_id in configured.split(",") if tool_id.strip())


def _tool_response(declaration: ToolDeclaration, allowed_tools: frozenset[str] | None) -> dict:
    """Render a declaration with the current platform policy view."""
    return {
        "id": declaration.id,
        "title": declaration.title,
        "description": declaration.description,
        "side_effect": declaration.side_effect,
        "external_idempotent": declaration.external_idempotent,
        "input_schema": declaration.input_json_schema(),
        "allowed": allowed_tools is None or declaration.id in allowed_tools,
    }


@router.get("")
def list_tools(
    principal: Annotated[Principal, Depends(current_principal)],
) -> list[dict]:
    """List platform tool declarations and their current allowlist state."""
    del principal
    allowed_tools = _allowed_tool_ids()
    return [_tool_response(declaration, allowed_tools) for declaration in TOOL_DECLARATIONS.values()]


@router.get("/{tool_id}")
def get_tool(
    tool_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """Get one platform tool declaration."""
    del principal
    declaration = TOOL_DECLARATIONS.get(tool_id)
    if declaration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    return _tool_response(declaration, _allowed_tool_ids())


class ToolRunCreate(BaseModel):
    """Body of POST /tools/{tool_id}/runs. `run_id` (optional, client-generated) makes a retry idempotent."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any]
    run_id: uuid.UUID | None = None


def _safe_errors(exc: ValidationError, prefix: list[Any]) -> list[dict[str, Any]]:
    """FastAPI's error shape (loc, msg, type) without the rejected values or ctx: tool input can be
    anything a user typed, including secrets, and must never be echoed back."""
    return [
        {"loc": [*prefix, *err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


async def _tool_run_body(request: Request) -> ToolRunCreate:
    """Parse and validate the body here rather than through FastAPI's request validation, whose
    default 422 includes each rejected input (the global handler stays as it is for other routes).
    Malformed JSON, a wrong type and an extra field all answer 422 with loc/msg/type only."""
    raw = await request.body()
    try:
        return ToolRunCreate.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_safe_errors(exc, ["body"]))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validated_input(declaration: ToolDeclaration, raw: dict[str, Any]) -> dict[str, Any]:
    """The input as the tool's Pydantic input model accepts it (JSON mode), or a 422 with its errors."""
    try:
        validated = declaration.input_model.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_safe_errors(exc, ["body", "input"])
        )
    return validated.model_dump(mode="json")


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
    stores: StoresDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)],
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
    declaration = TOOL_DECLARATIONS.get(tool_id)
    if declaration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    allowed_tools = _allowed_tool_ids()
    if allowed_tools is not None and tool_id not in allowed_tools:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"tool '{tool_id}' is not allowed on this platform (not in the EAP_ALLOWED_TOOLS allowlist)",
        )
    input_json = _canonical_json(_validated_input(declaration, body.input))
    if len(input_json.encode("utf-8")) > MAX_TOOL_INPUT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"input must be at most {MAX_TOOL_INPUT_BYTES // 1024} KiB as JSON",
        )

    run_store, _ = stores
    tenant_id = principal.tenant_id
    run_id = body.run_id or uuid.uuid4()
    trace_id, traceparent = current_trace_context()
    try:
        _, created = run_store.create_tool_run(
            tenant_id, tool_id, input_json, run_id, trace_id=trace_id, traceparent=traceparent
        )
    except RunIdConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
    run = run_store.get_run(tenant_id, run_id)
    if run is None:  # pragma: no cover - deleted between insert and read
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if not created and (run["kind"] != "tool" or run["tool_id"] != tool_id or run["input"] != input_json):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
        )
    # This request owns the hand-off of a row it created, or of one an earlier failed hand-off left
    # failed and it re-queues now; it never touches a run some workflow already drives.
    owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)

    try:
        # Exactly what the stored row says, so a retry starts the recorded run.
        launcher.launch_tool(
            run_id=run_id,
            tenant_id=tenant_id,
            tool_id=run["tool_id"],
            input_json=run["input"],
            traceparent=run.get("traceparent"),
        )
    except Exception as exc:
        # Type and code only: the error can quote the DBOS database URL or the enqueued payload.
        log_failure(
            log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
            run_id=str(run_id), tenant_id=tenant_id,
        )
        if owns_handoff:
            try:
                run_store.mark_enqueue_failed(tenant_id, run_id)
            except Exception as mark_exc:  # noqa: BLE001 - the 503 below is the answer either way
                log_failure(
                    log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                    level=logging.ERROR, run_id=str(run_id), tenant_id=tenant_id,
                )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Run recorded but could not be started; retry the same request (same run_id)",
        )
    run = run_store.get_run(tenant_id, run_id) or run
    run_status, display_status = run["status"], run["display_status"]
    if run["error"] == ENQUEUE_FAILED_ERROR:
        # A concurrent request that owned the hand-off failed and marked the run failed, but this
        # enqueue succeeded: the workflow exists, and its start step clears that error.
        run_status = display_status = "queued"
    log.info("tool run %s queued: tenant=%s tool=%s", run_id, tenant_id, tool_id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "run_id": str(run["id"]),
            "kind": "tool",
            "tool_id": run["tool_id"],
            "status": run_status,
            "display_status": display_status,
            "trace_id": run["trace_id"],
        },
    )
