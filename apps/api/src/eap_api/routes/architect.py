"""The Architect: seed, options, draft and page save (Task 5 of the Architect milestone).

- PUT  /architect/model      set the model of the three built-in agents and publish them.
- POST /architect            seed the tenant's three built-in agents (idempotent): 201 when it made one, else 200.
- GET  /architect            seed state plus the agents and workflows the Architect made.
- GET  /architect/options    the allowlist the page's pickers show (what a created agent may use).
- POST /architect/draft      the form's "Fill in the form": one drafter run, its structured answer
                             filtered through the allowlist.
- POST /architect/draft-workflow  the same for the workflow form (the built-in workflow drafter).
- POST /architect/agents     save (and optionally publish) an agent from the restricted format.
- POST /architect/workflows  the same for a workflow.

Ids on this surface are the Architect's slugs (`^[a-z][a-z0-9-]{1,47}$`); a row id appears only as
`agent_id` / `workflow_id` in answers, for links into the Agents and Workflows pages.

The allowlist is computed here from the API's own sources (tool declarations, model discovery, the
tenant's MCP servers and knowledge bases) with the rule the worker applies to its registry
(`eap_schemas.architect.offer_tool`), and `compile_agent` / `compile_workflow` validate every save
against it, so the page cannot grant what the options route did not offer.

Publishing on the page is the signed-in user's own decision: it calls the shared
eap_run_store.definitions publish with `created_by` = the user and `created_via` = "architect-page".
A refused publish saves nothing (one transaction).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from eap_api import model_discovery
from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.models import model_items
from eap_api.routes.runs import StoresDep, start_run
from eap_api.schemas import RunCreate
from eap_run_store import definitions
from eap_run_store.agent_checks import draft_hash
from eap_run_store.definition_errors import DefinitionError, refuse
from eap_run_store.store import TERMINAL_RUN_STATUSES
from eap_run_store.workflow_checks import allowed_tool_ids
from eap_schemas import AgentSpec
from eap_schemas.architect import (
    AGENT_ID_PATTERN,
    RESERVED_AGENT_IDS,
    Allowlist,
    AllowlistAgent,
    AllowlistKnowledgeBase,
    AllowlistMcpServer,
    AllowlistModel,
    ArchitectAgentSpec,
    ArchitectWorkflowSpec,
    as_uuid,
    compile_agent,
    compile_workflow,
    offer_tool,
    slugify,
)
from eap_schemas.architect_seed import (
    ARCHITECT_SLUG,
    DRAFTER_SLUG,
    WORKFLOW_DRAFTER_SLUG,
    architect_agent_spec,
    drafter_agent_spec,
    filter_draft,
    filter_workflow_draft,
    gated_approvals_missing,
    normalize_workflow_draft,
    pick_seed_model,
    workflow_drafter_agent_spec,
)
from eap_schemas.models_policy import ModelPolicy
from eap_schemas.scorer_suggestions import SUGGESTER_SLUG
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_schemas.workflows import WorkflowSpec, canonical_workflow_hash

log = logging.getLogger("eap_api.architect")

router = APIRouter(prefix="/architect", tags=["architect"], route_class=GuardedJsonRoute)

PrincipalDep = Annotated[Principal, Depends(current_principal)]
SessionDep = Annotated[Session, Depends(get_session)]

# The drafter run is awaited at most this long (seconds); the run itself keeps its own limits.
DRAFT_TIMEOUT_S = 60.0
DRAFT_POLL_S = 0.2
MCP_DISCOVERY_TIMEOUT_S = 12.0
PAGE_MARKER = "architect-page"
BUILTIN_SLUGS = (ARCHITECT_SLUG, DRAFTER_SLUG, WORKFLOW_DRAFTER_SLUG)
MAX_DESCRIPTION_CHARS = 8000
MAX_RAW_CHARS = 8000


class DraftFailed(DefinitionError):
    status_code = 502


class DraftTimeout(DefinitionError):
    status_code = 504


# --------------------------------------------------------------------------------------- models


def _providers() -> list[dict[str, Any]]:
    return [p.public() for p in model_discovery.all_provider_statuses() if p.configured]


def seed_model() -> str | None:
    """The model of the built-in agents: the best-ranked model of the providers with status ok that the
    platform allowlist permits (ranking: eap_schemas.architect_seed.pick_seed_model), else the first
    concrete model the allowlist names. The platform
    has no per-tenant default model, so there is no tenant setting to consult first."""
    policy = ModelPolicy.from_env()
    provider_models = []
    for status in model_discovery.all_provider_statuses():
        names = [m.name for m in status.models if policy.allows(status.id, m.name)]  # a catalogue counts too
        if names:
            provider_models.append((status.id, names))
    fallback = [item["id"] for item in model_items(policy) if not item["wildcard"]]
    return pick_seed_model(provider_models, fallback)


def _choose_model() -> DefinitionError:
    return refuse(
        409, "Choose a model for the Architect first: no model is available.", code="choose_model",
        providers=_providers(),
    )


# ---------------------------------------------------------------------------------------- seed

_BY_SLUG = text(
    "SELECT id, name, draft_spec, archived, builtin, slug FROM agents WHERE tenant_id = :t AND slug = :s"
)


def _spec_model(spec: dict[str, Any] | None) -> str | None:
    model = (spec or {}).get("model") or {}
    return f"{model['provider']}:{model['name']}" if model.get("provider") and model.get("name") else None


def _seed_one(conn: Connection, tenant_id: str, user_id: str, slug: str, name: str, build: Any, model: list[str]):
    """Create the built-in agent `slug` when missing (and publish its first version); when it exists keep
    the tenant's edits and only add the gated approvals entries it lacks. Returns (row, created)."""
    row = conn.execute(_BY_SLUG, {"t": tenant_id, "s": slug}).first()
    if row is None:
        if not model:
            chosen = seed_model()
            if chosen is None:
                raise _choose_model()
            model.append(chosen)
        spec = build(model[0])
        inserted = conn.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, draft_spec, archived, builtin, slug, created_at, updated_at)"
                " VALUES (:id, :t, :n, CAST(:spec AS jsonb), false, true, :s, now(), now())"
                " ON CONFLICT (tenant_id, slug) WHERE slug IS NOT NULL DO NOTHING RETURNING id"
            ),
            {"id": str(uuid.uuid4()), "t": tenant_id, "n": name, "spec": json.dumps(spec.model_dump(mode="json")), "s": slug},
        ).first()
        if inserted is not None:
            definitions.publish_agent(conn, tenant_id, str(inserted.id), created_by=user_id)
            return conn.execute(_BY_SLUG, {"t": tenant_id, "s": slug}).one(), True
        return conn.execute(_BY_SLUG, {"t": tenant_id, "s": slug}).one(), False  # a concurrent seed won
    missing = gated_approvals_missing(row.draft_spec or {})
    if missing:
        locked = definitions.lock_agent(conn, tenant_id, str(row.id))
        spec = dict(locked.draft_spec or {})
        latest = definitions.latest_agent_version(conn, str(row.id))
        clean = latest is not None and draft_hash(spec) == latest.spec_hash  # no unpublished edits
        approvals = dict(spec.get("approvals") or {})
        approvals["tools"] = [*(approvals.get("tools") or []), *missing]
        spec["approvals"] = approvals
        conn.execute(
            text("UPDATE agents SET draft_spec = CAST(:s AS jsonb), updated_at = now() WHERE id = :i AND tenant_id = :t"),
            {"s": json.dumps(spec), "i": str(row.id), "t": tenant_id},
        )
        if clean:
            try:
                with conn.begin_nested():
                    definitions.publish_agent(conn, tenant_id, str(row.id), created_by=user_id)
            except DefinitionError:
                log.warning("architect seed: repaired draft could not be published tenant=%s slug=%s", tenant_id, slug)
        row = conn.execute(_BY_SLUG, {"t": tenant_id, "s": slug}).one()
    return row, False


