"""Workflow definitions API: drafts and immutable published versions, mirroring /agents.

Publish re-validates the draft and checks what the spec points at: every agent step's agent must
belong to this tenant and have a published version (the pinned one, if pinned); every tool step's
tool must exist in the platform catalog and be allowed by EAP_ALLOWED_TOOLS.

Structured agent output (AC.9): an agent step whose agent version has `output.json_schema` outputs
that object, so `steps.<id>.output.<path>` reads its fields. Publish checks every such field `$ref`
(in input templates and branch conditions; a whole-output ref is not checked) against the schema,
following it as the runtime validates it (output_ref_problem):
- pinned step, a path that cannot exist (into a scalar, a named field of an array, or a field an
  `additionalProperties: false` object does not list) -> 422;
- pinned step, a field not listed by an object that allows other fields (`additionalProperties`
  absent or true) -> warning `unlisted_output_field`;
- pinned step whose version outputs text -> warning `text_agent_output_ref`;
- unpinned step (`version: null`) -> ALWAYS a warning `unpinned_agent_output_ref`, saying whether
  the latest version matches today: the version is resolved when a run reaches the step, so a
  later agent version can rename the field (at run time the branch's step_completed then lists the
  path in `unresolved_refs`).
Publish answers 201 with the version and `warnings` ([{code, path: "steps[<index>]", message}]). `GET /workflows/agent-output-schema` serves an
agent version's output schema and its field paths to API clients and editors.

Workflow loops (LP.5): every check above walks loop bodies at every depth, and a body step's
warning `path` is `steps[<i>].body.steps[<j>]`.
- Agent and tool steps inside bodies are checked like top-level ones (reference_problems).
- Every `llm_judge` evaluator of an `evaluate` step needs an allowlisted model (EAP_ALLOWED_MODELS).
- Environment caps (loop_limit_problems): each loop's `max_items`/`max_iterations` <=
  EAP_WORKFLOW_MAX_LOOP_ITERATIONS (100); `static_execution_bound` <= EAP_WORKFLOW_MAX_STEP_EXECUTIONS
  (2000, `workflow_too_many_executions`); `run_concurrency` <= EAP_WORKFLOW_MAX_RUN_CONCURRENCY (8,
  `workflow_concurrency_exceeded`). Checked at publish and run start; a draft save accepts them.
  A malformed value fails app startup (check_workflow_configuration).
- `loop.<L>.item.<path>` is checked like a field ref: the foreach's `items` ref is followed to the
  agent step output it lists, and the path is checked under that array's `items` schema.
- A pinned body agent whose version has an approval policy gives the warning
  `loop_agent_approvals_denied` (D8a): inside a loop its gated tool calls are denied.
- With `limits.max_cost_usd`, an `llm_judge` model whose requests count as $0 (no price, a $0
  price, or no usable price file) gives the agent publish's warning `cost_cap_unpriced_model`
  (LP.16), path `steps[<i>].evaluators[<k>]`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.database import get_session
from eap_api.models import AgentModel
from eap_api.request_guard import guarded_json_route, schema_checks_storable
from eap_api.routes import _run_definition_op
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_api.workflows.schemas import WorkflowCreate, WorkflowUpdate
from eap_run_store.revisions import bump_row
from eap_run_store.definitions import (
    publish_workflow,
    restore_workflow_version,
    set_workflow_archived,
)
from eap_run_store.workflow_checks import (  # noqa: F401 - re-exported: the API's users and tests import them from here
    CAP_BOUNDS,
    LOOP_ITERATIONS_ENV,
    RUN_CONCURRENCY_ENV,
    STEP_EXECUTIONS_ENV,
    WARNING_LOOP_AGENT_APPROVALS,
    WARNING_TEXT_OUTPUT_REF,
    WARNING_UNLISTED_OUTPUT_FIELD,
    WARNING_UNPINNED_OUTPUT_REF,
    RefProblem,
    _agent_version,
    _env_cap,
    agent_step_cost_cap_warnings,
    check_workflow_configuration,
    judge_cost_cap_warnings,
    judge_model_problems,
    loop_agent_approval_warnings,
    loop_limit_problems,
    output_fields,
    output_ref_checks,
    output_ref_problem,
    output_schema_of,
)
from eap_run_store.workflow_checks import reference_problems as _reference_problems
from eap_schemas.workflows import MAX_SPEC_DEPTH, WorkflowSpec

# A request body is {"spec": <spec>}: one level above the spec's own nesting cap.
MAX_BODY_DEPTH = MAX_SPEC_DEPTH + 1
# The spec is capped at 64 KiB as JSON; the body adds the {"spec": ...} wrapper and whitespace.
MAX_BODY_BYTES = 128 * 1024

# The shared JSON write-route guards (eap_api.request_guard): more than MAX_BODY_BYTES -> 413; not
# strict UTF-8 (or any NUL byte) -> 415; nested deeper than MAX_BODY_DEPTH -> 422; compact 422s;
# unstorable strings (U+0000, lone surrogates) -> 422. Create and patch skip that last check
# (@schema_checks_storable): WorkflowSpec rejects them itself, with messages naming the step.
BoundedJsonRoute = guarded_json_route(
    max_body_bytes=MAX_BODY_BYTES,
    max_depth=MAX_BODY_DEPTH,
    depth_detail=f"workflow spec is nested more than {MAX_SPEC_DEPTH} levels deep",
)


router = APIRouter(prefix="/workflows", tags=["workflows"], route_class=BoundedJsonRoute)

SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _get_workflow(db: Session, workflow_id: uuid.UUID, tenant_id: str) -> WorkflowModel:
    workflow = (
        db.query(WorkflowModel)
        .filter(WorkflowModel.id == str(workflow_id), WorkflowModel.tenant_id == tenant_id)
        .first()
    )
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return workflow


def _latest_version(db: Session, workflow: WorkflowModel) -> int | None:
    return (
        db.query(func.max(WorkflowVersionModel.version))
        .filter(
            WorkflowVersionModel.workflow_id == workflow.id,
            WorkflowVersionModel.tenant_id == workflow.tenant_id,
        )
        .scalar()
    )


def _workflow_response(workflow: WorkflowModel, latest_version: int | None) -> dict:
    return {
        "id": str(workflow.id),
        "name": workflow.name,
        "draft_spec": workflow.draft_spec,
        "latest_version": latest_version,
        "archived": bool(workflow.archived),
        "archived_at": workflow.archived_at.isoformat() if workflow.archived_at else None,
        "revision": workflow.revision,
    }


def _version_response_dict(version: dict) -> dict:
    """A version row as the shared definition operations return it."""
    return {
        "workflow_id": str(version["workflow_id"]),
        "version": version["version"],
        "spec": version["spec"],
        "spec_hash": version["spec_hash"],
        "created_at": version["created_at"].isoformat(),
        "created_by": version["created_by"],
        "created_via": version["created_via"],
    }


def _version_response(version: WorkflowVersionModel) -> dict:
    return {
        "workflow_id": str(version.workflow_id),
        "version": version.version,
        "spec": version.spec,
        "spec_hash": version.spec_hash,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
        "created_via": version.created_via,
    }


# The publish-time validation (loop caps, references, output refs, warnings) lives in
# eap_run_store.workflow_checks, shared with the Architect tools; re-exported above for the API's users.
def reference_problems(db: Session, spec: WorkflowSpec, tenant_id: str, *, lock: bool = False) -> list[str]:
    """What a spec points at that this tenant cannot run (see eap_run_store.workflow_checks)."""
    return _reference_problems(db.connection(), spec, tenant_id, lock=lock)



@router.get(
    "/agent-output-schema",
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}},
)
def get_agent_output_schema(
    principal: PrincipalDep,
    db: SessionDep,
    agent_id: Annotated[uuid.UUID, Query()],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> JSONResponse:
    """The output schema an agent step would read (AC.9): the pinned `version`, else the latest
    published one. `json_schema` is null for a text agent (its step output is a string), and
    `fields` lists the paths `steps.<id>.output.<path>` can read."""
    agent = (
        db.query(AgentModel)
        .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == principal.tenant_id)
        .first()
    )
    if agent is None or agent.archived:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    row = _agent_version(db.connection(), principal.tenant_id, str(agent_id), version)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    schema = output_schema_of(row.spec)
    return JSONResponse(content={
        "agent_id": str(agent_id),
        "version": row.version,
        "pinned": version is not None,
        "structured": schema is not None,
        "json_schema": schema,
        "fields": output_fields(schema) if schema is not None else [],
    })


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    responses={422: {"model": ErrorDetail}},
)
@schema_checks_storable
def create_workflow(request: WorkflowCreate, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """Create a workflow with an initial draft."""
    workflow = WorkflowModel(
        tenant_id=principal.tenant_id,
        name=request.spec.name,
        draft_spec=request.spec.model_dump(mode="json"),
    )
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    return stamp(JSONResponse(content=_workflow_response(workflow, None), status_code=201), workflow.revision)


@router.get("", response_class=JSONResponse)
def list_workflows(
    principal: PrincipalDep, db: SessionDep, archived: Literal["true", "false", "all"] = "false"
) -> JSONResponse:
    """List this tenant's workflows, oldest first: the active ones, or with `?archived=true` the
    archived ones only, or with `?archived=all` both."""
    query = db.query(WorkflowModel).filter(WorkflowModel.tenant_id == principal.tenant_id)
    if archived != "all":
        query = query.filter(WorkflowModel.archived.is_(archived == "true"))
    workflows = query.order_by(WorkflowModel.created_at, WorkflowModel.id).all()
    return JSONResponse(content=[_workflow_response(w, _latest_version(db, w)) for w in workflows])


@router.get(
    "/{workflow_id}", response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}, **etag_responses(write=False)},
)
def get_workflow(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    return read_response(_workflow_response(workflow, _latest_version(db, workflow)), workflow.revision)


@router.patch(
    "/{workflow_id}",
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, **etag_responses()},
)
@schema_checks_storable
def patch_workflow(
    workflow_id: uuid.UUID, request: WorkflowUpdate, principal: PrincipalDep, db: SessionDep, pre: IfMatch
) -> JSONResponse:
    """Replace the draft. Published versions never change. Send the ETag you read as `If-Match`: a
    stale one is 412 `revision_mismatch` with the current workflow (see eap_api.concurrency)."""
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)

    def current() -> tuple[dict, int]:
        db.refresh(workflow)
        return _workflow_response(workflow, _latest_version(db, workflow)), workflow.revision

    pre.check(workflow.revision, current)
    with pre.guard(current, db):
        bump_row(db, workflow, pre.expected)
        workflow.name = request.spec.name
        workflow.draft_spec = request.spec.model_dump(mode="json")
        db.commit()
    db.refresh(workflow)
    return pre.respond(_workflow_response(workflow, _latest_version(db, workflow)), workflow.revision)


@router.post(
    "/{workflow_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def publish_workflow_version(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """Publish the draft as the next immutable version (409 when it equals the latest one): 201 with
    the version and `warnings` (AC.9: refs into unpinned or text agents' output; LP.5: pinned loop
    body agents with an approval policy; LP.16: `cost_cap_unpriced_model` for an llm_judge model,
    or an agent step's agent model, that counts as $0 toward `limits.max_cost_usd`). The work is
    eap_run_store.definitions.publish_workflow, shared with the Architect's publish tool."""
    result = _run_definition_op(
        db, lambda conn: publish_workflow(conn, principal.tenant_id, str(workflow_id), created_by=principal.user_id)
    )
    return stamp(
        JSONResponse(content={**_version_response_dict(result.version), "warnings": result.warnings}, status_code=201),
        result.revision,
    )


class WorkflowVersionRestore(BaseModel):
    """Body of POST /workflows/{id}/versions/{n}/restore (optional): same as the agent's."""

    model_config = ConfigDict(extra="forbid")

    discard_draft: bool = False


@router.post(
    "/{workflow_id}/versions/{version_num}/restore",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def restore_workflow_version_route(
    workflow_id: uuid.UUID,
    version_num: int,
    principal: PrincipalDep,
    db: SessionDep,
    request: Annotated[WorkflowVersionRestore | None, Body()] = None,
) -> JSONResponse:
    """Roll back by publishing version `latest + 1` whose spec equals version `version_num`
    (history is never rewritten; the spec is re-validated under today's policy; the draft becomes the
    restored spec). 409 when the source is (or equals) the latest version, when the draft has
    unpublished changes and `discard_draft` is not set, or for an archived workflow. 201 with the
    version, `restored_from` (not stored: workflow versions have no such column) and `warnings`.
    The work is eap_run_store.definitions.restore_workflow_version."""
    discard = request.discard_draft if request is not None else False
    result = _run_definition_op(
        db,
        lambda conn: restore_workflow_version(
            conn, principal.tenant_id, str(workflow_id), version_num, created_by=principal.user_id,
            discard_draft=discard,
        ),
    )
    return stamp(
        JSONResponse(
            content={**_version_response_dict(result.version), "restored_from": version_num, "warnings": result.warnings},
            status_code=201,
        ),
        result.revision,
    )


class WorkflowArchive(BaseModel):
    """Body of POST /workflows/{id}/archive (the body itself is optional)."""

    model_config = ConfigDict(extra="forbid")
    archived: bool = True
    force: bool = False


def _archive(
    workflow_id: uuid.UUID, principal: Principal, db: Session, archived: bool, force: bool = False
) -> JSONResponse:
    result = _run_definition_op(
        db,
        lambda conn: set_workflow_archived(conn, principal.tenant_id, str(workflow_id), archived, force=force),
    )
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    db.refresh(workflow)
    return read_response(
        {**_workflow_response(workflow, _latest_version(db, workflow)), "paused_schedules": list(result.paused_schedules)},
        workflow.revision,
    )


@router.post(
    "/{workflow_id}/archive",
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}, 409: {"description": "runs_active: queued or running runs"}},
)
def archive_workflow(
    workflow_id: uuid.UUID,
    principal: PrincipalDep,
    db: SessionDep,
    body: Annotated[WorkflowArchive | None, Body()] = None,
) -> JSONResponse:
    """Archive the workflow, or with `{"archived": false}` unarchive it (body optional, default
    `{"archived": true, "force": false}`; idempotent; 200 with the workflow, `archived`,
    `archived_at`, and the ids of the schedules the archive paused). It disappears from
    GET /workflows (listed with `?archived=true`), refuses new runs and schedules with 409
    `workflow_archived`, and cannot be published or restored until unarchived; versions, runs and
    schedules stay readable. 409 `runs_active` (with `active_runs`) while it has queued or running
    runs, unless `force`. Unarchiving does not resume schedules. Another tenant's workflow is 404."""
    body = body or WorkflowArchive()
    return _archive(workflow_id, principal, db, body.archived, body.force)


