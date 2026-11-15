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
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from eap_schemas.workflows import (
    MAX_SPEC_DEPTH,
    REF_PATTERN,
    AgentStep,
    ToolStep,
    WorkflowSpec,
    canonical_workflow_hash,
    iter_refs,
)

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


def reference_problems(db: Session, spec: WorkflowSpec, tenant_id: str) -> list[str]:
    """What a spec points at that this tenant cannot run, one message per problem, naming the step.

    Another tenant's agent reads exactly like a missing one, so nothing about it leaks."""
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


# Publish warnings for refs into agent output (AC.9), {code, path, message} like the agent publish
# warnings; `path` is `steps[<index>]`, the message names the step and the ref.
WARNING_UNPINNED_OUTPUT_REF = "unpinned_agent_output_ref"
WARNING_TEXT_OUTPUT_REF = "text_agent_output_ref"
WARNING_UNLISTED_OUTPUT_FIELD = "unlisted_output_field"


def _schema_types(node: dict[str, Any]) -> set[str] | None:
    """The node's `type` as a set, None when it declares none (anything goes)."""
    declared = node.get("type")
    if declared is None:
        return None
    return {declared} if isinstance(declared, str) else {t for t in declared if isinstance(t, str)}


@dataclass(frozen=True)
class RefProblem:
    """Why a path cannot (blocking) or may not (not blocking) exist in a schema's object."""

    blocking: bool
    message: str


def output_ref_problem(schema: dict[str, Any], segments: list[str]) -> RefProblem | None:
    """Why `segments` (the path after `steps.<id>.output`) may not exist in an object of `schema`,
    or None when the schema lists it. Walks `properties` along the path; a numeric segment steps
    into an array's `items`. It follows the schema as the runtime validates it (Draft 2020-12, where
    a missing `additionalProperties` means true):
    - blocking: the path cannot exist (into a scalar, a named field of an array, or a field not in
      `properties` of an object with `additionalProperties: false`);
    - not blocking: the field is not listed, but the object allows other keys, so the agent may
      still return it (`additionalProperties` absent or true);
    - None: listed all the way, or below a sub-schema that constrains nothing (`true`, or an object
      without `properties`), where any path may exist."""
    node: Any = schema
    walked: list[str] = []
    for seg in segments:
        if not isinstance(node, dict):
            return None  # `true`/missing sub-schema: unconstrained
        types = _schema_types(node)
        if types is not None:
            types = types - {"null"} or {"null"}  # a nullable value is checked as its other type
        where = ".".join(walked) or "the output"
        props = node.get("properties")
        if isinstance(props, dict) and seg in props:
            node = props[seg]
        elif seg.isdigit() and (types is None or "array" in types) and ("items" in node or types == {"array"}):
            node = node.get("items", True)
        elif types is not None and not types & {"object", "array"}:
            kind = "/".join(sorted(types))
            return RefProblem(True, f"'{seg}' does not exist: {where} is a {kind}")
        elif types == {"array"}:
            return RefProblem(True, f"'{seg}' does not exist: {where} is an array (use a numeric index)")
        elif not isinstance(props, dict) and node.get("additionalProperties") is not False:
            return None  # an object that names no fields: anything may be there
        else:
            known = ", ".join(sorted(props)) if isinstance(props, dict) and props else "none"
            if node.get("additionalProperties") is False:
                return RefProblem(True, f"'{seg}' is not a field of {where} (fields: {known})")
            return RefProblem(False, (
                f"'{seg}' is not a listed field of {where} (fields: {known}); the schema allows other "
                "fields, so it resolves only if the agent returns it"
            ))
        walked.append(seg)
    return None


