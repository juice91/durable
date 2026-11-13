"""Route definitions for the control-plane API."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import uuid

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.mcp_servers.models import McpServerModel
from eap_api.models import AgentModel, AgentVersionModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import AgentCreate, AgentUpdate, ErrorDetail, VersionRestore
from eap_api.version_diff import compare_specs
from eap_processors import processor_errors
from eap_run_store import KnowledgeStore
from eap_schemas import AgentSpec, canonical_spec_hash, reserved_tool_name_conflicts
from eap_schemas.tools import HIDDEN_TOOL_DECLARATIONS
from eap_schemas.models_policy import ModelPolicy
from eap_schemas.processors import processor_models
from eap_schemas.request_context import placeholder_errors

# Body cap, UTF-8, depth, unstorable strings and compact 422s: eap_api.request_guard.
router = APIRouter(route_class=GuardedJsonRoute)

SessionDep = Annotated[Session, Depends(get_session)]


def _agent_to_response(agent: AgentModel, latest_version: int | None) -> dict:
    """Convert AgentModel to response dict."""
    return {
        "id": str(agent.id),
        "name": agent.name,
        "draft_spec": agent.draft_spec,
        "latest_version": latest_version,
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
    }


@router.post(
    "/agents",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    tags=["agents"],
    responses={422: {"model": ErrorDetail}},
)
def create_agent(
    request: AgentCreate,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Create a new agent."""
    spec_dict = request.spec.model_dump(mode="json")

    agent = AgentModel(
        tenant_id=principal.tenant_id,
        name=request.spec.name,
        draft_spec=spec_dict,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    return JSONResponse(
        content={
            "id": str(agent.id),
            "name": agent.name,
            "draft_spec": agent.draft_spec,
            "latest_version": None,
        },
        status_code=201,
    )


@router.get(
    "/agents",
    response_class=JSONResponse,
    tags=["agents"],
)
def list_agents(
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> list[dict]:
    """List agents for the current tenant (non-archived)."""
    agents = (
        db.query(AgentModel)
        .filter(AgentModel.tenant_id == principal.tenant_id, AgentModel.archived == False)
        .all()
    )

    results = []
    for agent in agents:
        latest = (
            db.query(func.max(AgentVersionModel.version))
            .filter(AgentVersionModel.agent_id == agent.id)
            .scalar()
        )
        results.append(_agent_to_response(agent, latest))

    return JSONResponse(content=results)


@router.get(
    "/agents/{agent_id}",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}},
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

    latest = (
        db.query(func.max(AgentVersionModel.version))
        .filter(AgentVersionModel.agent_id == str(agent_id))
        .scalar()
    )

    return JSONResponse(content=_agent_to_response(agent, latest))


@router.patch(
    "/agents/{agent_id}",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def patch_agent(
    agent_id: uuid.UUID,
    request: AgentUpdate,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Update an agent's draft spec."""
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

    spec_dict = request.spec.model_dump(mode="json")

    agent.name = request.spec.name
    agent.draft_spec = spec_dict
    db.commit()
    db.refresh(agent)

    latest = (
        db.query(func.max(AgentVersionModel.version))
        .filter(AgentVersionModel.agent_id == str(agent_id))
        .scalar()
    )

    return JSONResponse(content=_agent_to_response(agent, latest))


def _check_mcp_servers(db: Session, tenant_id: str, spec: AgentSpec) -> None:
    """Every MCP tool must name an enabled MCP server of this tenant (422 naming the first that
    does not). Another tenant's server is reported exactly like an unknown one. Whether the tool
    exists on the server is checked by the worker at run time, since the server can change."""
    names = list(dict.fromkeys(ref.server for ref in spec.mcp_tools))
    if not names:
        return
    enabled = dict(
        db.query(McpServerModel.name, McpServerModel.enabled)
        .filter(McpServerModel.tenant_id == tenant_id, McpServerModel.name.in_(names))
        .all()
    )
    for name in names:
        if name not in enabled:
            raise HTTPException(status_code=422, detail=f"MCP server '{name}' is not registered in this tenant")
        if not enabled[name]:
            raise HTTPException(status_code=422, detail=f"MCP server '{name}' is disabled")


def _check_publishable(db: Session, tenant_id: str, spec: AgentSpec) -> None:
    """Publish-time policy, applied by publish and by restore (a restore is re-validated under
    today's policy, not the one in force when the source version was published). 422 on the first
    failure."""
    _check_memory_features(spec)

    policy = ModelPolicy.from_env()
    # Model governance: only allowlisted models (EAP_ALLOWED_MODELS) can be published. The worker
    # re-checks before every model call, since the policy can change after publish.
    if not policy.allows(spec.model.provider, spec.model.name):
        raise HTTPException(
            status_code=422,
            detail=(
                f"model '{spec.model.provider}:{spec.model.name}' is not allowed on this platform "
                "(not in the EAP_ALLOWED_MODELS allowlist)"
            ),
        )

    # Current Anthropic models (Claude Opus 5.5 and later) reject sampling parameters with a 400,
    # so a spec that sets temperature would fail on every run: refuse it at publish instead.
    if spec.model.provider == "anthropic" and spec.model_settings.temperature is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"model '{spec.model.provider}:{spec.model.name}' does not accept a temperature "
                "setting; remove temperature from the agent's model settings"
            ),
        )

    # Processors that call a model (moderation) are governed by the same allowlist.
    for ref in processor_models(spec.input_processors + spec.output_processors):
        if not policy.allows(ref.provider, ref.name):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"processor model '{ref.provider}:{ref.name}' is not allowed on this platform "
                    "(not in the EAP_ALLOWED_MODELS allowlist)"
                ),
            )

    # Memory's model calls (the observational memory observer, RM.13) are governed by the same chat
    # allowlist; the worker re-checks it at call time and records a memory_observe_failed event.
    if spec.memory is not None:
        for ref in spec.memory.chat_models():
            if not policy.allows(ref.provider, ref.name):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"observer model '{ref.provider}:{ref.name}' is not allowed on this platform "
                        "(not in the EAP_ALLOWED_MODELS allowlist)"
                    ),
                )

    # Regex blocklist patterns must compile with RE2 (no backreferences or lookarounds, bounded
    # size, not matching empty text); eap_schemas only bounds their count and length.
    processor_problems = processor_errors(spec.input_processors, spec.output_processors)
    if processor_problems:
        raise HTTPException(status_code=422, detail=processor_problems[0])

    # {{context.<name>}} placeholders must be well formed and name a declared context field.
    errors = placeholder_errors(spec.instructions, spec.context_fields)
    if errors:
        raise HTTPException(status_code=422, detail=f"instructions: {errors[0]}")

    _check_mcp_servers(db, tenant_id, spec)
    _check_platform_tools(spec)
    _check_knowledge_bases(db, tenant_id, spec)


