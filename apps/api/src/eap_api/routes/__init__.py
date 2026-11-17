"""Route definitions for the control-plane API."""

import logging
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import uuid

from eap_api.agent_archive import referencing_workflows, refuse_if_archived
from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.mcp_servers.models import McpServerModel
from eap_api.models import AgentModel, AgentVersionModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import AgentCreate, AgentUpdate, ErrorDetail, PublishWarning, VersionRestore
from eap_api.version_diff import compare_specs
from eap_observability import log_failure
from eap_pricing import (
    OVERRIDE_ONLY_PROVIDERS,
    PRICE_UNPRICED,
    PRICE_ZERO,
    InvalidPriceFile,
    load_overrides,
    price_known,
)
from eap_processors import processor_errors
from eap_run_store import KnowledgeStore
from eap_schemas import AgentSpec, canonical_spec_hash, reserved_tool_name_conflicts
from eap_schemas.embedding_policy import EmbeddingModelNotAllowed, EmbeddingModelPolicy
from eap_schemas.tools import HIDDEN_TOOL_DECLARATIONS
from eap_schemas.models_policy import ModelPolicy
from eap_schemas.processors import processor_models
from eap_schemas.model_settings_support import unsupported_settings
from eap_schemas.request_context import placeholder_errors

# Body cap, UTF-8, depth, unstorable strings and compact 422s: eap_api.request_guard.
log = logging.getLogger("eap_api")

router = APIRouter(route_class=GuardedJsonRoute)

SessionDep = Annotated[Session, Depends(get_session)]

# Tool ids of the Architect's management tools: valid registry ids, but only an agent whose row is
# `builtin` may list them (the check lives here, on create and patch; the worker re-checks per run).
ARCHITECT_TOOL_PREFIX = "architect."
TOOL_NOT_ALLOWED = "tool_not_allowed"
BUILTIN_AGENT = "builtin_agent"


def _refuse_architect_tools(spec: AgentSpec, builtin: bool) -> JSONResponse | None:
    """422 `tool_not_allowed` when a non-builtin agent's spec lists an `architect.*` tool."""
    if builtin:
        return None
    denied = [t for t in spec.tools if t.startswith(ARCHITECT_TOOL_PREFIX)]
    if not denied:
        return None
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": f"tools {', '.join(denied)} are reserved for built-in agents",
            "code": TOOL_NOT_ALLOWED,
            "tools": denied,
        },
    )


def _agent_to_response(agent: AgentModel, latest_version: int | None) -> dict:
    """Convert AgentModel to response dict."""
    return {
        "id": str(agent.id),
        "name": agent.name,
        "draft_spec": agent.draft_spec,
        "latest_version": latest_version,
        "archived": bool(agent.archived),
        "builtin": bool(agent.builtin),
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
    responses={422: {"model": ErrorDetail}},
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

    return JSONResponse(
        content={
            "id": str(agent.id),
            "name": agent.name,
            "draft_spec": agent.draft_spec,
            "latest_version": None,
            "archived": False,
            "builtin": False,
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
    archived: bool = False,
) -> list[dict]:
    """List the current tenant's agents: the active ones, or with `?archived=true` the archived
    ones only (see eap_api.agent_archive)."""
    agents = (
        db.query(AgentModel)
        .filter(AgentModel.tenant_id == principal.tenant_id, AgentModel.archived.is_(archived))
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

    refused = _refuse_architect_tools(request.spec, builtin=bool(agent.builtin))
    if refused is not None:
        return refused
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


def _set_archived(
    agent_id: uuid.UUID, principal: Principal, db: Session, archived: bool, force: bool = False
) -> JSONResponse:
    # Locked like publish and restore: a publish that already holds the row finishes first, and one
    # that starts after this commit sees the new flag.
    agent = _tenant_agent(db, principal.tenant_id, agent_id, lock=True)
    if archived and agent.builtin:
        db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "a built-in agent cannot be archived", "code": BUILTIN_AGENT},
        )
    if archived and not agent.archived and not force:
        workflows = referencing_workflows(db, principal.tenant_id, agent.id)
        if workflows:
            db.rollback()
            names = ", ".join(w["workflow_name"] for w in workflows[:5]) + (" …" if len(workflows) > 5 else "")
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "detail": (
                        f"agent is a step of {len(workflows)} published workflow(s) ({names}); their runs "
                        "would be refused. Remove the step, or archive with force=true"
                    ),
                    "workflows": workflows,
                },
            )
    if agent.archived != archived:
        agent.archived = archived
        db.commit()
        db.refresh(agent)
    else:
        db.rollback()
    latest = (
        db.query(func.max(AgentVersionModel.version))
        .filter(AgentVersionModel.agent_id == str(agent_id))
        .scalar()
    )
    return JSONResponse(content=_agent_to_response(agent, latest))


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