def ensure_seed(engine: Engine, tenant_id: str, user_id: str) -> tuple[dict[str, Any], bool]:
    """The tenant's built-in agents, created when missing. `({architect_agent_id, drafter_agent_id,
    workflow_drafter_agent_id, model}, created)`; raises the 409 `choose_model` refusal when a row is missing and no model exists."""
    chosen: list[str] = []
    with engine.begin() as conn:
        architect, made_a = _seed_one(conn, tenant_id, user_id, ARCHITECT_SLUG, "Architect", architect_agent_spec, chosen)
        drafter, made_d = _seed_one(conn, tenant_id, user_id, DRAFTER_SLUG, "Architect drafter", drafter_agent_spec, chosen)
        wdrafter, made_w = _seed_one(
            conn, tenant_id, user_id, WORKFLOW_DRAFTER_SLUG, "Architect workflow drafter", workflow_drafter_agent_spec, chosen
        )
    return {
        "architect_agent_id": str(architect.id),
        "drafter_agent_id": str(drafter.id),
        "workflow_drafter_agent_id": str(wdrafter.id),
        "model": _spec_model(architect.draft_spec),
    }, made_a or made_d or made_w


@router.post("", responses={409: {"description": "choose_model"}})
def seed(request: Request, principal: PrincipalDep) -> JSONResponse:
    """Seed the tenant's Architect and its two drafters (idempotent). 201 `{architect_agent_id,
    drafter_agent_id, workflow_drafter_agent_id, model}` when a row was created, 200 with the same shape otherwise (the tenant's edits are kept).
    409 `{detail, code: "choose_model", providers}` when no model is available."""
    body, created = ensure_seed(request.app.state.database_engine, principal.tenant_id, principal.user_id)
    return JSONResponse(content=body, status_code=201 if created else 200)


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=3, max_length=200, pattern=r"^[^:\s]+:\S+$")  # "provider:name"


