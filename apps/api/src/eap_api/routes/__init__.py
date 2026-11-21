"""Route definitions for the control-plane API."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, status
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import AgentCreate, AgentUpdate, ErrorDetail, VersionRestore
from eap_api.service_deps.agents import AgentServiceDep
from eap_api.services.agents import (  # noqa: F401 - re-exported: the API's users and tests import them from here
    DRAFT,
    PRICE_UNUSABLE,
    WARNING_COST_CAP_UNPRICED,
    WARNING_REDACTION_FIXED_VALUES,
    ToolRefusal,
    _cost_cap_warnings,
    _fixed_string_paths,
    _price_status,
    cost_cap_zero_reason,
    draft_hash,
    publish_warnings,
    validate_stored_draft,
)
from fastapi import Depends

# Body cap, UTF-8, depth, unstorable strings and compact 422s: eap_api.request_guard.
log = logging.getLogger("eap_api")

router = APIRouter(route_class=GuardedJsonRoute)

PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _refusal_response(refusal: ToolRefusal) -> JSONResponse:
    """The 422 `{detail, code, tools}` a spec with a tool its agent may not list answers."""
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=refusal.detail_object)


@router.post(
    "/agents",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    tags=["agents"],
    responses={422: {"model": ErrorDetail}, **etag_responses(success=201, write=False)},
)
def create_agent(request: AgentCreate, principal: PrincipalDep, service: AgentServiceDep) -> dict:
    """Create a new agent."""
    try:
        agent, revision = service.create(principal.tenant_id, request.spec)
    except ToolRefusal as refusal:
        return _refusal_response(refusal)
    return stamp(JSONResponse(content=agent, status_code=201), revision)


@router.get(
    "/agents",
    response_class=JSONResponse,
    tags=["agents"],
)
def list_agents(
    principal: PrincipalDep,
    service: AgentServiceDep,
    archived: bool = False,
    include_system: bool = False,
) -> list[dict]:
    """List the current tenant's agents: the active ones, or with `?archived=true` the archived
    ones only (see eap_api.agent_archive). The Architect's drafter helpers (`builtin_role ==
    "drafter"`) are left out unless `?include_system=true`; they stay reachable by id."""
    return JSONResponse(content=service.list(principal.tenant_id, archived=archived, include_system=include_system))


@router.get(
    "/agents/{agent_id}",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}, **etag_responses(write=False)},
)
def get_agent(agent_id: uuid.UUID, principal: PrincipalDep, service: AgentServiceDep) -> dict:
    """Get a specific agent by ID."""
    agent, revision = service.get(principal.tenant_id, agent_id)
    return read_response(agent, revision)


@router.patch(
    "/agents/{agent_id}",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, **etag_responses()},
)
def patch_agent(
    agent_id: uuid.UUID,
    request: AgentUpdate,
    principal: PrincipalDep,
    service: AgentServiceDep,
    pre: IfMatch,
) -> dict:
    """Update an agent's draft spec. Send the ETag you read as `If-Match`: a stale one is 412
    `revision_mismatch` with the current agent (see eap_api.concurrency)."""
    try:
        agent, revision = service.patch(principal.tenant_id, agent_id, request.spec, pre.expected)
    except ToolRefusal as refusal:
        return _refusal_response(refusal)
    return pre.respond(agent, revision)


@router.post(
    "/agents/{agent_id}/archive",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}, 409: {"description": "Published workflows run this agent"}},
)
def archive_agent(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    service: AgentServiceDep,
    force: bool = False,
) -> dict:
    """Archive the agent (idempotent; 200 with the agent, `archived: true`). New runs,
    conversations, turns and publishes are then refused with 409; existing data stays readable.
    409 `{detail, workflows: [{workflow_id, workflow_name, versions, latest_version}]}` when a
    published version of a workflow runs it as a step, unless `force=true` (those workflows' runs
    are then refused). See eap_api.agent_archive."""
    agent, revision = service.set_archived(principal.tenant_id, agent_id, True, force)
    return read_response(agent, revision)


@router.post(
    "/agents/{agent_id}/unarchive",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}},
)
def unarchive_agent(agent_id: uuid.UUID, principal: PrincipalDep, service: AgentServiceDep) -> dict:
    """Unarchive the agent (idempotent; 200 with the agent, `archived: false`)."""
    agent, revision = service.set_archived(principal.tenant_id, agent_id, False)
    return read_response(agent, revision)


@router.post(
    "/agents/{agent_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def publish_version(agent_id: uuid.UUID, principal: PrincipalDep, service: AgentServiceDep) -> dict:
    """Publish the agent's draft as a new version.

    201 with the version and `warnings`: publish-time advice that does not block the publish,
    `[{code, path, message}]` (`cost_cap_unpriced_model`: `limits.max_cost_usd` is set and a model
    of the chain has no price; `redaction_may_break_output_schema`: an output processor redacts
    while the output schema fixes string values). 422 for a spec the platform refuses, naming the
    field (`fallback_models[i]: ...` for a fallback model off the allowlist or one that refuses the
    spec's temperature). The work is the shared definition operation `publish_agent`, shared with the
    Architect's publish tool."""
    version, revision = service.publish(principal.tenant_id, principal.user_id, agent_id)
    return stamp(JSONResponse(content=version, status_code=201), revision)


@router.get(
    "/agents/{agent_id}/versions",
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}},
)
def list_versions(agent_id: uuid.UUID, principal: PrincipalDep, service: AgentServiceDep) -> list[dict]:
    """List versions for an agent (newest first)."""
    return JSONResponse(content=service.list_versions(principal.tenant_id, agent_id))


def _parse_side(name: str, value: str) -> int | str:
    """A compare side: a version number (>= 1) or the literal `draft`."""
    if value == DRAFT:
        return DRAFT
    if value.isascii() and value.isdigit() and len(value) <= 9 and int(value) >= 1:
        return int(value)
    raise HTTPException(status_code=422, detail=f"{name} must be a version number or 'draft'")


@router.get(
    "/agents/{agent_id}/versions/compare",
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def compare_versions(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    service: AgentServiceDep,
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
    # The tenant's agent is looked up first: another tenant's agent is a 404 before a bad side is a 422.
    service.tenant_agent(principal.tenant_id, agent_id)
    return JSONResponse(
        content=service.compare(
            principal.tenant_id, agent_id, _parse_side("base", base), _parse_side("target", target)
        )
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
    principal: PrincipalDep,
    service: AgentServiceDep,
    request: Annotated[VersionRestore | None, Body()] = None,
) -> dict:
    """Roll back by publishing version `latest + 1` whose spec equals version `version_num`.

    History is never rewritten: no version row is updated or deleted. The new version carries
    `restored_from = version_num` and the same `spec_hash`, and passes the same checks as a publish
    under today's policy. The draft becomes the restored spec. 409 when the source already is (or
    equals) the latest version, when the draft has unpublished changes and `discard_draft` is not
    set, or when a concurrent publish or restore won the version number. The response carries
    `warnings` as publish does. The work is the shared definition operation `restore_agent_version`.
    """
    discard_draft = request.discard_draft if request is not None else False
    version, revision = service.restore(principal.tenant_id, principal.user_id, agent_id, version_num, discard_draft)
    return stamp(JSONResponse(content=version, status_code=201), revision)


@router.get(
    "/agents/{agent_id}/versions/{version_num}",
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}},
)
def get_version(agent_id: uuid.UUID, version_num: int, principal: PrincipalDep, service: AgentServiceDep) -> dict:
    """Get a specific version by number."""
    return JSONResponse(content=service.get_version(principal.tenant_id, agent_id, version_num))