def _check_publishable(db: Session, tenant_id: str, spec: AgentSpec, *, draft_run: bool = False) -> None:
    """Publish-time policy, applied by publish and by restore (a restore is re-validated under
    today's policy, not the one in force when the source version was published), and by a draft
    test run (`draft_run`, run_options._draft_target). 422 on the first failure.

    A draft run skips only the knowledge base existence check: a draft whose knowledge bases were
    deleted (DELETE /knowledge-bases/{id} lists draft references and needs force=true) runs like a
    published version that attaches one, and its retrieve tool answers "The attached knowledge bases
    no longer exist." The worker resolves knowledge bases within the run's tenant only, so an id of
    another tenant still finds nothing."""
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

    # Current Anthropic models (Claude Opus 5.5 and later) reject sampling parameters with a 400, and
    # a provider can lack a setting altogether (Anthropic: penalties and seed; OpenAI: top_k), so a
    # spec that sets one would fail on every run: refuse it at publish instead.
    for name in unsupported_settings(spec.model.provider, spec.model_settings):
        raise HTTPException(
            status_code=422,
            detail=(
                f"model '{spec.model.provider}:{spec.model.name}' does not accept a {name} "
                f"setting; remove {name} from the agent's model settings"
            ),
        )

    _check_fallback_models(policy, spec)

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
    processor_problems = processor_errors(
        spec.input_processors, spec.step_processors, spec.stream_processors, spec.output_processors
    )
    if processor_problems:
        raise HTTPException(status_code=422, detail=processor_problems[0])

    # {{context.<name>}} placeholders must be well formed and name a declared context field.
    errors = placeholder_errors(spec.instructions, spec.context_fields)
    if errors:
        raise HTTPException(status_code=422, detail=f"instructions: {errors[0]}")

    _check_mcp_servers(db, tenant_id, spec)
    _check_platform_tools(spec)
    if not draft_run:
        _check_knowledge_bases(db, tenant_id, spec)


def _check_fallback_models(policy: ModelPolicy, spec: AgentSpec) -> None:
    """Every fallback model passes the chat allowlist (EAP_ALLOWED_MODELS), and the Anthropic
    temperature rule covers the whole chain: a fallback answers with the spec's model settings.
    AgentSpec already checks that fallbacks are distinct and none is the primary model. The worker
    re-checks the allowlist at run start and in the resolver. 422 naming `fallback_models[i]`."""
    for i, ref in enumerate(spec.fallback_models):
        if not policy.allows(ref.provider, ref.name):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"fallback_models[{i}]: model '{ref.provider}:{ref.name}' is not allowed on this platform "
                    "(not in the EAP_ALLOWED_MODELS allowlist)"
                ),
            )
        for name in unsupported_settings(ref.provider, spec.model_settings):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"fallback_models[{i}]: model '{ref.provider}:{ref.name}' does not accept a {name} "
                    f"setting; remove model_settings.{name} or this fallback model"
                ),
            )


# Publish warnings (agent capabilities milestone, AC.7): advice in the publish and restore
# responses, never a refusal. Each is {code, path, message}.
WARNING_COST_CAP_UNPRICED = "cost_cap_unpriced_model"
WARNING_REDACTION_FIXED_VALUES = "redaction_may_break_output_schema"


