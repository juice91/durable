"""Workflow runs: start a run of a published workflow version, and decide its approval steps.

`POST /workflows/{workflow_id}/runs` records a tenant-scoped run of kind 'workflow' and enqueues
`eap.workflow_run` for it (run hand-off contract); the run is then read through GET /runs/{run_id},
its /events and its /stream like any run. Same 202/404/409/422/503 semantics as
POST /tools/{tool_id}/runs, with compact 422s that never echo the input.

`POST /workflows/runs/{run_id}/steps/{step_id}/approve|reject` records the decision of a waiting
approval step as the run's `step:{step_id}:decision` event (atomically: only a step that is waiting
gets 200, everything else 409), then wakes the waiting workflow with DBOS send. The decision is
durable before the wake-up is sent; if the send fails the answer is still 200 with
`"notified": false`, and the worker picks the decision up at its next poll
(EAP_APPROVAL_POLL_SECONDS, default one hour).
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.routes.runs import StoresDep
from eap_api.request_guard import guarded_json_route
from eap_api.routes.workflows import reference_problems
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_observability import current_trace_context, log_failure
from eap_run_store import ENQUEUE_FAILED_ERROR, RunIdConflict, RunNotFound
from eap_schemas.workflows import STEP_ID_PATTERN, WorkflowSpec

log = logging.getLogger("eap_api")

# Same bound as a tool run's input and an agent run's input text: the canonical JSON of the input.
MAX_WORKFLOW_INPUT_BYTES = 32 * 1024
# The body is {"input": ..., "version"?, "run_id"?} or {"comment": ...}: the input plus slack.
MAX_RUN_BODY_BYTES = 64 * 1024
MAX_RUN_BODY_DEPTH = 64
MAX_COMMENT_CHARS = 2000
MAX_REPORTED_ERRORS = 20
NOT_WAITING_DETAIL = "step is not waiting for approval"


# The shared JSON write-route guards (eap_api.request_guard): more than MAX_RUN_BODY_BYTES -> 413;
# not strict UTF-8 (or any NUL byte) -> 415; nested deeper than MAX_RUN_BODY_DEPTH -> 422;
# unstorable strings (U+0000, lone surrogates) -> 422; compact 422s.
WorkflowRunRoute = guarded_json_route(max_body_bytes=MAX_RUN_BODY_BYTES, max_depth=MAX_RUN_BODY_DEPTH)


router = APIRouter(prefix="/workflows", tags=["workflow runs"], route_class=WorkflowRunRoute)

SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]
LauncherDep = Annotated[RunLauncher, Depends(get_run_launcher)]


class WorkflowRunCreate(BaseModel):
    """Body of POST /workflows/{workflow_id}/runs. `version` (optional) pins a published version,
    else the latest; `run_id` (optional, client-generated) makes a retry idempotent."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any]
    version: int | None = Field(default=None, ge=1)
    run_id: uuid.UUID | None = None


class ApprovalDecision(BaseModel):
    """Body of .../approve and .../reject (optional; an empty body means no comment)."""

    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)


def _safe_errors(exc: ValidationError, prefix: list[Any]) -> list[dict[str, Any]]:
    """loc, msg and type only: never the rejected value or ctx (input can hold anything typed)."""
    return [
        {"loc": [*prefix, *err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False, include_context=False, include_input=False)[:MAX_REPORTED_ERRORS]
    ]


async def _parse_body(request: Request, model: type[BaseModel], *, allow_empty: bool = False) -> Any:
    raw = await request.body()
    if allow_empty and not raw.strip():
        raw = b"{}"
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_safe_errors(exc, ["body"]))


async def _run_body(request: Request) -> WorkflowRunCreate:
    return await _parse_body(request, WorkflowRunCreate)


async def _decision_body(request: Request) -> ApprovalDecision:
    return await _parse_body(request, ApprovalDecision, allow_empty=True)


def _canonical_json(value: Any) -> str:
    # allow_nan=False: NaN/Infinity are not JSON (JSONB rejects them); non_finite_errors runs first.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def non_finite_errors(value: Any) -> list[dict[str, Any]]:
    """One compact error per non-finite number (NaN, Infinity, -Infinity) anywhere in the input:
    the JSON parser accepts them and jsonschema treats NaN as a number, but they are not JSON and
    can never be stored. Iterative; the body depth is already capped."""
    errors: list[dict[str, Any]] = []
    stack: list[tuple[Any, list[Any]]] = [(value, ["body", "input"])]
    while stack and len(errors) < MAX_REPORTED_ERRORS:
        item, loc = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            errors.append({"loc": loc, "msg": "must be a finite number (NaN and Infinity are not JSON)",
                           "type": "finite_number"})
        elif isinstance(item, dict):
            stack.extend((v, [*loc, str(k)[:100]]) for k, v in item.items())
        elif isinstance(item, list):
            stack.extend((v, [*loc, i]) for i, v in enumerate(item))
    return errors