@router.put(
    "/model",
    responses={404: {"description": "not_seeded"}, 422: {"description": "model_not_allowed"}},
)
def set_model(body: ModelRequest, request: Request, principal: PrincipalDep) -> JSONResponse:
    """Set the model of the tenant's three built-in agents (architect, architect-drafter,
    architect-workflow-drafter): each row's draft gets the new model (nothing else in the draft changes,
    hand-edited instructions stay) and is published as the signed-in user (`created_via`
    "architect-page"), in one transaction. A row whose draft already equals its latest version after the
    change publishes nothing. 200 `{model, agent_ids: [..]}`; 404 `not_seeded`; 422 `model_not_allowed`
    when the platform allowlist or provider discovery does not list the model."""
    model_id = body.model
    if model_id not in {i["id"] for i in model_items(ModelPolicy.from_env()) if not i["wildcard"]}:
        raise refuse(422, f"The model {model_id!r} is not available.", code="model_not_allowed")
    provider, _, name = model_id.partition(":")
    tenant_id = principal.tenant_id
    engine: Engine = request.app.state.database_engine
    ids: list[str] = []
    with engine.begin() as conn:
        rows = [conn.execute(_BY_SLUG, {"t": tenant_id, "s": slug}).first() for slug in BUILTIN_SLUGS]
        if any(r is None for r in rows):
            raise refuse(404, "The Architect is not set up yet.", code="not_seeded")
        suggester = conn.execute(_BY_SLUG, {"t": tenant_id, "s": SUGGESTER_SLUG}).first()  # seeded on first use
        for row in [*rows, *([suggester] if suggester is not None else [])]:
            locked = definitions.lock_agent(conn, tenant_id, str(row.id))
            spec = dict(locked.draft_spec or {})
            spec["model"] = {**(spec.get("model") or {}), "provider": provider, "name": name}
            conn.execute(
                text("UPDATE agents SET draft_spec = CAST(:s AS jsonb), updated_at = now() WHERE id = :i AND tenant_id = :t"),
                {"s": json.dumps(spec), "i": str(row.id), "t": tenant_id},
            )
            latest = definitions.latest_agent_version(conn, str(row.id))
            if latest is None or draft_hash(spec) != latest.spec_hash:
                definitions.publish_agent(conn, tenant_id, str(row.id), created_by=principal.user_id, created_via=PAGE_MARKER)
            ids.append(str(row.id))
    return JSONResponse(content={"model": model_id, "agent_ids": ids})


# ---------------------------------------------------------------------------------- state lists

_AGENT_LIST = text(
    "SELECT a.id, a.slug, a.name, a.archived, a.draft_spec,"
    " (SELECT count(*) FROM agent_versions v WHERE v.agent_id = a.id) AS versions,"
    " (SELECT v.created_via FROM agent_versions v WHERE v.agent_id = a.id ORDER BY v.version DESC LIMIT 1) AS via,"
    " (SELECT v.spec_hash FROM agent_versions v WHERE v.agent_id = a.id ORDER BY v.version DESC LIMIT 1) AS latest_hash"
    " FROM agents a WHERE a.tenant_id = :t AND a.slug IS NOT NULL AND NOT a.builtin ORDER BY a.slug"
)
_WORKFLOW_LIST = text(
    "SELECT w.id, w.slug, w.name, w.archived, w.draft_spec,"
    " (SELECT count(*) FROM workflow_versions v WHERE v.workflow_id = w.id AND v.tenant_id = :t) AS versions,"
    " (SELECT v.created_via FROM workflow_versions v WHERE v.workflow_id = w.id AND v.tenant_id = :t"
    "  ORDER BY v.version DESC LIMIT 1) AS via,"
    " (SELECT v.spec_hash FROM workflow_versions v WHERE v.workflow_id = w.id AND v.tenant_id = :t"
    "  ORDER BY v.version DESC LIMIT 1) AS latest_hash"
    " FROM workflows w WHERE w.tenant_id = :t AND w.slug IS NOT NULL ORDER BY w.slug"
)


def _workflow_draft_hash(draft: dict | None) -> str | None:
    if draft is None:
        return None
    try:
        return canonical_workflow_hash(WorkflowSpec.model_validate(draft))
    except ValidationError:
        return None


