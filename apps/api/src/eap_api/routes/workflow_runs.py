"""Workflow runs: start a run of a published workflow version, and decide its approval steps.

`POST /workflows/{workflow_id}/runs` records a tenant-scoped run of kind 'workflow' and enqueues
`eap.workflow_run` for it (run hand-off contract); the run is then read through GET /runs/{run_id},
its /events and its /stream like any run. Same 202/404/409/422/503 semantics as
POST /tools/{tool_id}/runs, with compact 422s that never echo the input.

`POST /workflows/runs/{run_id}/steps/{step_id}/approve|reject` records the decision of a waiting
approval step as the run's `step:{step_id}:decision` event (atomically: only a step that is waiting
gets 200, everything else 409; the inbox row in pending_approvals is closed in the same
transaction), through the shared helper eap_api.approvals.decide, then wakes the waiting workflow with DBOS send. The decision is
durable before the wake-up is sent; if the send fails the answer is still 200 with
`"notified": false`, and the worker picks the decision up at its next poll
(EAP_APPROVAL_POLL_SECONDS, default one hour).

`POST /workflows/runs/{run_id}/steps/{step_id}/resume {data, comment?}` answers an input step (a
step with a `resume_schema`): `data` is validated against the schema recorded in the step's waiting
event (422 with loc/msg/type only, never the values), then recorded as the decision event
`step:{step_id}:decision` (step_approved, data {decided_by, comment, resume_data}; the event's time
is when) through the same `decide` helper, so the audit (who, when, what) is the event, tenant-scoped
with the run, and a second answer is 409. `approve` on an input step is 422 `resume_data_required`.

`POST /runs/{run_id}/cancel` (loops D12, LP.8; `cancel_router`, mounted without the /workflows
prefix) cancels a workflow run and its whole tree in three steps, none of them holding a lock
across another:
1. the durable request: `cancel_requested` (key `cancel`, `{requested_by}`) in its own short
   transaction; an existing `cancel` is reused, and a run that already has its `end` (and no
   `cancel`) is 409;
2. the cascade: `RunLauncher.cancel_workflow_tree` (DBOSClient.cancel_workflow(...,
   cancel_children=True)), with no eap_app transaction open, because a worker's event append
   takes the run-row lock (m7);
3. the guarded terminal write: `RunStore.cancel_run_tree` (an `end` run_failed `run_cancelled`
   for every run of the tree that has none, the status from `end`, the open approvals closed).
A failure after step 1 answers 503 and leaves the durable request: the scheduled
`eap.cancel_reconcile` (worker) re-runs steps 2 and 3 for it, and a retried POST does so at once.
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

from eap_api.agent_archive import archived_step_problems
from eap_api.approvals import DecisionResult, decide
from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.routes.runs import StoresDep
from eap_api.request_guard import guarded_json_route
from eap_api.routes.workflows import reference_problems
from eap_api.schemas import CancelNotSupportedDetail, ErrorDetail, RunCancelResponse
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_observability import current_trace_context, log_failure
from eap_run_store import CANCEL_EVENT_KEY, ENQUEUE_FAILED_ERROR, RunIdConflict
from eap_run_store.store import TERMINAL_RUN_STATUSES
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
    version, or an agent step's agent is archived (naming the step); 422 body rejected, input not valid for the version's input_schema (loc/msg/type only,
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
    run_store, _ = stores
    # A retry of a recorded run (same run_id) gets the normal retry handling below (compared with the
    # row, then re-enqueued): the run already committed to its version, and the worker reports a step
    # whose agent was archived since as `agent_archived`. Only a new run is checked up front.
    recorded = body.run_id is not None and run_store.get_run(tenant_id, body.run_id) is not None
    if not recorded:
        archived = archived_step_problems(db, tenant_id, spec.steps)
        if archived:
            # Before anything runs: an archived agent step would fail the run mid-way, after the
            # earlier steps' side effects (eap_api.agent_archive).
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="; ".join(archived))
        problems = reference_problems(db, spec, tenant_id)
        if problems:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="; ".join(problems))

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


class ResumeSubmission(BaseModel):
    """Body of .../resume: the object the input step's `resume_schema` describes."""

    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any]
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)


async def _resume_body(request: Request) -> ResumeSubmission:
    return await _parse_body(request, ResumeSubmission)