@router.post(
    "/{workflow_id}/unarchive",
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}},
)
def unarchive_workflow(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """Unarchive the workflow (idempotent; same as POST archive with `{"archived": false}`). Its
    paused schedules stay paused; resume them with PATCH /workflows/schedules/{id} `{"active": true}`."""
    return _archive(workflow_id, principal, db, False)


@router.get("/{workflow_id}/versions", response_class=JSONResponse, responses={404: {"model": ErrorDetail}})
def list_workflow_versions(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """Published versions, newest first."""
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    versions = (
        db.query(WorkflowVersionModel)
        .filter(
            WorkflowVersionModel.workflow_id == workflow.id,
            WorkflowVersionModel.tenant_id == principal.tenant_id,
        )
        .order_by(WorkflowVersionModel.version.desc())
        .all()
    )
    return JSONResponse(content=[_version_response(v) for v in versions])


@router.get(
    "/{workflow_id}/versions/{version_num}",
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}},
)
def get_workflow_version(
    workflow_id: uuid.UUID, version_num: int, principal: PrincipalDep, db: SessionDep
) -> JSONResponse:
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    version = (
        db.query(WorkflowVersionModel)
        .filter(
            WorkflowVersionModel.workflow_id == workflow.id,
            WorkflowVersionModel.tenant_id == principal.tenant_id,
            WorkflowVersionModel.version == version_num,
        )
        .first()
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return JSONResponse(content=_version_response(version))