def input_schema_errors(schema: dict[str, Any], value: Any) -> list[dict[str, Any]]:
    """`value` checked against the version's input_schema with an EMPTY referencing.Registry (no
    remote $ref is ever retrieved). Errors name the input path and the failing JSON-schema keyword,
    never jsonschema's message (it quotes the rejected value). An invalid or unresolvable schema
    is one error at loc ["body", "input"]."""
    import jsonschema
    from referencing import Registry

    try:
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        errors = sorted(cls(schema, registry=Registry()).iter_errors(value), key=lambda e: list(e.absolute_path))
        found = []
        for err in errors[:MAX_REPORTED_ERRORS]:
            loc = ["body", "input", *[p if isinstance(p, int) else str(p)[:100] for p in err.absolute_path]]
            found.append({"loc": loc, "msg": f"does not satisfy the input schema's '{err.validator}' keyword",
                          "type": f"json_schema.{err.validator}"})
        return found
    except Exception:  # noqa: BLE001 - SchemaError, an unresolvable $ref, ...
        return [{"loc": ["body", "input"], "msg": "the workflow's input schema could not be applied",
                 "type": "json_schema.invalid_schema"}]


def _workflow_or_404(db: Session, workflow_id: uuid.UUID, tenant_id: str) -> WorkflowModel:
    workflow = (
        db.query(WorkflowModel)
        .filter(WorkflowModel.id == str(workflow_id), WorkflowModel.tenant_id == tenant_id)
        .first()
    )
    if workflow is None or workflow.archived:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return workflow


def _version_row(db: Session, workflow: WorkflowModel, version: int | None) -> WorkflowVersionModel:
    query = db.query(WorkflowVersionModel).filter(
        WorkflowVersionModel.workflow_id == workflow.id, WorkflowVersionModel.tenant_id == workflow.tenant_id
    )
    if version is None:
        latest = query.with_entities(func.max(WorkflowVersionModel.version)).scalar()
        if latest is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow has no published version")
        version = latest
    row = query.filter(WorkflowVersionModel.version == version).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return row