# _price_status: what the worker's price lookup (eap_pricing) says about one model.
PRICE_UNUSABLE = "unusable"  # the override file is unreadable, invalid, or named by the environment but missing


def _price_status(model_id: str, today: date) -> str:
    """How the worker would price a request of `model_id` today: PRICE_UNUSABLE when the override
    file (EAP_MODEL_PRICES_FILE) cannot be used, so the worker prices nothing at all; otherwise
    eap_pricing.price_known's "priced", "zero" or "unpriced". The same lookup as the worker's
    price() (eap_pricing.price_request: override file, then genai-prices for direct providers), so
    the publish warning and the recorded cost can never disagree."""
    overrides = load_overrides()
    if isinstance(overrides, InvalidPriceFile):
        return PRICE_UNUSABLE
    return price_known(model_id, today, overrides)


def cost_cap_zero_reason(provider: str, name: str, day: date) -> str | None:
    """Why requests of `provider:name` count as $0 toward a cost cap on `day` (no usable price
    file, a $0 price, or no price at all), None when the worker prices them above $0. The shared
    reason of the agent and workflow publish warnings `cost_cap_unpriced_model`."""
    model_id = f"{provider}:{name}"
    status_ = _price_status(model_id, day)
    if status_ == PRICE_UNUSABLE:
        return (
            "the model price override file is unreadable, invalid or missing, so no model has a price; "
            "its requests count as $0"
        )
    if status_ == PRICE_ZERO:
        return f"'{model_id}' is priced at $0, so its requests count as $0"
    if status_ == PRICE_UNPRICED:
        where = (
            "the model price override file"
            if provider in OVERRIDE_ONLY_PROVIDERS
            else "the model price override file or genai-prices"
        )
        return f"'{model_id}' has no price in {where}; its requests count as $0"
    return None


def _cost_cap_warnings(spec: AgentSpec, today: date | None = None) -> list[dict[str, str]]:
    """With `limits.max_cost_usd`, one warning per model of the chain (primary, then fallbacks)
    whose requests will count as $0 toward the cap: recorded without a price, or priced at $0 (the
    repository's default price file prices `test:*` and `litellm:*` at 0).

    The lookup is the worker's (`_price_status`): a model with neither an override entry nor a
    genai-prices price (genai-prices is never consulted for `test` and `litellm`) is unpriced, a
    genai-priced model is not warned about, and an unusable override file prices nothing, so then
    every model of the chain is warned about."""
    if spec.limits is None or spec.limits.max_cost_usd is None:
        return []
    day = today or datetime.now(timezone.utc).date()
    chain = [("model", spec.model)] + [(f"fallback_models[{i}]", ref) for i, ref in enumerate(spec.fallback_models)]
    warnings: list[dict[str, str]] = []
    for path, ref in chain:
        reason = cost_cap_zero_reason(ref.provider, ref.name, day)
        if reason is None:
            continue
        warnings.append(PublishWarning(
            code=WARNING_COST_CAP_UNPRICED,
            path=path,
            message=f"{path}: {reason} toward limits.max_cost_usd, so the cost limit may not stop the run",
        ).model_dump())
    return warnings


def _fixed_string_paths(schema: Any, path: str = "") -> list[str]:
    """JSON paths (into the output object, `items[]` for array items) of every schema node that
    fixes a string value through `enum` or `const`."""
    if not isinstance(schema, dict):
        return []
    found: list[str] = []
    values = list(schema.get("enum") or []) + ([schema["const"]] if "const" in schema else [])
    if any(isinstance(v, str) for v in values):
        found.append(path or "$")
    for name, sub in (schema.get("properties") or {}).items():
        found += _fixed_string_paths(sub, f"{path}.{name}" if path else name)
    if isinstance(schema.get("items"), dict):
        found += _fixed_string_paths(schema["items"], f"{path}[]" if path else "[]")
    return found


