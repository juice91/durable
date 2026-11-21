"""Route definitions for the control-plane API."""

import logging
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from eap_api.deps import SessionDep
from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.models import AgentModel, AgentVersionModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import AgentCreate, AgentUpdate, ErrorDetail, VersionRestore
from eap_api.version_diff import compare_specs
from eap_run_store.agent_checks import (  # noqa: F401 - re-exported: the API's users and tests import them from here
    PRICE_UNUSABLE,
    WARNING_COST_CAP_UNPRICED,
    WARNING_REDACTION_FIXED_VALUES,
    _fixed_string_paths,
    _price_status,
    check_publishable,
    cost_cap_warnings as _cost_cap_warnings,
    cost_cap_zero_reason,
    draft_hash,
    publish_warnings,
    validate_stored_draft,
)
from eap_run_store.revisions import bump_row
from eap_run_store.definitions import (
    publish_agent,
    restore_agent_version,
    set_agent_archived,
)
from eap_schemas import AgentSpec
from eap_schemas.architect import BUILTIN_ONLY_TOOL_PREFIXES
from eap_schemas.tools import ungated_always_gated_tools

# Body cap, UTF-8, depth, unstorable strings and compact 422s: eap_api.request_guard.
log = logging.getLogger("eap_api")

router = APIRouter(route_class=GuardedJsonRoute)


# Tool ids of the Architect's management tools: valid registry ids, but only an agent whose row is
# `builtin` may list them (the check lives here, on create and patch; the worker re-checks per run).
ARCHITECT_TOOL_PREFIX = BUILTIN_ONLY_TOOL_PREFIXES  # `architect.*` and `platform.*`
TOOL_NOT_ALLOWED = "tool_not_allowed"
BUILTIN_AGENT = "builtin_agent"
APPROVAL_REQUIRED = "approval_required"


def _refuse_ungated(spec: AgentSpec) -> JSONResponse | None:
    """422 `approval_required` when the spec lists an always-gated tool (the Architect's publish,
    restore, archive and schedule tools; `secrets.request`) without its `approvals.tools` entry."""
    ungated = ungated_always_gated_tools(spec.tools, spec.approvals.tools if spec.approvals else None)
    if not ungated:
        return None
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": f"tools {', '.join(ungated)} must be listed in approvals.tools",
            "code": APPROVAL_REQUIRED,
            "tools": ungated,
        },
    )


def _refuse_architect_tools(spec: AgentSpec, builtin: bool) -> JSONResponse | None:
    """422 `tool_not_allowed` when a non-builtin agent's spec lists an `architect.*` tool; 422
    `approval_required` for any agent that lists an always-gated tool without its approval entry."""
    if builtin:
        return _refuse_ungated(spec)
    denied = [t for t in spec.tools if t.startswith(ARCHITECT_TOOL_PREFIX)]
    if not denied:
        return _refuse_ungated(spec)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": f"tools {', '.join(denied)} are reserved for built-in agents",
            "code": TOOL_NOT_ALLOWED,
            "tools": denied,
        },
    )


def _builtin_role(agent: AgentModel) -> str | None:
    """`architect` for the Architect, `drafter` for its two helpers (slug `*-drafter`), else None."""
    if not agent.builtin:
        return None
    slug = agent.slug or ""
    if slug == "architect":
        return "architect"
    if slug.endswith("-drafter") or slug in ("scorer-suggester", "workflow-explainer"):  # helpers the page hides by default
        return "drafter"
    return None


def _latest_spec_summary(latest: AgentVersionModel | None) -> dict | None:
    """What the latest published version runs: version, model, tool count and spec hash (None: draft-only)."""
    if latest is None:
        return None
    spec = latest.spec or {}
    model = spec.get("model") or {}
    return {
        "version": latest.version,
        "model": {"provider": model.get("provider"), "name": model.get("name")},
        "tool_count": len(spec.get("tools") or []) + len(spec.get("mcp_tools") or []),
        "spec_hash": latest.spec_hash,
    }


def _agent_to_response(agent: AgentModel, latest: AgentVersionModel | None) -> dict:
    """Convert AgentModel to response dict. `latest` is the newest published version (None: draft-only);
    `draft_dirty` is true only when a version exists and the draft's canonical hash differs from it."""
    return {
        "builtin_role": _builtin_role(agent),
        "id": str(agent.id),
        "name": agent.name,
        "draft_spec": agent.draft_spec,
        "latest_version": latest.version if latest is not None else None,
        "latest_spec_summary": _latest_spec_summary(latest),
        "draft_dirty": latest is not None and draft_hash(agent.draft_spec) != latest.spec_hash,
        "archived": bool(agent.archived),
        "builtin": bool(agent.builtin),
        "revision": agent.revision,
    }