@router.post(
    "/{workflow_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail},
        413: {"model": ErrorDetail},
        415: {"model": ErrorDetail},
        503: {"model": ErrorDetail},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": WorkflowRunCreate.model_json_schema()}},
        }
    },
)
def start_workflow_run(
    workflow_id: uuid.UUID,
    principal: PrincipalDep,  # before the body: 401 wins over 422
    body: Annotated[WorkflowRunCreate, Depends(_run_body)],
    db: SessionDep,
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Run a published workflow version as a durable, tenant-scoped run; 202 with the run id.

    404 unknown (or another tenant's, or archived) workflow, or unknown `version`; 409 no published
    version; 422 body rejected, input not valid for the version's input_schema (loc/msg/type only,
    never the input), a non-finite number anywhere in the input (NaN, Infinity), input over 32 KiB as JSON, or the version points at something this tenant
    can no longer run (an agent gone, a tool no longer allowed); 409 `run_id` already used by a
    different request. A retry with the same `run_id`, input and version answers 202 again and
    re-enqueues (DBOS attaches it to the existing workflow). A failed hand-off answers 503 and marks
    the run failed with ENQUEUE_FAILED_ERROR (if still queued); the same retry re-queues it.
    """
    tenant_id = principal.tenant_id
    workflow = _workflow_or_404(db, workflow_id, tenant_id)
    version_row = _version_row(db, workflow, body.version)
    spec = WorkflowSpec.model_validate(version_row.spec)
    errors = non_finite_errors(body.input) or input_schema_errors(spec.input_schema, body.input)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors)
    input_json = _canonical_json(body.input)
    if len(input_json.encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"input must be at most {MAX_WORKFLOW_INPUT_BYTES // 1024} KiB as JSON",
        )
    problems = reference_problems(db, spec, tenant_id)
    if problems:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="; ".join(problems))

    run_store, _ = stores
    run_id = body.run_id or uuid.uuid4()
    trace_id, traceparent = current_trace_context()
    try:
        _, created = run_store.create_workflow_run(
            tenant_id, workflow.id, version_row.version, input_json, run_id, trace_id=trace_id, traceparent=traceparent
        )
    except RunIdConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
    run = run_store.get_run(tenant_id, run_id)
    if run is None:  # pragma: no cover - deleted between insert and read
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if not created and (
        run["kind"] != "workflow"
        or run["workflow_id"] != str(workflow.id)
        or run["input"] != input_json
        or (body.version is not None and run["workflow_version"] != body.version)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
        )
    if not created and run["workflow_version"] != version_row.version:
        # A retry without `version` after a newer publish: start exactly the recorded version.
        version_row = _version_row(db, workflow, run["workflow_version"])
    owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)

    try:
        launcher.launch_workflow(
            run_id=run_id,
            tenant_id=tenant_id,
            workflow_version_id=str(version_row.id),
            input_json=run["input"],
            traceparent=run.get("traceparent"),
        )
    except Exception as exc:
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
        run_status = display_status = "queued"
    log.info("workflow run %s queued: tenant=%s workflow=%s", run_id, tenant_id, workflow.id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "run_id": str(run["id"]),
            "kind": "workflow",
            "workflow_id": run["workflow_id"],
            "version": run["workflow_version"],
            "status": run_status,
            "display_status": display_status,
            "trace_id": run["trace_id"],
        },
    )


def _decide(
    decision: str,
    run_id: uuid.UUID,
    step_id: str,
    principal: Principal,
    body: ApprovalDecision,
    stores: Any,
    launcher: RunLauncher,
) -> JSONResponse:
    run_store, event_log = stores
    tenant_id = principal.tenant_id
    run = run_store.get_run(tenant_id, run_id)
    if run is None or run.get("kind") != "workflow":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    event_type = "step_approved" if decision == "approved" else "step_rejected"
    data = {"step_id": step_id, "decided_by": principal.user_id, "comment": body.comment}
    try:
        outcome, _event = event_log.append_step_decision(tenant_id, run_id, step_id, event_type, data)
    except RunNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if outcome != "created":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_WAITING_DETAIL)
    waiting = event_log.get_event(tenant_id, run_id, f"step:{step_id}:waiting")
    target = str((waiting.data if waiting is not None else {}).get("workflow_id") or run_id)
    notified = True
    try:
        launcher.notify_approval(workflow_id=target, step_id=step_id, decision=decision)
    except Exception as exc:  # noqa: BLE001 - the decision is durable; the worker polls for it
        notified = False
        log_failure(
            log, "approval wake-up failed", exc, code="approval_notify_failed", level=logging.ERROR,
            run_id=str(run_id), tenant_id=tenant_id,
        )
    log.info("workflow run %s step %s %s by %s", run_id, step_id, decision, principal.user_id)
    return JSONResponse(
        content={
            "run_id": str(run_id),
            "step_id": step_id,
            "decision": decision,
            "decided_by": principal.user_id,
            "comment": body.comment,
            "notified": notified,
        }
    )


# A step id as the spec allows it (^[a-z][a-z0-9_]{0,63}$), checked before any query: anything else
# (a NUL, a lone surrogate, uppercase, too long) is a compact 422 at loc ["path", "step_id"] and
# never reaches an event key.
StepIdPath = Annotated[str, Path(pattern=STEP_ID_PATTERN.pattern, max_length=64)]

_DECISION_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
}


@router.post("/runs/{run_id}/steps/{step_id}/approve", responses=_DECISION_RESPONSES)
def approve_step(
    run_id: uuid.UUID,
    step_id: StepIdPath,
    principal: PrincipalDep,
    body: Annotated[ApprovalDecision, Depends(_decision_body)],
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Approve a waiting approval step: 200 {run_id, step_id, decision, decided_by, comment,
    notified}; 409 when the step is not waiting (never reached, already decided, timed out, run
    finished); 404 unknown, another tenant's or non-workflow run."""
    return _decide("approved", run_id, step_id, principal, body, stores, launcher)


@router.post("/runs/{run_id}/steps/{step_id}/reject", responses=_DECISION_RESPONSES)
def reject_step(
    run_id: uuid.UUID,
    step_id: StepIdPath,
    principal: PrincipalDep,
    body: Annotated[ApprovalDecision, Depends(_decision_body)],
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Reject a waiting approval step (the run then fails with `approval_rejected`); same answers
    as approve."""
    return _decide("rejected", run_id, step_id, principal, body, stores, launcher)