def _listing(conn: Connection, tenant_id: str, statement: Any, hasher: Any, id_key: str) -> list[dict[str, Any]]:
    out = []
    for row in conn.execute(statement, {"t": tenant_id}).mappings().all():
        out.append({
            "id": row["slug"],
            id_key: str(row["id"]),
            "name": row["name"],
            "versions": int(row["versions"]),
            "latest_created_via": row["via"],
            "draft": row["latest_hash"] is None or hasher(row["draft_spec"]) != row["latest_hash"],
            "archived": bool(row["archived"]),
        })
    return out


@router.get("")
def state(request: Request, principal: PrincipalDep) -> JSONResponse:
    """`{seeded, choose_model, architect_agent_id, drafter_agent_id, workflow_drafter_agent_id, model,
    agents, workflows}`.
    `seeded` is true when both built-in agents exist; `choose_model` is true when they do not and
    no model is available to seed them with. `agents` and `workflows` are the Architect-made
    definitions by id: `{id (slug), agent_id | workflow_id (the row id), name, versions,
    latest_created_via (null, "architect-page" or "architect:<run_id>"), draft (the draft differs from
    the latest version, or nothing is published), archived}`. The built-in agents are not listed."""
    engine: Engine = request.app.state.database_engine
    tenant_id = principal.tenant_id
    with engine.connect() as conn:
        built = {r.slug: r for r in conn.execute(
            text("SELECT id, slug, draft_spec FROM agents WHERE tenant_id = :t AND builtin AND slug IN (:a, :d, :w)"),
            {"t": tenant_id, "a": ARCHITECT_SLUG, "d": DRAFTER_SLUG, "w": WORKFLOW_DRAFTER_SLUG},
        )}
        agents = _listing(conn, tenant_id, _AGENT_LIST, draft_hash, "agent_id")
        workflows = _listing(conn, tenant_id, _WORKFLOW_LIST, _workflow_draft_hash, "workflow_id")
    seeded = ARCHITECT_SLUG in built and DRAFTER_SLUG in built
    architect = built.get(ARCHITECT_SLUG)
    return JSONResponse(content={
        "seeded": seeded,
        "choose_model": (not seeded) and seed_model() is None,
        "architect_agent_id": str(architect.id) if architect else None,
        "drafter_agent_id": str(built[DRAFTER_SLUG].id) if DRAFTER_SLUG in built else None,
        "workflow_drafter_agent_id": str(built[WORKFLOW_DRAFTER_SLUG].id) if WORKFLOW_DRAFTER_SLUG in built else None,
        "model": _spec_model(architect.draft_spec) if architect else None,
        "agents": agents,
        "workflows": workflows,
    })


# ----------------------------------------------------------------------------------- allowlist