def _redaction_warnings(spec: AgentSpec) -> list[dict[str, str]]:
    """With structured output, an output processor that redacts (`pii_redact` or `regex_blocklist`
    with action `redact`) rewrites string fields; a redacted `enum` or `const` string no longer
    validates, and the run then ends `output_blocked` (schema_invalid_after_processing)."""
    if spec.output is None:
        return []
    fields = _fixed_string_paths(spec.output.json_schema)
    if not fields:
        return []
    warnings: list[dict[str, str]] = []
    for i, processor in enumerate(spec.output_processors):
        if processor.type not in ("pii_redact", "regex_blocklist") or processor.config.action != "redact":
            continue
        path = f"output_processors[{i}]"
        warnings.append(PublishWarning(
            code=WARNING_REDACTION_FIXED_VALUES,
            path=path,
            message=(
                f"{path}: {processor.type} redacts output text, and the output schema fixes string values "
                f"({', '.join(fields[:5])}); a redacted value fails the schema and the run ends output_blocked"
            ),
        ).model_dump())
    return warnings


def publish_warnings(spec: AgentSpec) -> list[dict[str, str]]:
    """Publish-time advice for a spec that passed _check_publishable (publish and restore)."""
    return _cost_cap_warnings(spec) + _redaction_warnings(spec)


def _check_memory_features(spec: AgentSpec) -> None:
    """Memory's embedding models (semantic recall, RM.14) must pass the embedding allowlist
    (EAP_ALLOWED_EMBEDDING_MODELS) with known dimensions: 422 naming the first that does not, 503
    when the allowlist itself is misconfigured. The worker re-checks before every embedding call
    and records a failed `embedding` event instead of failing the turn."""
    memory = spec.memory
    if memory is None:
        return
    models = memory.embedding_models()
    if not models:
        return
    try:
        policy = EmbeddingModelPolicy.from_env()
    except ValueError as exc:  # checked at startup; the environment changed under the process
        log_failure(log, "embedding model allowlist invalid", exc, code="embedding_policy_invalid", level=logging.ERROR)
        raise HTTPException(
            status_code=503,
            detail="the embedding model allowlist (EAP_ALLOWED_EMBEDDING_MODELS) is misconfigured",
        ) from None
    for model_id in models:
        try:
            policy.resolve(model_id)
        except EmbeddingModelNotAllowed as exc:
            raise HTTPException(status_code=422, detail=f"memory.semantic_recall: {exc}") from None


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
    base deleted after publish (or from a draft, whose test runs skip this check) gives the run's
    retrieve tool a model-visible error instead."""
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
    """Publish the agent's draft as a new version.

    201 with the version and `warnings`: publish-time advice that does not block the publish,
    `[{code, path, message}]` (`cost_cap_unpriced_model`: `limits.max_cost_usd` is set and a model
    of the chain has no price; `redaction_may_break_output_schema`: an output processor redacts
    while the output schema fixes string values). 422 for a spec the platform refuses, naming the
    field (`fallback_models[i]: ...` for a fallback model off the allowlist or one that refuses the
    spec's temperature)."""
    # Lock the agent row before reading the draft and the latest version, like restore does: a
    # publish and a restore (which may discard the draft) then run one after the other, so a draft a
    # restore discarded can never be published, and "no changes since latest" is judged on the state
    # the other request committed.
    agent = _tenant_agent(db, principal.tenant_id, agent_id, lock=True)
    refuse_if_archived(agent)

    if agent.draft_spec is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent has no draft spec",
        )

    latest_version = _latest_version(db, str(agent_id))
    latest = latest_version.version if latest_version is not None else None

    spec = validate_stored_draft(agent.draft_spec)

    _check_publishable(db, principal.tenant_id, spec)
    warnings = publish_warnings(spec)

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

    return JSONResponse(content={**_version_to_response(version), "warnings": warnings}, status_code=201)


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
    `warnings` as publish does.
    """
    discard_draft = request.discard_draft if request is not None else False
    # Lock the agent row: a concurrent draft edit cannot slip between the dirty-draft check and the
    # draft overwrite below.
    agent = _tenant_agent(db, principal.tenant_id, agent_id, lock=True)
    refuse_if_archived(agent)

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

    return JSONResponse(
        content={**_version_to_response(restored), "warnings": publish_warnings(spec)}, status_code=201
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
