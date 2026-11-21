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
A workflow run that started knowledge-base source syncs (`kb.source.sync`; `kb_source_syncs.parent_run_id` is the
run) cancels them too, each the same three steps (`cancel_requested` on the sync run, the DBOS cascade of its
workflow tree, the guarded terminal write), after the run's own cascade: the sync is a separate DBOS workflow, not
a child of the run's, and `workflow.run.wait` sees the run's `cancel` event meanwhile.
A failure after step 1 answers 503 and leaves the durable request: the scheduled
`eap.cancel_reconcile` (worker) re-runs steps 2 and 3 for it, and a retried POST does so at once.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from eap_api.bodies import MAX_COMMENT_CHARS, ApprovalDecision, ResumeSubmission, WorkflowRunCreate  # noqa: F401
from eap_api.auth import Principal, current_principal
from eap_api.request_guard import guarded_json_route
from eap_api.routes.workflows import reference_problems  # noqa: F401 - re-exported
from eap_api.schemas import CancelNotSupportedDetail, ErrorDetail, RunCancelResponse
from eap_api.service_deps.workflow_runs import WorkflowRunServiceDep
from eap_api.services.workflow_inputs import input_schema_errors, non_finite_errors  # noqa: F401 - re-exported
from eap_api.services.workflow_runs import (  # noqa: F401 - re-exported: the API's users and tests import them from here
    ALREADY_ENDED_DETAIL,
    CANCEL_NOT_SUPPORTED,
    CANCEL_NOT_SUPPORTED_DETAIL,
    CANCEL_PENDING_DETAIL,
    CANCELLABLE_KINDS,
    MAX_WORKFLOW_INPUT_BYTES,
    NOT_WAITING_DETAIL,
    resume_data_errors,
)
from eap_schemas.workflows import STEP_ID_PATTERN

log = logging.getLogger("eap_api")

# The body is {"input": ..., "version"?, "run_id"?} or {"comment": ...}: the input plus slack.
MAX_RUN_BODY_BYTES = 64 * 1024
MAX_RUN_BODY_DEPTH = 64

MAX_REPORTED_ERRORS = 20


# The shared JSON write-route guards (eap_api.request_guard): more than MAX_RUN_BODY_BYTES -> 413;
# not strict UTF-8 (or any NUL byte) -> 415; nested deeper than MAX_RUN_BODY_DEPTH -> 422;
# unstorable strings (U+0000, lone surrogates) -> 422; compact 422s.
WorkflowRunRoute = guarded_json_route(max_body_bytes=MAX_RUN_BODY_BYTES, max_depth=MAX_RUN_BODY_DEPTH)


router = APIRouter(prefix="/workflows", tags=["workflow runs"], route_class=WorkflowRunRoute)

PrincipalDep = Annotated[Principal, Depends(current_principal)]


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
    service: WorkflowRunServiceDep,
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
    started = service.start(
        principal.tenant_id, workflow_id, body.input, version=body.version, run_id=body.run_id,
        rerun_of=body.rerun_of, pause_after_each_step=body.pause_after_each_step,
    )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=started)


async def _resume_body(request: Request) -> ResumeSubmission:
    return await _parse_body(request, ResumeSubmission)


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
    service: WorkflowRunServiceDep,
) -> JSONResponse:
    """Answer a waiting input step with `data`: 200 {run_id, step_id, decision: "approved",
    decided_by, decided_at, comment, notified}. 404 unknown, another tenant's or non-workflow run;
    409 the step is not waiting, is already decided (the answer names the recorded decision, who and
    when) or takes no data (an approval step); 422 `data` does not satisfy the step's resume_schema
    (loc/msg/type only, never the values), is over 32 KiB or has a non-finite number."""
    answer = service.resume_step(principal.tenant_id, principal.user_id, run_id, step_id, body.data, body.comment)
    return JSONResponse(content=answer)


@router.post("/runs/{run_id}/steps/{step_id}/approve", responses=_DECISION_RESPONSES)
def approve_step(
    run_id: uuid.UUID,
    step_id: StepIdPath,
    principal: PrincipalDep,
    body: Annotated[ApprovalDecision, Depends(_decision_body)],
    service: WorkflowRunServiceDep,
) -> JSONResponse:
    """Approve a waiting approval step: 200 {run_id, step_id, decision, decided_by, comment,
    notified}; 409 when the step is not waiting (never reached, already decided, timed out, run
    finished); 404 unknown, another tenant's or non-workflow run."""
    answer = service.decide_step(principal.tenant_id, principal.user_id, run_id, step_id, "approved", body.comment)
    return JSONResponse(content=answer)


@router.post("/runs/{run_id}/steps/{step_id}/reject", responses=_DECISION_RESPONSES)
def reject_step(
    run_id: uuid.UUID,
    step_id: StepIdPath,
    principal: PrincipalDep,
    body: Annotated[ApprovalDecision, Depends(_decision_body)],
    service: WorkflowRunServiceDep,
) -> JSONResponse:
    """Reject a waiting approval step (the run then fails with `approval_rejected`); same answers
    as approve."""
    answer = service.decide_step(principal.tenant_id, principal.user_id, run_id, step_id, "rejected", body.comment)
    return JSONResponse(content=answer)


# --------------------------------------------------------------------------- cancel (loops D12)

# POST /runs/{run_id}/cancel: no /workflows prefix (D12 names the route by run). No body is read.
cancel_router = APIRouter(tags=["workflow runs"], route_class=WorkflowRunRoute)


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
def cancel_run(run_id: uuid.UUID, principal: PrincipalDep, service: WorkflowRunServiceDep) -> JSONResponse:
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
    answer = service.cancel(principal.tenant_id, principal.user_id, run_id)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=RunCancelResponse(**answer).model_dump())