def _version_dict_to_response(version: dict) -> dict:
    """A version row as the shared definition operations return it (see eap_run_store.definitions)."""
    return {
        "agent_id": str(version["agent_id"]),
        "version": version["version"],
        "spec": version["spec"],
        "spec_hash": version["spec_hash"],
        "created_at": version["created_at"].isoformat(),
        "created_by": version["created_by"],
        "restored_from": version["restored_from"],
        "created_via": version["created_via"],
    }


def _version_to_response(version: AgentVersionModel) -> dict:
    """Convert AgentVersionModel to response dict."""
    return {
        "agent_id": str(version.agent_id),
        "version": version.version,
        "spec": version.spec,
        "spec_hash": version.spec_hash,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
        "restored_from": version.restored_from,
        "created_via": version.created_via,
    }


@router.post(
    "/agents",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    tags=["agents"],
    responses={422: {"model": ErrorDetail}, **etag_responses(success=201, write=False)},
)
def create_agent(
    request: AgentCreate,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Create a new agent."""
    refused = _refuse_architect_tools(request.spec, builtin=False)
    if refused is not None:
        return refused
    spec_dict = request.spec.model_dump(mode="json")

    agent = AgentModel(
        tenant_id=principal.tenant_id,
        name=request.spec.name,
        draft_spec=spec_dict,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    return stamp(JSONResponse(content={**_agent_to_response(agent, None), "builtin_role": None}, status_code=201), agent.revision)


@router.get(
    "/agents",
    response_class=JSONResponse,
    tags=["agents"],
)
def list_agents(
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
    archived: bool = False,
    include_system: bool = False,
) -> list[dict]:
    """List the current tenant's agents: the active ones, or with `?archived=true` the archived
    ones only (see eap_api.agent_archive). The Architect's drafter helpers (`builtin_role ==
    "drafter"`) are left out unless `?include_system=true`; they stay reachable by id."""
    agents = (
        db.query(AgentModel)
        .filter(AgentModel.tenant_id == principal.tenant_id, AgentModel.archived.is_(archived))
        .all()
    )

    agents = [a for a in agents if include_system or _builtin_role(a) != "drafter"]
    # One query for every agent's newest version (spec included), not one per agent.
    newest = (
        db.query(AgentVersionModel.agent_id, func.max(AgentVersionModel.version).label("v"))
        .filter(AgentVersionModel.agent_id.in_([str(a.id) for a in agents]))
        .group_by(AgentVersionModel.agent_id)
        .subquery()
    )
    latest_by_agent = {
        str(row.agent_id): row
        for row in db.query(AgentVersionModel).join(
            newest, (AgentVersionModel.agent_id == newest.c.agent_id) & (AgentVersionModel.version == newest.c.v)
        )
    }
    results = [_agent_to_response(agent, latest_by_agent.get(str(agent.id))) for agent in agents]

    return JSONResponse(content=results)


@router.get(
    "/agents/{agent_id}",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}, **etag_responses(write=False)},
)
def get_agent(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Get a specific agent by ID."""
    agent = (
        db.query(AgentModel)
        .filter(
            AgentModel.id == str(agent_id),
            AgentModel.tenant_id == principal.tenant_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    return read_response(_agent_to_response(agent, _latest_version(db, str(agent_id))), agent.revision)


@router.patch(
    "/agents/{agent_id}",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, **etag_responses()},
)
def patch_agent(
    agent_id: uuid.UUID,
    request: AgentUpdate,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
    pre: IfMatch,
) -> dict:
    """Update an agent's draft spec. Send the ETag you read as `If-Match`: a stale one is 412
    `revision_mismatch` with the current agent (see eap_api.concurrency)."""
    agent = (
        db.query(AgentModel)
        .filter(
            AgentModel.id == str(agent_id),
            AgentModel.tenant_id == principal.tenant_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    def current() -> tuple[dict, int]:
        db.refresh(agent)
        return _agent_to_response(agent, _latest_version(db, str(agent_id))), agent.revision

    pre.check(agent.revision, current)
    refused = _refuse_architect_tools(request.spec, builtin=bool(agent.builtin))
    if refused is not None:
        return refused
    spec_dict = request.spec.model_dump(mode="json")

    with pre.guard(current, db):
        bump_row(db, agent, pre.expected)
        agent.name = request.spec.name
        agent.draft_spec = spec_dict
        db.commit()
    db.refresh(agent)
    return pre.respond(_agent_to_response(agent, _latest_version(db, str(agent_id))), agent.revision)


def _latest_number(db: Session, agent_id: uuid.UUID | str) -> int | None:
    return db.query(func.max(AgentVersionModel.version)).filter(AgentVersionModel.agent_id == str(agent_id)).scalar()


def _set_archived(
    agent_id: uuid.UUID, principal: Principal, db: Session, archived: bool, force: bool = False
) -> JSONResponse:
    # eap_run_store.definitions.set_agent_archived: locked like publish and restore, 409 for a
    # built-in agent or when published workflows run it (unless force).
    _run_definition_op(
        db, lambda conn: set_agent_archived(conn, principal.tenant_id, str(agent_id), archived, force=force)
    )
    agent = _tenant_agent(db, principal.tenant_id, agent_id)
    return read_response(_agent_to_response(agent, _latest_version(db, str(agent_id))), agent.revision)


@router.post(
    "/agents/{agent_id}/archive",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}, 409: {"description": "Published workflows run this agent"}},
)
def archive_agent(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
    force: bool = False,
) -> dict:
    """Archive the agent (idempotent; 200 with the agent, `archived: true`). New runs,
    conversations, turns and publishes are then refused with 409; existing data stays readable.
    409 `{detail, workflows: [{workflow_id, workflow_name, versions, latest_version}]}` when a
    published version of a workflow runs it as a step, unless `force=true` (those workflows' runs
    are then refused). See eap_api.agent_archive."""
    return _set_archived(agent_id, principal, db, True, force)


@router.post(
    "/agents/{agent_id}/unarchive",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}},
)
def unarchive_agent(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Unarchive the agent (idempotent; 200 with the agent, `archived: false`)."""
    return _set_archived(agent_id, principal, db, False)


# The publish-time policy and warnings live in eap_run_store.agent_checks, shared with the Architect's
# gated tools; the names below keep their old home for the API's users (run_options, workflows).
def _check_publishable(db: Session, tenant_id: str, spec: AgentSpec, *, draft_run: bool = False) -> None:
    """Publish-time policy (see eap_run_store.agent_checks.check_publishable), applied by publish and
    restore and by a draft test run (`draft_run`, run_options._draft_target). A refusal is a
    DefinitionError, which the app answers with the same status and body as before."""
    check_publishable(db.connection(), tenant_id, spec, draft_run=draft_run)


def _run_definition_op(db: Session, op: Callable[[Connection], Any]) -> Any:
    """Run one shared definition operation (eap_run_store.definitions) in the request's transaction:
    commit on success, roll back (releasing the row locks) on a refusal."""
    try:
        result = op(db.connection())
    except BaseException:
        db.rollback()
        raise
    db.commit()
    return result


def _tenant_agent(db: Session, tenant_id: str, agent_id: uuid.UUID, *, lock: bool = False) -> AgentModel:
    """The tenant's agent or 404 (another tenant's agent is reported exactly like an unknown one)."""
    query = db.query(AgentModel).filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id)
    if lock:
        # FOR NO KEY UPDATE: conflicts with itself (publish and restore run one after the other) but
        # not with the KEY SHARE locks foreign-key checks take, so run inserts referencing the agent
        # are not blocked for the length of a publish or restore.
        query = query.with_for_update(key_share=True)
    agent = query.first()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


def _latest_version(db: Session, agent_id: str) -> AgentVersionModel | None:
    return (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == agent_id)
        .order_by(AgentVersionModel.version.desc())
        .first()
    )


def _draft_hash(draft_spec: dict | None) -> str | None:
    """Canonical hash of a stored draft, or None when it no longer validates."""
    return draft_hash(draft_spec)


@router.post(
    "/agents/{agent_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def publish_version(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Publish the agent's draft as a new version.

    201 with the version and `warnings`: publish-time advice that does not block the publish,
    `[{code, path, message}]` (`cost_cap_unpriced_model`: `limits.max_cost_usd` is set and a model
    of the chain has no price; `redaction_may_break_output_schema`: an output processor redacts
    while the output schema fixes string values). 422 for a spec the platform refuses, naming the
    field (`fallback_models[i]: ...` for a fallback model off the allowlist or one that refuses the
    spec's temperature). The work is eap_run_store.definitions.publish_agent, shared with the
    Architect's publish tool."""
    result = _run_definition_op(
        db,
        lambda conn: publish_agent(
            conn, principal.tenant_id, str(agent_id), created_by=principal.user_id,
            after_lock=lambda: _latest_version(db, str(agent_id)),
        ),
    )
    return stamp(
        JSONResponse(content={**_version_dict_to_response(result.version), "warnings": result.warnings}, status_code=201),
        result.revision,
    )


@router.get(
    "/agents/{agent_id}/versions",
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}},
)
def list_versions(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> list[dict]:
    """List versions for an agent (newest first)."""
    agent = (
        db.query(AgentModel)
        .filter(
            AgentModel.id == str(agent_id),
            AgentModel.tenant_id == principal.tenant_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    versions = (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == str(agent_id))
        .order_by(AgentVersionModel.version.desc())
        .all()
    )

    return JSONResponse(content=[_version_to_response(v) for v in versions])


_DRAFT = "draft"


def _parse_side(name: str, value: str) -> int | str:
    """A compare side: a version number (>= 1) or the literal `draft`."""
    if value == _DRAFT:
        return _DRAFT
    if value.isascii() and value.isdigit() and len(value) <= 9 and int(value) >= 1:
        return int(value)
    raise HTTPException(status_code=422, detail=f"{name} must be a version number or 'draft'")


def _compare_side(db: Session, agent: AgentModel, side: int | str) -> dict:
    if side == _DRAFT:
        if agent.draft_spec is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent has no draft spec")
        return {
            "version": _DRAFT,
            "spec": agent.draft_spec,
            "spec_hash": _draft_hash(agent.draft_spec),
            "created_at": None,
            "created_by": None,
            "restored_from": None,
        }
    version = (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == str(agent.id), AgentVersionModel.version == side)
        .first()
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Version {side} not found")
    return {
        "version": version.version,
        "spec": version.spec,
        "spec_hash": version.spec_hash,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
        "restored_from": version.restored_from,
    }


@router.get(
    "/agents/{agent_id}/versions/compare",
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def compare_versions(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
    base: Annotated[str, Query(max_length=16)],
    target: Annotated[str, Query(max_length=16)],
) -> dict:
    """Compare two versions, or a version and the draft (`base`/`target` = number or `draft`).

    `changes` is the field-level change list over the specs as today's AgentSpec dumps them
    (`[{path, kind, before, after}]`, kind added/removed/changed), `instructions_diff` a line diff
    of the instructions, and `instructions_diff_truncated` true when the instructions were too long
    for a line diff (more than ~2,000 lines on a side) and the diff is one whole-block replace.
    Read-only.
    """
    agent = _tenant_agent(db, principal.tenant_id, agent_id)
    base_side = _compare_side(db, agent, _parse_side("base", base))
    target_side = _compare_side(db, agent, _parse_side("target", target))
    diff = compare_specs(base_side["spec"], target_side["spec"])
    hashes = (base_side["spec_hash"], target_side["spec_hash"])
    identical = hashes[0] == hashes[1] if None not in hashes else not diff["changes"]
    return JSONResponse(
        content={
            "agent_id": str(agent.id),
            "base": base_side,
            "target": target_side,
            "identical": identical,
            **diff,
        }
    )


@router.post(
    "/agents/{agent_id}/versions/{version_num}/restore",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def restore_version(
    agent_id: uuid.UUID,
    version_num: int,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
    request: Annotated[VersionRestore | None, Body()] = None,
) -> dict:
    """Roll back by publishing version `latest + 1` whose spec equals version `version_num`.

    History is never rewritten: no version row is updated or deleted. The new version carries
    `restored_from = version_num` and the same `spec_hash`, and passes the same checks as a publish
    under today's policy. The draft becomes the restored spec. 409 when the source already is (or
    equals) the latest version, when the draft has unpublished changes and `discard_draft` is not
    set, or when a concurrent publish or restore won the version number. The response carries
    `warnings` as publish does. The work is eap_run_store.definitions.restore_agent_version.
    """
    discard_draft = request.discard_draft if request is not None else False
    result = _run_definition_op(
        db,
        lambda conn: restore_agent_version(
            conn, principal.tenant_id, str(agent_id), version_num, created_by=principal.user_id,
            discard_draft=discard_draft, after_lock=lambda: _latest_version(db, str(agent_id)),
        ),
    )
    return stamp(
        JSONResponse(content={**_version_dict_to_response(result.version), "warnings": result.warnings}, status_code=201),
        result.revision,
    )


@router.get(
    "/agents/{agent_id}/versions/{version_num}",
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}},
)
def get_version(
    agent_id: uuid.UUID,
    version_num: int,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Get a specific version by number."""
    agent = (
        db.query(AgentModel)
        .filter(
            AgentModel.id == str(agent_id),
            AgentModel.tenant_id == principal.tenant_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    version = (
        db.query(AgentVersionModel)
        .filter(
            AgentVersionModel.agent_id == str(agent_id),
            AgentVersionModel.version == version_num,
        )
        .first()
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Version not found",
        )

    return JSONResponse(content=_version_to_response(version))