def resume_data_errors(schema: dict[str, Any], value: Any) -> list[dict[str, Any]]:
    """`value` checked against an input step's resume_schema, as `input_schema_errors` does (an empty
    Registry; loc/keyword only, never jsonschema's message, which quotes the rejected value), at
    loc ["body", "data", ...]. Non-finite numbers are errors too."""
    import jsonschema
    from referencing import Registry

    found = [{**e, "loc": ["body", "data", *e["loc"][2:]]} for e in non_finite_errors(value)]
    try:
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        for err in sorted(cls(schema, registry=Registry()).iter_errors(value), key=lambda e: list(e.absolute_path))[
            :MAX_REPORTED_ERRORS
        ]:
            loc = ["body", "data", *[p if isinstance(p, int) else str(p)[:100] for p in err.absolute_path]]
            found.append({"loc": loc, "msg": f"does not satisfy the step's schema ('{err.validator}' keyword)",
                          "type": f"json_schema.{err.validator}"})
    except Exception:  # noqa: BLE001 - SchemaError, an unresolvable $ref, ...
        return [{"loc": ["body", "data"], "msg": "the step's schema could not be applied",
                 "type": "json_schema.invalid_schema"}]
    return found[:MAX_REPORTED_ERRORS]


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
    # The shared decision helper (eap_api.approvals, AC.8): the decision event and its
    # pending_approvals projection update in one transaction, then the wake-up.
    result = decide(
        event_log, launcher, tenant_id=tenant_id, run_id=run_id, kind="step", key=step_id,
        decision=decision, decided_by=principal.user_id, comment=body.comment,
    )
    if result.outcome == "run_not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if result.outcome == "data_required":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": ["path", "step_id"], "msg": "this step takes data: answer it with .../resume",
                     "type": "resume_data_required"}],
        )
    if result.outcome != "created":
        # Same text as always; an earlier decision (a person's or the timeout's) is added.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": NOT_WAITING_DETAIL, **(result.recorded_decision() or {})},
        )
    return JSONResponse(
        content={
            "run_id": str(run_id),
            "step_id": step_id,
            "decision": decision,
            "decided_by": principal.user_id,
            "comment": body.comment,
            "notified": result.notified,
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


@router.post(
    "/runs/{run_id}/steps/{step_id}/resume",
    responses={**_DECISION_RESPONSES, 413: {"model": ErrorDetail}, 415: {"model": ErrorDetail}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": ResumeSubmission.model_json_schema()}},
        }
    },
)
def resume_step(
    run_id: uuid.UUID,
    step_id: StepIdPath,
    principal: PrincipalDep,  # before the body: 401 wins over 422
    body: Annotated[ResumeSubmission, Depends(_resume_body)],
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Answer a waiting input step with `data`: 200 {run_id, step_id, decision: "approved",
    decided_by, decided_at, comment, notified}. 404 unknown, another tenant's or non-workflow run;
    409 the step is not waiting, is already decided (the answer names the recorded decision, who and
    when) or takes no data (an approval step); 422 `data` does not satisfy the step's resume_schema
    (loc/msg/type only, never the values), is over 32 KiB or has a non-finite number."""
    run_store, event_log = stores
    tenant_id = principal.tenant_id
    run = run_store.get_run(tenant_id, run_id)
    if run is None or run.get("kind") != "workflow":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    waiting = event_log.get_event(tenant_id, run_id, f"step:{step_id}:waiting")
    waiting_data = waiting.data if waiting is not None and isinstance(waiting.data, dict) else {}
    schema = waiting_data.get("resume_schema")
    earlier = event_log.get_event(tenant_id, run_id, f"step:{step_id}:decision") if waiting is not None else None
    if earlier is not None:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": NOT_WAITING_DETAIL, **(DecisionResult("exists", event=earlier).recorded_decision() or {})},
        )
    if waiting is None or run.get("status") in TERMINAL_RUN_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_WAITING_DETAIL)
    if not isinstance(schema, dict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="step takes no data: approve or reject it")
    errors = resume_data_errors(schema, body.data)
    if not errors and len(_canonical_json(body.data).encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES:
        errors = [{"loc": ["body", "data"], "msg": f"data must be at most {MAX_WORKFLOW_INPUT_BYTES // 1024} KiB as JSON",
                   "type": "too_long"}]
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors)
    result = decide(
        event_log, launcher, tenant_id=tenant_id, run_id=run_id, kind="step", key=step_id,
        decision="approved", decided_by=principal.user_id, comment=body.comment, resume_data=body.data,
    )
    if result.outcome == "run_not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if result.outcome != "created":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": NOT_WAITING_DETAIL, **(result.recorded_decision() or {})},
        )
    return JSONResponse(
        content={
            "run_id": str(run_id),
            "step_id": step_id,
            "decision": "approved",
            "decided_by": principal.user_id,
            "decided_at": (result.recorded_decision() or {}).get("decided_at"),
            "comment": body.comment,
            "notified": result.notified,
        }
    )


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


# --------------------------------------------------------------------------- cancel (loops D12)

CANCEL_NOT_SUPPORTED = "cancel_not_supported"
CANCEL_NOT_SUPPORTED_DETAIL = "only workflow runs can be cancelled"
ALREADY_ENDED_DETAIL = "the run has already ended"
CANCEL_PENDING_DETAIL = (
    "cancel recorded but not finished; it completes automatically, or retry the request"
)

# POST /runs/{run_id}/cancel: no /workflows prefix (D12 names the route by run). No body is read.
cancel_router = APIRouter(tags=["workflow runs"], route_class=WorkflowRunRoute)


def _cascade(launcher: RunLauncher, run_id: uuid.UUID) -> None:
    """D12 step 2 (a seam the tests replace): cancel the run's DBOS workflow tree. The run's DBOS
    workflow id is its run id."""
    launcher.cancel_workflow_tree(workflow_id=str(run_id))


def _finish_cancel(run_store: Any, tenant_id: str, run_id: uuid.UUID) -> Any:
    """D12 step 3 (a seam the tests replace to inject a crash after the cascade)."""
    return run_store.cancel_run_tree(tenant_id, run_id)


@cancel_router.post(
    "/runs/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunCancelResponse,
    responses={
        404: {"model": ErrorDetail},
        409: {"model": CancelNotSupportedDetail | ErrorDetail},
        503: {"model": ErrorDetail},
    },
)
def cancel_run(
    run_id: uuid.UUID,
    principal: PrincipalDep,
    stores: StoresDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Cancel a workflow run and its whole tree (branch and iteration workflows, child agent runs).

    202 RunCancelResponse: the run ended `failed` (or `needs_attention` when a side effect was
    still reserved) with `run_cancelled`; idempotent, so a retry answers 202 again with the same
    status (a run that ended by itself between the request and the terminal write keeps its own
    outcome, `cancelled` False). 404 unknown run or another tenant's. 409 `{detail, code:
    "cancel_not_supported"}` for a run that is not a workflow run (agent, tool, ingest, eval; a
    workflow's child agent run is cancelled with its workflow); 409 `{detail}` when the run had
    already ended before any cancel was requested. 503 when the request is recorded but the
    cascade or the terminal write failed: the reconciler finishes it, a retry finishes it at once.
    """
    run_store, event_log = stores
    tenant_id = principal.tenant_id
    run = run_store.get_run(tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.get("kind") != "workflow":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": CANCEL_NOT_SUPPORTED_DETAIL, "code": CANCEL_NOT_SUPPORTED},
        )
    # Step 1, its own transaction (EventLog.append commits before returning): under the run-row
    # lock, an existing `cancel` is returned as it is, and after `end` a new key is dropped.
    requested = event_log.append(
        tenant_id, run_id, CANCEL_EVENT_KEY, "cancel_requested", {"requested_by": principal.user_id}
    )
    if requested.dropped:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ALREADY_ENDED_DETAIL)
    try:
        _cascade(launcher, run_id)  # step 2: no eap_app lock or transaction is held here
        result = _finish_cancel(run_store, tenant_id, run_id)  # step 3
    except Exception as exc:  # noqa: BLE001 - the durable request stands; the reconciler finishes it
        log_failure(
            log, "cancel not finished", exc, code="cancel_incomplete", level=logging.ERROR,
            run_id=str(run_id), tenant_id=tenant_id,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=CANCEL_PENDING_DETAIL)
    end = event_log.get_event(tenant_id, run_id, "end")
    error_code = (end.data or {}).get("error_code") if end is not None else None
    current = run_store.get_run(tenant_id, run_id) or run
    log.info("workflow run %s cancel: tenant=%s status=%s", run_id, tenant_id, result.status)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=RunCancelResponse(
            run_id=str(run_id),
            kind="workflow",
            status=result.status or current["status"],
            display_status=current["display_status"],
            error_code=error_code,
            cancelled=error_code == "run_cancelled",
            requested_by=(requested.event.data or {}).get("requested_by"),
            approvals_closed=result.approvals_closed,
        ).model_dump(),
    )
