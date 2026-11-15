"""Workflow definitions API: drafts and immutable published versions, mirroring /agents.

Publish re-validates the draft and checks what the spec points at: every agent step's agent must
belong to this tenant and have a published version (the pinned one, if pinned); every tool step's
tool must exist in the platform catalog and be allowed by EAP_ALLOWED_TOOLS.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.models import AgentModel, AgentVersionModel
from eap_api.request_guard import guarded_json_route, schema_checks_storable
from eap_api.routes.tools import _allowed_tool_ids
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_api.workflows.schemas import WorkflowCreate, WorkflowUpdate
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_schemas.workflows import MAX_SPEC_DEPTH, AgentStep, ToolStep, WorkflowSpec, canonical_workflow_hash

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
    }


def _version_response(version: WorkflowVersionModel) -> dict:
    return {
        "workflow_id": str(version.workflow_id),
        "version": version.version,
        "spec": version.spec,
        "spec_hash": version.spec_hash,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
    }


def reference_problems(db: Session, spec: WorkflowSpec, tenant_id: str, *, lock: bool = False) -> list[str]:
    """What a spec points at that this tenant cannot run, one message per problem, naming the step.

    Another tenant's agent reads exactly like a missing one, so nothing about it leaks. With `lock`
    (publish), the referenced agent rows are first locked FOR SHARE until the transaction ends, in
    id order (no deadlock between two publishes): an archive (FOR NO KEY UPDATE) of one of them
    waits for the publish, whose step then shows in the archive's referencing workflows, or this
    check sees `archived`."""
    if lock:
        agent_ids = sorted({str(step.agent_id) for step in spec.steps if isinstance(step, AgentStep)})
        if agent_ids:
            (
                db.query(AgentModel.id)
                .filter(AgentModel.id.in_(agent_ids), AgentModel.tenant_id == tenant_id)
                .order_by(AgentModel.id)
                .with_for_update(read=True)
                .all()
            )
    problems: list[str] = []
    allowed_tools = _allowed_tool_ids()
    for step in spec.steps:
        if isinstance(step, AgentStep):
            agent_id = str(step.agent_id)
            agent = (
                db.query(AgentModel)
                .filter(AgentModel.id == agent_id, AgentModel.tenant_id == tenant_id)
                .first()
            )
            if agent is None or agent.archived:
                problems.append(f"step '{step.id}': agent '{agent_id}' was not found")
                continue
            versions = db.query(AgentVersionModel.version).filter(
                AgentVersionModel.agent_id == agent_id, AgentVersionModel.tenant_id == tenant_id
            )
            if step.version is None:
                if versions.first() is None:
                    problems.append(f"step '{step.id}': agent '{agent_id}' has no published version")
            elif versions.filter(AgentVersionModel.version == step.version).first() is None:
                problems.append(f"step '{step.id}': agent '{agent_id}' has no published version {step.version}")
        elif isinstance(step, ToolStep):
            if step.tool_id not in TOOL_DECLARATIONS:
                problems.append(f"step '{step.id}': tool '{step.tool_id}' does not exist")
            elif allowed_tools is not None and step.tool_id not in allowed_tools:
                problems.append(
                    f"step '{step.id}': tool '{step.tool_id}' is not allowed on this platform "
                    "(not in the EAP_ALLOWED_TOOLS allowlist)"
                )
    return problems


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
    return JSONResponse(content=_workflow_response(workflow, None), status_code=201)


@router.get("", response_class=JSONResponse)
def list_workflows(principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """List this tenant's workflows (not archived), oldest first."""
    workflows = (
        db.query(WorkflowModel)
        .filter(WorkflowModel.tenant_id == principal.tenant_id, WorkflowModel.archived.is_(False))
        .order_by(WorkflowModel.created_at, WorkflowModel.id)
        .all()
    )
    return JSONResponse(content=[_workflow_response(w, _latest_version(db, w)) for w in workflows])


@router.get("/{workflow_id}", response_class=JSONResponse, responses={404: {"model": ErrorDetail}})
def get_workflow(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    return JSONResponse(content=_workflow_response(workflow, _latest_version(db, workflow)))


@router.patch(
    "/{workflow_id}",
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
@schema_checks_storable
def patch_workflow(
    workflow_id: uuid.UUID, request: WorkflowUpdate, principal: PrincipalDep, db: SessionDep
) -> JSONResponse:
    """Replace the draft. Published versions never change."""
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    workflow.name = request.spec.name
    workflow.draft_spec = request.spec.model_dump(mode="json")
    db.commit()
    db.refresh(workflow)
    return JSONResponse(content=_workflow_response(workflow, _latest_version(db, workflow)))


@router.post(
    "/{workflow_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def publish_workflow_version(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """Publish the draft as the next immutable version (409 when it equals the latest one)."""
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    if workflow.draft_spec is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workflow has no draft spec")
    try:
        spec = WorkflowSpec.model_validate(workflow.draft_spec)
    except ValidationError as exc:
        messages = "; ".join(err["msg"].removeprefix("Value error, ") for err in exc.errors())
        raise HTTPException(status_code=422, detail=f"draft spec is invalid: {messages}") from None
    problems = reference_problems(db, spec, principal.tenant_id, lock=True)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))

    spec_hash = canonical_workflow_hash(spec)
    latest = _latest_version(db, workflow)
    if latest is not None:
        latest_row = (
            db.query(WorkflowVersionModel)
            .filter(WorkflowVersionModel.workflow_id == workflow.id, WorkflowVersionModel.version == latest)
            .first()
        )
        if latest_row is not None and latest_row.spec_hash == spec_hash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"no changes since version {latest}")

    version = WorkflowVersionModel(
        workflow_id=workflow.id,
        tenant_id=principal.tenant_id,
        version=(latest or 0) + 1,
        spec=spec.model_dump(mode="json"),
        spec_hash=spec_hash,
        created_by=principal.user_id,
    )
    db.add(version)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="concurrent publish, retry") from None
    db.refresh(version)
    return JSONResponse(content=_version_response(version), status_code=201)


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