# Memory features the spec can describe (RM.2) but the worker does not run yet. Until RM.14
# (semantic recall) lands, a spec that sets one would publish and then silently do nothing, so
# publish, restore and draft runs refuse it. RM.14 removes its entry here (the plan's task sections
# say so); RM.13 removed observational memory's.
UNAVAILABLE_MEMORY_FEATURES: tuple[tuple[str, str], ...] = (
    ("semantic_recall", "semantic recall is not available yet"),
)


def _check_memory_features(spec: AgentSpec) -> None:
    """422 when the spec sets a memory feature that is not implemented yet (memory.semantic_recall);
    remove it from the agent's memory settings to publish."""
    memory = spec.memory
    if memory is None:
        return
    for field, message in UNAVAILABLE_MEMORY_FEATURES:
        if getattr(memory, field) is not None:
            raise HTTPException(
                status_code=422, detail=f"{message}: remove memory.{field} from the agent's memory settings"
            )


def _check_platform_tools(spec: AgentSpec) -> None:
    """No built-in or MCP tool may take a reserved platform tool name (`retrieve`,
    `update_working_memory`), checked for every spec (AgentSpec itself checks only specs that use
    knowledge or memory, so stored specs keep validating). Hidden platform tools (knowledge.search)
    are for tool runs only and never part of an agent."""
    conflicts = reserved_tool_name_conflicts(spec)
    if conflicts:
        raise HTTPException(
            status_code=422, detail=f"tool name '{conflicts[0]}' is reserved for a platform tool"
        )
    for tool_id in spec.tools:
        if tool_id in HIDDEN_TOOL_DECLARATIONS:
            raise HTTPException(
                status_code=422, detail=f"tool '{tool_id}' is a platform tool and cannot be added to an agent"
            )