def output_schema_of(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """A stored agent version spec's structured output JSON Schema, None for a text agent."""
    output = (spec or {}).get("output")
    if isinstance(output, dict) and isinstance(output.get("json_schema"), dict):
        return output["json_schema"]
    return None


def output_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field path of `schema` that a `$ref` can read (`steps.<id>.output.<path>`), depth first;
    an array's items appear under the index `0`. Each entry is {path, type, description?}."""
    fields: list[dict[str, Any]] = []

    def visit(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            for name, sub in props.items():
                path = f"{prefix}.{name}" if prefix else name
                entry: dict[str, Any] = {"path": path, "type": sub.get("type") if isinstance(sub, dict) else None}
                if isinstance(sub, dict) and isinstance(sub.get("description"), str):
                    entry["description"] = sub["description"]
                fields.append(entry)
                visit(sub, path)
        items = node.get("items")
        if isinstance(items, dict) and prefix:
            path = f"{prefix}.0"
            fields.append({"path": path, "type": items.get("type")})
            visit(items, path)

    visit(schema, "")
    return fields


def _agent_version(db: Session, tenant_id: str, agent_id: str, version: int | None) -> AgentVersionModel | None:
    """The agent's pinned version, or its latest one for `version=None` (None when there is none)."""
    query = db.query(AgentVersionModel).filter(
        AgentVersionModel.agent_id == agent_id, AgentVersionModel.tenant_id == tenant_id
    )
    if version is not None:
        return query.filter(AgentVersionModel.version == version).first()
    return query.order_by(AgentVersionModel.version.desc()).first()


def output_ref_checks(
    db: Session, spec: WorkflowSpec, tenant_id: str
) -> tuple[list[str], list[dict[str, str]]]:
    """(problems, warnings) for `$ref`s into agent step outputs (see the module docstring). Steps
    whose agent or version does not resolve are left to reference_problems."""
    agent_steps = {s.id: s for s in spec.steps if isinstance(s, AgentStep)}
    schemas: dict[str, tuple[bool, dict[str, Any] | None] | None] = {}

    def target(step_id: str) -> tuple[bool, dict[str, Any] | None] | None:
        """(pinned, output schema) of agent step `step_id`; None when its version does not resolve."""
        if step_id not in schemas:
            step = agent_steps[step_id]
            row = _agent_version(db, tenant_id, str(step.agent_id), step.version)
            schemas[step_id] = None if row is None else (step.version is not None, output_schema_of(row.spec))
        return schemas[step_id]

    problems: list[str] = []
    warnings: list[dict[str, str]] = []
    for index, step in enumerate(spec.steps):
        for path in dict.fromkeys(iter_refs(step)):
            match = REF_PATTERN.fullmatch(path)
            ref_step = match.group("step") if match else None
            if ref_step not in agent_steps:
                continue
            segments = path.split(".")[3:]
            if not segments:
                continue  # the whole output
            found = target(ref_step)
            if found is None:
                continue
            pinned, schema = found
            where = f"steps[{index}]"
            if schema is None:
                version_note = "" if pinned else " (not pinned: its latest version)"
                warnings.append({
                    "code": WARNING_TEXT_OUTPUT_REF if pinned else WARNING_UNPINNED_OUTPUT_REF,
                    "path": where,
                    "message": (
                        f"step '{step.id}': $ref '{path}' reads a field, but agent step '{ref_step}'"
                        f"{version_note} outputs text (no structured output), so it does not resolve"
                    ),
                })
                continue
            problem = output_ref_problem(schema, segments)
            if not pinned:
                # Always: the version is resolved when a run reaches the step, so a later agent
                # version can rename the field (the branch then logs `unresolved_refs`).
                detail = (
                    f"it does not match the latest version's output schema: {problem.message}"
                    if problem is not None else "it matches the latest version's output schema today"
                )
                warnings.append({
                    "code": WARNING_UNPINNED_OUTPUT_REF,
                    "path": where,
                    "message": (
                        f"step '{step.id}': $ref '{path}' reads the output of agent step '{ref_step}', "
                        f"which is not pinned to a version; {detail}. Pin the version to check it at publish"
                    ),
                })
            elif problem is None:
                continue
            elif problem.blocking:
                problems.append(
                    f"step '{step.id}': $ref '{path}' does not match the output schema of agent step "
                    f"'{ref_step}': {problem.message}"
                )
            else:
                warnings.append({
                    "code": WARNING_UNLISTED_OUTPUT_FIELD,
                    "path": where,
                    "message": f"step '{step.id}': $ref '{path}' into agent step '{ref_step}': {problem.message}",
                })
    return problems, warnings


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
    row = _agent_version(db, principal.tenant_id, str(agent_id), version)
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
    """Publish the draft as the next immutable version (409 when it equals the latest one): 201 with
    the version and `warnings` (AC.9: refs into unpinned or text agents' output)."""
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    if workflow.draft_spec is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workflow has no draft spec")
    try:
        spec = WorkflowSpec.model_validate(workflow.draft_spec)
    except ValidationError as exc:
        messages = "; ".join(err["msg"].removeprefix("Value error, ") for err in exc.errors())
        raise HTTPException(status_code=422, detail=f"draft spec is invalid: {messages}") from None
    problems = reference_problems(db, spec, principal.tenant_id)
    ref_problems, warnings = output_ref_checks(db, spec, principal.tenant_id)
    problems += ref_problems
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
    return JSONResponse(content={**_version_response(version), "warnings": warnings}, status_code=201)


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