async def _mcp_tools(request: Request, principal: Principal, servers: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Tool names per server name from a live `tools/list` (the server test route's own path, bounded);
    a server that does not answer offers no tools."""
    from eap_api.mcp_servers.managed import get_mcp_runtime
    from eap_api.routes.mcp_servers import get_outbound_policy, run_mcp_server_test

    overrides = request.app.dependency_overrides
    policy = overrides.get(get_outbound_policy, get_outbound_policy)()
    runtime = overrides.get(get_mcp_runtime, get_mcp_runtime)()

    async def one(name: str, server_id: str) -> tuple[str, tuple[str, ...]]:
        try:
            response = await asyncio.wait_for(
                run_mcp_server_test(uuid.UUID(server_id), request, principal, policy, runtime), MCP_DISCOVERY_TIMEOUT_S
            )
            body = json.loads(response.body)
            return name, tuple(sorted(t["name"] for t in body.get("tools", []))) if body.get("status") == "ok" else ()
        except Exception as exc:  # noqa: BLE001 - best effort: the server just offers no tools
            log.warning("architect options: mcp discovery failed %s", type(exc).__name__)
            return name, ()

    return dict(await asyncio.gather(*(one(n, i) for n, i in servers.items())))


_CREATED_AGENTS = text(
    "SELECT a.slug, a.name, a.id FROM agents a WHERE a.tenant_id = :t AND a.slug IS NOT NULL AND NOT a.archived"
    " AND NOT a.builtin ORDER BY a.slug"
)


def build_allowlist(request: Request, principal: Principal, *, mcp_servers: set[str] | None = None) -> Allowlist:
    """The tenant's allowlist (see the module docstring). `mcp_servers` limits the live MCP discovery to
    those server names (a save only needs the servers its spec names); None discovers every enabled one."""
    engine: Engine = request.app.state.database_engine
    tenant_id = principal.tenant_id
    allowed = allowed_tool_ids()
    tools = tuple(
        offered for decl in TOOL_DECLARATIONS.values()
        if (allowed is None or decl.id in allowed)
        and (offered := offer_tool(
            decl.id, decl.title, decl.description, side_effect=decl.side_effect,
            input_schema=decl.input_json_schema(), example=decl.example,
            result_example=decl.result_example, result_schema=decl.result_schema)) is not None
    )
    with engine.connect() as conn:
        servers = {
            r.name: str(r.id) for r in conn.execute(
                text("SELECT id, name FROM mcp_servers WHERE tenant_id = :t AND enabled ORDER BY name"), {"t": tenant_id}
            )
        }
        agents = tuple(
            AllowlistAgent(id=r.slug, name=r.name, row_id=str(r.id)) for r in conn.execute(_CREATED_AGENTS, {"t": tenant_id})
        )
    wanted = {n: i for n, i in servers.items() if mcp_servers is None or n in mcp_servers}
    found = anyio.from_thread.run(_mcp_tools, request, principal, wanted) if wanted else {}
    kbs = request.app.state.knowledge_store.list_knowledge_bases(tenant_id)
    items = [i for i in model_items(ModelPolicy.from_env()) if not i["wildcard"]]
    models = tuple(AllowlistModel(id=i["id"], provider=i["provider"], capabilities=i.get("capabilities") or {}) for i in items)
    default = seed_model()
    return Allowlist(
        tools=tools,
        mcp_servers=tuple(AllowlistMcpServer(id=n, name=n, tools=found.get(n, ())) for n in servers),
        knowledge_bases=tuple(AllowlistKnowledgeBase(id=str(kb["id"]), name=str(kb["name"])) for kb in kbs),
        models=models,
        agents=agents,
        default_model=default if default in {m.id for m in models} else (models[0].id if models else None),
    )


@router.get("/options")
def options(request: Request, principal: PrincipalDep) -> JSONResponse:
    """The allowlist for the page: `{tools: [{id, title, description, requires_approval}], mcp_servers:
    [{id, name, tools: [name]}], knowledge_bases: [{id, name}], models: [{id, provider, capabilities:
    {image, pdf, source}}], agents: [{id (slug), name}] (Architect-made, draft or published, not archived: what a
    workflow step may run), default_model}`. Tools exclude the platform and host tools; a tool marked
    `requires_approval` is gated when granted (the save writes the approvals entry). MCP tools of a server
    that does not answer are not listed."""
    return JSONResponse(content=build_allowlist(request, principal).model_dump(mode="json"))


# ------------------------------------------------------------------------------------------ draft


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    current: ArchitectAgentSpec | None = None


def drafter_input(description: str, current: ArchitectAgentSpec | None, allowlist: Allowlist) -> str:
    """The drafter's prompt: the description as written, then what it may grant, then the current
    definition when the person is editing one."""
    lines = [description.strip(), "", "--- Available to grant (only what the description names) ---"]
    lines.append("Tools: " + ("; ".join(f"{t.id} ({t.title})" for t in allowlist.tools) or "none"))
    mcp = [f"{s.id}/{t}" for s in allowlist.mcp_servers for t in s.tools]
    lines.append("MCP tools (server/tool): " + ("; ".join(mcp) or "none"))
    lines.append("Knowledge bases (id): " + ("; ".join(f"{kb.id} ({kb.name})" for kb in allowlist.knowledge_bases) or "none"))
    if current is not None:
        lines += ["", "--- Current definition (change only what the description asks for) ---",
                  current.model_dump_json(exclude_none=True)]
    return "\n".join(lines)


def _wait_for_run(run_store: Any, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
    deadline = time.monotonic() + DRAFT_TIMEOUT_S
    while True:
        run = run_store.get_run(tenant_id, run_id)
        if run is not None and run["status"] in TERMINAL_RUN_STATUSES:
            return run
        if time.monotonic() >= deadline:
            try:
                run_store.cancel_run_tree(tenant_id, run_id)
            except Exception:  # noqa: BLE001 - the 504 below is the answer either way
                log.warning("architect draft: could not cancel run %s", run_id)
            raise DraftTimeout("The drafter did not answer in time; try again.", code="draft_timeout")
        time.sleep(DRAFT_POLL_S)


def _invalid(raw: str, problems: list[str], what: str = "agent") -> DefinitionError:
    return refuse(422, f"The drafter's answer is not a valid {what} definition.", code="draft_invalid",
                  raw=raw[:MAX_RAW_CHARS], problems=problems)


def _run_drafter(
    drafter_agent_id: str, prompt: str, principal: Principal, db: Session, stores: Any, launcher: RunLauncher,
    what: str,
) -> tuple[dict[str, Any], list[str], str, str, str | None]:
    """One run of a built-in drafter and its answer: `(parsed object, assumptions, raw, run_id, model)`.
    The parsed object has `assumptions` removed; 502, 504 and 422 `draft_invalid` for the failures."""
    try:
        run = RunCreate(run_id=uuid.uuid4(), input=prompt)
    except ValidationError:
        raise refuse(422, "The description is too long.", code="description_too_long") from None
    started = start_run(uuid.UUID(drafter_agent_id), run, principal, db, stores, launcher)
    run_id = json.loads(started.body)["run_id"]
    finished = _wait_for_run(stores[0], principal.tenant_id, uuid.UUID(run_id))
    if finished["status"] != "completed":
        raise DraftFailed("The drafter run did not finish.", code="draft_failed", run_id=run_id,
                          error=str(finished.get("error") or finished["status"])[:300])
    raw = finished.get("output") or ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise _invalid(raw, ["the answer is not JSON"], what) from None
    if not isinstance(parsed, dict):
        raise _invalid(raw, ["the answer is not a JSON object"], what)
    listed = parsed.pop("assumptions", [])
    assumptions = [a for a in listed if isinstance(a, str)][:12] if isinstance(listed, list) else []
    return parsed, assumptions, raw, run_id, finished.get("model_id")


def _problems(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


@router.post("/draft", responses={409: {"description": "choose_model"}, 422: {"description": "draft_invalid"}})
def draft(
    body: DraftRequest,
    request: Request,
    principal: PrincipalDep,
    db: SessionDep,
    stores: StoresDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)],
) -> JSONResponse:
    """Run the drafter once (a normal agent run of the built-in `architect-drafter`, seeded on first use)
    and wait for its structured answer, at most 60 s. 200 `{spec: ArchitectAgentSpec, assumptions:
    [string], dropped: [{kind: tool | mcp_tool | knowledge_base | model | id, id}], run_id, model}`: the
    spec is cut down to what the allowlist offers (see /architect/options), `dropped` lists what was
    removed. 422 `{code: "draft_invalid", raw, problems}` when the answer is not a valid definition
    (`raw` is the text); 502 `draft_failed` when the run failed; 504 `draft_timeout`; 409 `choose_model`."""
    engine: Engine = request.app.state.database_engine
    ids, _ = ensure_seed(engine, principal.tenant_id, principal.user_id)
    allowlist = build_allowlist(request, principal)
    parsed, assumptions, raw, run_id, model = _run_drafter(
        ids["drafter_agent_id"], drafter_input(body.description, body.current, allowlist),
        principal, db, stores, launcher, "agent",
    )
    bad_id = parsed.get("id") if isinstance(parsed.get("id"), str) and not AGENT_ID_PATTERN.fullmatch(parsed["id"]) else None
    if bad_id is not None:
        parsed["id"] = None  # the schema cannot state the id rule: enforced here
    try:
        spec = ArchitectAgentSpec.model_validate(parsed)
    except ValidationError as exc:
        raise _invalid(raw, _problems(exc)) from None
    cleaned, dropped = filter_draft(spec, allowlist)
    if bad_id is not None:
        dropped.append({"kind": "id", "id": bad_id})
    return JSONResponse(content={
        "spec": cleaned.model_dump(mode="json"),
        "assumptions": assumptions,
        "dropped": dropped,
        "run_id": run_id,
        "model": model,
    })


class DraftWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    current: ArchitectWorkflowSpec | None = None


def workflow_drafter_input(description: str, current: ArchitectWorkflowSpec | None, allowlist: Allowlist) -> str:
    """The workflow drafter's prompt: the description as written, then the agents and tools the steps
    may use, then the current definition when the person is editing one."""
    lines = [description.strip(), "", "--- Available to use in steps (only these) ---"]
    lines.append("Agents (id): " + ("; ".join(f"{a.id} ({a.name})" for a in allowlist.agents) or "none"))
    lines.append("Tools (id): " + ("; ".join(f"{t.id} ({t.title})" for t in allowlist.tools) or "none"))
    if current is not None:
        lines += ["", "--- Current definition (change only what the description asks for) ---",
                  current.model_dump_json(exclude_none=True)]
    return "\n".join(lines)


@router.post(
    "/draft-workflow", responses={409: {"description": "choose_model"}, 422: {"description": "draft_invalid"}}
)
def draft_workflow(
    body: DraftWorkflowRequest,
    request: Request,
    principal: PrincipalDep,
    db: SessionDep,
    stores: StoresDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)],
) -> JSONResponse:
    """`/architect/draft` for a workflow (the built-in `architect-workflow-drafter`). 200 `{spec:
    ArchitectWorkflowSpec, assumptions: [string], dropped: [{kind: agent | tool | id, id}], run_id,
    model}`: a step that uses an agent or tool the allowlist did not offer is removed and reported (a
    parallel step keeps its remaining branches), so `spec.steps` may come back empty. 422 `draft_invalid`
    with `raw` and `problems`; 502 `draft_failed`; 504 `draft_timeout`; 409 `choose_model`. The page
    saves the result through POST /architect/workflows, which validates it again."""
    engine: Engine = request.app.state.database_engine
    ids, _ = ensure_seed(engine, principal.tenant_id, principal.user_id)
    allowlist = build_allowlist(request, principal, mcp_servers=set())
    parsed, assumptions, raw, run_id, model = _run_drafter(
        ids["workflow_drafter_agent_id"], workflow_drafter_input(body.description, body.current, allowlist),
        principal, db, stores, launcher, "workflow",
    )
    bad_id = parsed.get("id") if isinstance(parsed.get("id"), str) and not AGENT_ID_PATTERN.fullmatch(parsed["id"]) else None
    if bad_id is not None:
        parsed["id"] = None
    try:
        spec = ArchitectWorkflowSpec.model_validate(normalize_workflow_draft(parsed))
    except ValidationError as exc:
        raise _invalid(raw, _problems(exc), "workflow") from None
    cleaned, dropped = filter_workflow_draft(spec, allowlist)
    if bad_id is not None:
        dropped.append({"kind": "id", "id": bad_id})
    return JSONResponse(content={
        "spec": cleaned.model_dump(mode="json"),
        "assumptions": assumptions,
        "dropped": dropped,
        "run_id": run_id,
        "model": model,
    })


# ------------------------------------------------------------------------------------ page save


class SaveAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: ArchitectAgentSpec
    expected_version: int | None = Field(default=None, ge=1)
    publish: bool = False


class SaveWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: ArchitectWorkflowSpec
    expected_version: int | None = Field(default=None, ge=1)
    publish: bool = False


def _compile_refusal(exc: ValueError) -> DefinitionError:
    message = str(exc)
    head, sep, _ = message.partition(":")
    code = head if sep and head.replace("_", "").isalpha() and head.islower() else "invalid_spec"
    return refuse(422, message, code=code)


class _Kind:
    def __init__(self, table: str, versions: str, fk: str, builtin: str) -> None:
        self.table, self.versions, self.fk, self.label = table, versions, fk, "agent" if table == "agents" else "workflow"
        self.by_slug = text(
            f"SELECT id, slug, name, draft_spec, archived, {builtin} AS builtin FROM {table}"
            " WHERE tenant_id = :t AND slug = :s FOR NO KEY UPDATE"
        )
        # `spec.id` may be the UUID of an existing Architect-made definition (it has a slug): that one is updated.
        self.by_id = text(
            f"SELECT id, slug, name, draft_spec, archived, {builtin} AS builtin FROM {table}"
            " WHERE tenant_id = :t AND id = :s AND slug IS NOT NULL FOR NO KEY UPDATE"
        )
        self.latest = text(
            f"SELECT max(version) FROM {versions} WHERE {fk} = :d AND tenant_id = :t"
        )
        self.update = text(
            f"UPDATE {table} SET name = :name, draft_spec = CAST(:spec AS jsonb), updated_at = now()"
            " WHERE id = :id AND tenant_id = :t"
        )
        self.insert = text(
            f"INSERT INTO {table} (id, tenant_id, name, draft_spec, archived, slug, created_at, updated_at"
            f"{', builtin' if table == 'agents' else ''}) VALUES (:id, :t, :name, CAST(:spec AS jsonb), false, :s,"
            f" now(), now(){', false' if table == 'agents' else ''})"
        )
        self.taken = text(f"SELECT 1 FROM {table} WHERE tenant_id = :t AND slug = :s")


AGENTS = _Kind("agents", "agent_versions", "agent_id", "builtin")
WORKFLOWS = _Kind("workflows", "workflow_versions", "workflow_id", "false")


def _free_slug(conn: Connection, kind: _Kind, tenant_id: str, name: str) -> str:
    base = slugify(name)
    for n in range(1, 100):
        candidate = base if n == 1 else f"{base[: 47 - len(str(n))].rstrip('-')}-{n}"
        if candidate not in RESERVED_AGENT_IDS and conn.execute(kind.taken, {"t": tenant_id, "s": candidate}).first() is None:
            return candidate
    raise refuse(409, "Could not find a free id; give the definition an id.", code="id_conflict")


def _save(
    engine: Engine, principal: Principal, kind: _Kind, *, slug: str | None, name: str, spec_json: dict[str, Any],
    expected_version: int | None, publish: bool,
) -> dict[str, Any]:
    """Write the draft of one Architect definition by slug (a new row, or the Architect-made row that has
    the slug) and optionally publish it, in one transaction."""
    tenant_id = principal.tenant_id
    with engine.begin() as conn:
        if slug is None:
            slug = _free_slug(conn, kind, tenant_id, name)
            row = None
            if expected_version is not None:
                raise refuse(409, "This definition has no versions yet.", code="version_mismatch", latest_version=None)
        elif as_uuid(slug) is not None:
            row = conn.execute(kind.by_id, {"t": tenant_id, "s": as_uuid(slug)}).mappings().first()
            if row is None:
                raise refuse(404, f"No {kind.label} made by the Architect has the uuid {slug!r}.", code="not_found")
            slug = row["slug"]
        else:
            if slug in RESERVED_AGENT_IDS:
                raise refuse(422, f"{slug!r} is a built-in id; choose another id.", code="reserved_id")
            row = conn.execute(kind.by_slug, {"t": tenant_id, "s": slug}).mappings().first()
        if row is not None:
            if row["builtin"]:
                raise refuse(422, f"{slug!r} is a built-in agent; it is not edited here.", code="reserved_id")
            if row["archived"]:
                raise refuse(409, f"The {kind.label} {slug!r} is archived; unarchive it first.", code="archived")
            latest = conn.execute(kind.latest, {"d": row["id"], "t": tenant_id}).scalar()
            if latest != expected_version:
                raise refuse(
                    409, f"The {kind.label} {slug!r} is at version {latest}, not {expected_version}; reload it.",
                    code="version_mismatch", latest_version=latest,
                )
        elif expected_version is not None:
            raise refuse(409, "This definition has no versions yet.", code="version_mismatch", latest_version=None)
        params = {"t": tenant_id, "name": name, "spec": json.dumps(spec_json)}
        if row is not None:
            row_id = str(row["id"])
            conn.execute(kind.update, {**params, "id": row_id})
        else:
            row_id = str(uuid.uuid4())
            try:
                with conn.begin_nested():
                    conn.execute(kind.insert, {**params, "id": row_id, "s": slug})
            except IntegrityError:
                raise refuse(409, f"The id {slug!r} was taken a moment ago; try again.", code="id_conflict") from None
        warnings: list[dict[str, str]] = []
        if publish:
            op = definitions.publish_agent if kind is AGENTS else definitions.publish_workflow
            result = op(conn, tenant_id, row_id, created_by=principal.user_id, created_via=PAGE_MARKER)
            warnings = result.warnings
        version = conn.execute(kind.latest, {"d": row_id, "t": tenant_id}).scalar()
    return {
        "id": slug, f"{kind.label}_id": row_id, "version": version, "published": bool(publish),
        "warnings": warnings,
    }


@router.post("/agents", responses={409: {"description": "version_mismatch"}, 422: {"description": "not allowed"}})
def save_agent(body: SaveAgentRequest, request: Request, principal: PrincipalDep) -> JSONResponse:
    """Save an agent from the restricted format as the draft of the Architect-made agent with `spec.id`
    (a new one, with an id derived from the name when `spec.id` is empty), and with `publish: true` publish
    it as the signed-in user (`created_via` "architect-page"). `expected_version` is the latest version the
    page last saw (null: none); another latest version is 409 `version_mismatch` with `latest_version`.
    200 `{id, agent_id, version (latest, null when unpublished), published, warnings}`. 422 with the code
    `tool_not_allowed`, `knowledge_base_not_allowed`, `model_not_allowed`, `model_required` or `reserved_id`;
    a refused publish (422 or 409) saves nothing."""
    spec = body.spec
    allowlist = build_allowlist(request, principal, mcp_servers={r.server for r in spec.mcp_tools})
    try:
        compiled: AgentSpec = compile_agent(spec, allowlist)
    except ValueError as exc:
        raise _compile_refusal(exc) from None
    result = _save(
        request.app.state.database_engine, principal, AGENTS, slug=spec.id, name=compiled.name,
        spec_json=compiled.model_dump(mode="json"), expected_version=body.expected_version, publish=body.publish,
    )
    return JSONResponse(content=result)


@router.post("/workflows", responses={409: {"description": "version_mismatch"}, 422: {"description": "not allowed"}})
def save_workflow(body: SaveWorkflowRequest, request: Request, principal: PrincipalDep) -> JSONResponse:
    """The same for a workflow: the restricted format compiles to a full workflow spec (agent steps may
    name any Architect-made agent, draft or published; publishing needs each to have a published version, 422
    `agent_not_published`; tool steps only offered tools). 200 `{id,
    workflow_id, version, published, warnings}`; 422 `agent_not_allowed`, `tool_not_allowed`,
    `unknown_placeholder`, `workflow_invalid`, `reserved_id`; 409 `version_mismatch`."""
    spec = body.spec
    allowlist = build_allowlist(request, principal, mcp_servers=set())
    try:
        compiled = compile_workflow(spec, allowlist)
    except ValueError as exc:
        raise _compile_refusal(exc) from None
    result = _save(
        request.app.state.database_engine, principal, WORKFLOWS, slug=spec.id, name=compiled.name,
        spec_json=compiled.model_dump(mode="json"), expected_version=body.expected_version, publish=body.publish,
    )
    return JSONResponse(content=result)