def _check_knowledge_bases(db: Session, tenant_id: str, spec: AgentSpec) -> None:
    """Every attached knowledge base must exist in the principal's tenant (422 naming the first that
    does not). Another tenant's knowledge base is reported exactly like an unknown one. A knowledge
    base deleted after publish gives the run's retrieve tool a model-visible error instead."""
    ids = [ref.id for ref in spec.knowledge_bases]
    if not ids:
        return
    existing = KnowledgeStore(db.get_bind()).existing_knowledge_base_ids(tenant_id, ids)
    for kb_id in ids:
        if kb_id not in existing:
            raise HTTPException(status_code=422, detail=f"knowledge base '{kb_id}' does not exist in this tenant")


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
    if draft_spec is None:
        return None
    try:
        return canonical_spec_hash(AgentSpec.model_validate(draft_spec))
    except ValidationError:
        return None


def validate_stored_draft(draft_spec: dict) -> AgentSpec:
    """The agent's stored draft as an AgentSpec, or 422 when it no longer validates (e.g. a row
    written under older rules). Shared by publish and draft runs (run_options._draft_target) so the
    two answers cannot drift. The detail names the first error's location and Pydantic message; such
    a message may quote a value from the draft itself, which only the draft's own tenant sees."""
    try:
        return AgentSpec.model_validate(draft_spec)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "invalid"}
        where = ".".join(str(p) for p in first.get("loc", ())) or "spec"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The draft is not a valid agent spec ({where}: {first.get('msg', 'invalid')}); fix it in the editor",
        ) from None


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
    """Publish the agent's draft as a new version."""
    # Lock the agent row before reading the draft and the latest version, like restore does: a
    # publish and a restore (which may discard the draft) then run one after the other, so a draft a
    # restore discarded can never be published, and "no changes since latest" is judged on the state
    # the other request committed.
    agent = _tenant_agent(db, principal.tenant_id, agent_id, lock=True)

    if agent.draft_spec is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent has no draft spec",
        )

    latest_version = _latest_version(db, str(agent_id))
    latest = latest_version.version if latest_version is not None else None

    spec = validate_stored_draft(agent.draft_spec)

    _check_publishable(db, principal.tenant_id, spec)

    draft_hash = canonical_spec_hash(spec)

    if latest_version is not None and latest_version.spec_hash == draft_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"no changes since version {latest}",
        )

    new_version_num = (latest or 0) + 1
    version = AgentVersionModel(
        agent_id=str(agent_id),
        tenant_id=principal.tenant_id,
        version=new_version_num,
        spec=spec.model_dump(mode="json"),
        spec_hash=draft_hash,
        created_by=principal.user_id,
    )
    db.add(version)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="concurrent publish, retry",
        )
    db.refresh(version)

    return JSONResponse(content=_version_to_response(version), status_code=201)


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
    set, or when a concurrent publish or restore won the version number.
    """
    discard_draft = request.discard_draft if request is not None else False
    # Lock the agent row: a concurrent draft edit cannot slip between the dirty-draft check and the
    # draft overwrite below.
    agent = _tenant_agent(db, principal.tenant_id, agent_id, lock=True)

    source = (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == str(agent_id), AgentVersionModel.version == version_num)
        .first()
    )
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")

    latest = _latest_version(db, str(agent_id))
    assert latest is not None  # the source exists
    if latest.version == source.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"version {version_num} is already the latest version",
        )
    if latest.spec_hash == source.spec_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"version {version_num} has the same spec as the latest version {latest.version}",
        )

    if (
        not discard_draft
        and agent.draft_spec is not None
        and _draft_hash(agent.draft_spec) != latest.spec_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"the draft has unpublished changes since version {latest.version}; publish them, "
                "or restore with discard_draft: true to replace the draft"
            ),
        )

    try:
        spec = AgentSpec.model_validate(source.spec)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "spec"
        raise HTTPException(
            status_code=422,
            detail=f"version {version_num} is no longer a valid agent spec ({where}: {first.get('msg')})",
        ) from None
    _check_publishable(db, principal.tenant_id, spec)

    restored = AgentVersionModel(
        agent_id=str(agent_id),
        tenant_id=principal.tenant_id,
        version=latest.version + 1,
        spec=dict(source.spec),
        spec_hash=source.spec_hash,
        created_by=principal.user_id,
        restored_from=source.version,
    )
    db.add(restored)
    agent.draft_spec = dict(source.spec)
    agent.name = spec.name
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="concurrent publish, retry",
        )
    db.refresh(restored)

    return JSONResponse(content=_version_to_response(restored), status_code=201)


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
