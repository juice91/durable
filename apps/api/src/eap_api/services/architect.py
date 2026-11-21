"""The Architect: seeding of the built-in agents, the allowlist, the drafter runs and the page save.

The business logic behind eap_api.routes.architect (and the seeding and drafter runs that the suggested-rules
and workflow-explanation routes share):

- seed the tenant's built-in agents (idempotent), repair them, republish them at startup;
- set the model of the built-in agents;
- list what the Architect made;
- compute the allowlist (what a created agent may use) from the API's own sources;
- run a drafter agent once and read its structured answer, filtered through the allowlist;
- save (and optionally publish) an agent or workflow from the restricted format.

Ids on this surface are the Architect's slugs (`^[a-z][a-z0-9-]{1,47}$`); a row id appears only as
`agent_id` / `workflow_id` in answers.

Publishing on the page is the signed-in user's own decision: it calls the shared
eap_run_store.definitions publish with `created_by` = the user and `created_via` = "architect-page".
A refused publish saves nothing (one transaction). Running an agent goes through the `RunStarter` port
(services.runs.RunService).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import anyio
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from eap_api import model_discovery
from eap_api.ports import RunStarter, RunWaitTimeout
from eap_api.schemas import RunCreate
from eap_api.services.errors import Conflict, InvalidInput, NotFound
from eap_api.services.mcp_connection import McpConnectionService
from eap_api.services.model_catalog import model_items
from eap_run_store import definitions
from eap_run_store.agent_checks import draft_hash
from eap_run_store.definition_errors import DefinitionError, refuse
from eap_run_store.knowledge import KnowledgeStore
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
from eap_schemas.scorer_suggestions import SUGGESTER_NAME, SUGGESTER_SLUG, suggester_agent_spec
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_schemas.workflow_explanations import EXPLAINER_NAME, EXPLAINER_SLUG, explainer_agent_spec
from eap_schemas.workflows import WorkflowSpec, canonical_workflow_hash

log = logging.getLogger("eap_api.architect")

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
    the tenant's edits and only add the built-in tool ids (and gated approvals entries) the current seed
    declares and the draft lacks: published when the draft had no unpublished edits, draft-only otherwise. Returns (row, created)."""
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
    stored = row.draft_spec or {}
    stored_tools = set(stored.get("tools") or [])
    # Built-in tool ids: whatever the current seed spec declares (the model does not change the tools).
    new_tools = [t for t in build(_spec_model(stored) or "seed:tools").tools if t not in stored_tools]
    stored_tools |= set(new_tools)
    missing = gated_approvals_missing({**stored, "tools": list(stored_tools)})
    if new_tools or missing:
        locked = definitions.lock_agent(conn, tenant_id, str(row.id))
        spec = dict(locked.draft_spec or {})
        latest = definitions.latest_agent_version(conn, str(row.id))
        clean = latest is not None and draft_hash(spec) == latest.spec_hash  # no unpublished edits
        have = set(spec.get("tools") or [])
        spec["tools"] = [*(spec.get("tools") or []), *(t for t in new_tools if t not in have)]  # never removes
        missing = gated_approvals_missing(spec)
        if missing:
            approvals = dict(spec.get("approvals") or {})
            approvals["tools"] = [*(approvals.get("tools") or []), *missing]
            spec["approvals"] = approvals
        conn.execute(
            text("UPDATE agents SET draft_spec = CAST(:s AS jsonb), updated_at = now(), revision = revision + 1"
                " WHERE id = :i AND tenant_id = :t"),
            {"s": json.dumps(spec), "i": str(row.id), "t": tenant_id},
        )
        if clean:
            try:
                with conn.begin_nested():
                    definitions.publish_agent(conn, tenant_id, str(row.id), created_by=user_id)
            except DefinitionError:
                log.warning("architect seed: repaired draft could not be published tenant=%s slug=%s", tenant_id, slug)
        else:
            log.info(
                "architect seed: repaired the draft only (unpublished edits) tenant=%s slug=%s tools=%s approvals=%s",
                tenant_id, slug, new_tools, missing,
            )
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


_SEEDED = (
    (ARCHITECT_SLUG, architect_agent_spec),
    (DRAFTER_SLUG, drafter_agent_spec),
    (WORKFLOW_DRAFTER_SLUG, workflow_drafter_agent_spec),
    (EXPLAINER_SLUG, explainer_agent_spec),
)


def _republish_seed(conn: Connection, tenant_id: str, slug: str, build: Any) -> bool:
    """Bring one seeded agent's published text up to the current seed. The target is the seed spec with
    the agent's own model, plus any tools (and approvals entries) the tenant added. Skipped when the draft
    has unpublished edits (a human or the Architect changed it) or the latest version already equals the
    target. Returns True when a new version was published (created_via "seed")."""
    row = conn.execute(_BY_SLUG, {"t": tenant_id, "s": slug}).first()
    if row is None or row.archived:
        return False
    locked = definitions.lock_agent(conn, tenant_id, str(row.id))
    latest = definitions.latest_agent_version(conn, str(row.id))
    draft = dict(locked.draft_spec or {})
    model = _spec_model(draft)
    if latest is None or model is None:
        return False
    if draft_hash(draft) != latest.spec_hash:
        log.info("architect seed: draft has unpublished edits, seed text not republished tenant=%s slug=%s", tenant_id, slug)
        return False
    target = build(model).model_dump(mode="json")
    extra = [t for t in (draft.get("tools") or []) if t not in target["tools"]]
    target["tools"] = [*target["tools"], *extra]
    approvals = dict(target.get("approvals") or {})
    seeded = list(approvals.get("tools") or [])
    extra_approved = [t for t in ((draft.get("approvals") or {}).get("tools") or []) if t not in seeded]
    if extra_approved:
        target["approvals"] = {**approvals, "tools": [*seeded, *extra_approved]}
    if draft_hash(target) == latest.spec_hash:
        return False
    conn.execute(
        text("UPDATE agents SET draft_spec = CAST(:s AS jsonb), updated_at = now(), revision = revision + 1"
             " WHERE id = :i AND tenant_id = :t"),
        {"s": json.dumps(target), "i": str(row.id), "t": tenant_id},
    )
    try:
        with conn.begin_nested():
            definitions.publish_agent(conn, tenant_id, str(row.id), created_by="system:seed", created_via="seed")
    except DefinitionError:
        log.warning("architect seed: seed text could not be published tenant=%s slug=%s", tenant_id, slug, exc_info=True)
        raise
    return True


def reconcile_all_seeds(engine: Engine) -> int:
    """At startup: run `ensure_seed` for every tenant that already has an Architect row (new built-in tool
    ids reach existing tenants), then republish each seeded agent whose latest version differs from the
    current seed text (model and tenant-added tools kept; agents with unpublished draft edits skipped).
    Never raises (a tenant that fails is logged and skipped). Returns the number of tenants reconciled;
    the log line also counts the republished agents."""
    with engine.connect() as conn:
        tenants = [r.tenant_id for r in conn.execute(text("SELECT DISTINCT tenant_id FROM agents WHERE slug = :s AND builtin"), {"s": ARCHITECT_SLUG})]
    done = 0
    republished = 0
    for tenant in tenants:
        try:
            ensure_seed(engine, tenant, "system:seed")
            with engine.begin() as conn:  # the workflow explainer: a tenant seeded before it existed gets it here
                _seed_one(conn, tenant, "system:seed", EXPLAINER_SLUG, EXPLAINER_NAME, explainer_agent_spec, [])
            done += 1
        except Exception:
            log.warning("architect seed: startup reconcile failed tenant=%s", tenant, exc_info=True)
            continue
        for slug, build in _SEEDED:
            try:
                with engine.begin() as conn:
                    republished += _republish_seed(conn, tenant, slug, build)
            except Exception:
                log.warning("architect seed: republish failed tenant=%s slug=%s", tenant, slug, exc_info=True)
    log.info("architect seed: reconciled %d tenants, republished %d seeded agents", done, republished)
    return done


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
_CREATED_AGENTS = text(
    "SELECT a.slug, a.name, a.id FROM agents a WHERE a.tenant_id = :t AND a.slug IS NOT NULL AND NOT a.archived"
    " AND NOT a.builtin ORDER BY a.slug"
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


# ------------------------------------------------------------------------------------------ draft


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


def _invalid(raw: str, problems: list[str], what: str = "agent") -> DefinitionError:
    return refuse(422, f"The drafter's answer is not a valid {what} definition.", code="draft_invalid",
                  raw=raw[:MAX_RAW_CHARS], problems=problems)


def _problems(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


# ------------------------------------------------------------------------------------ page save


def _compile_refusal(exc: ValueError) -> InvalidInput:
    message = str(exc)
    head, sep, _ = message.partition(":")
    code = head if sep and head.replace("_", "").isalpha() and head.islower() else "invalid_spec"
    return InvalidInput(message, code=code)


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
            f"UPDATE {table} SET name = :name, draft_spec = CAST(:spec AS jsonb), updated_at = now(),"
            " revision = revision + 1"
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
    base = slugify(name, kind.label)  # type: ignore[arg-type]
    for n in range(1, 100):
        candidate = base if n == 1 else f"{base[: 47 - len(str(n))].rstrip('-')}-{n}"
        if candidate not in RESERVED_AGENT_IDS and conn.execute(kind.taken, {"t": tenant_id, "s": candidate}).first() is None:
            return candidate
    raise Conflict("Could not find a free id; give the definition an id.", code="id_conflict")


def _no_versions() -> DefinitionError:
    return refuse(409, "This definition has no versions yet.", code="version_mismatch", latest_version=None)


# -------------------------------------------------------------------------------------- service


class ArchitectService:
    """The Architect's operations. Collaborators come through the constructor: the engine (the Architect's
    definitions are written in one transaction), the knowledge store (the allowlist's knowledge bases), the
    run starter (drafter runs) and the MCP connection service (live tool discovery for the allowlist)."""

    def __init__(
        self, engine: Engine, knowledge: KnowledgeStore, runs: RunStarter, mcp: McpConnectionService | None = None
    ) -> None:
        self.engine = engine
        self.knowledge = knowledge
        self.runs = runs
        self.mcp = mcp

    # ---------------------------------------------------------------------------------- seeding

    def ensure_seed(self, tenant_id: str, user_id: str) -> tuple[dict[str, Any], bool]:
        """The tenant's built-in agents (see the module function `ensure_seed`)."""
        return ensure_seed(self.engine, tenant_id, user_id)

    def seed_model(self) -> str | None:
        return seed_model()

    def ensure_suggester(self, tenant_id: str, user_id: str) -> tuple[str, str | None]:
        """`(the tenant's scorer-suggester agent id, its model id)`, seeding the agent when missing. Raises
        the `choose_model` DefinitionError when a row is missing and no model exists."""
        with self.engine.begin() as conn:
            row, _ = _seed_one(conn, tenant_id, user_id, SUGGESTER_SLUG, SUGGESTER_NAME, suggester_agent_spec, [])
        return str(row.id), _spec_model(row.draft_spec) or seed_model()

    def ensure_explainer(self, tenant_id: str, user_id: str) -> str:
        """The tenant's workflow-explainer agent id (seeded when missing); 409 `no_model` without a model."""
        try:
            with self.engine.begin() as conn:
                row, _ = _seed_one(conn, tenant_id, user_id, EXPLAINER_SLUG, EXPLAINER_NAME, explainer_agent_spec, [])
        except DefinitionError as exc:
            if exc.code == "choose_model":
                raise refuse(409, "No model is available for the workflow explainer.", code="no_model", **exc.extra) from None
            raise
        if _spec_model(row.draft_spec) is None and seed_model() is None:
            raise refuse(409, "No model is available for the workflow explainer.", code="no_model")
        return str(row.id)

    def set_model(self, tenant_id: str, user_id: str, model_id: str) -> dict[str, Any]:
        """Set the model of the tenant's three built-in agents (and the helpers that exist): each row's draft
        gets the new model and is published as the user (`created_via` "architect-page"), in one transaction.
        A row whose draft already equals its latest version after the change publishes nothing."""
        if model_id not in {i["id"] for i in model_items(ModelPolicy.from_env()) if not i["wildcard"]}:
            raise InvalidInput(f"The model {model_id!r} is not available.", code="model_not_allowed")
        provider, _, name = model_id.partition(":")
        ids: list[str] = []
        with self.engine.begin() as conn:
            rows = [conn.execute(_BY_SLUG, {"t": tenant_id, "s": slug}).first() for slug in BUILTIN_SLUGS]
            if any(r is None for r in rows):
                raise NotFound("The Architect is not set up yet.", code="not_seeded")
            helpers = [conn.execute(_BY_SLUG, {"t": tenant_id, "s": slug}).first() for slug in (SUGGESTER_SLUG, EXPLAINER_SLUG)]
            for row in [*rows, *(h for h in helpers if h is not None)]:  # the helpers are seeded on first use
                locked = definitions.lock_agent(conn, tenant_id, str(row.id))
                spec = dict(locked.draft_spec or {})
                spec["model"] = {**(spec.get("model") or {}), "provider": provider, "name": name}
                conn.execute(
                    text("UPDATE agents SET draft_spec = CAST(:s AS jsonb), updated_at = now(), revision = revision + 1"
                    " WHERE id = :i AND tenant_id = :t"),
                    {"s": json.dumps(spec), "i": str(row.id), "t": tenant_id},
                )
                latest = definitions.latest_agent_version(conn, str(row.id))
                if latest is None or draft_hash(spec) != latest.spec_hash:
                    definitions.publish_agent(conn, tenant_id, str(row.id), created_by=user_id, created_via=PAGE_MARKER)
                ids.append(str(row.id))
        return {"model": model_id, "agent_ids": ids}

    # ------------------------------------------------------------------------------------ state

    def state(self, tenant_id: str) -> dict[str, Any]:
        """Seed state plus the agents and workflows the Architect made (see the route's docstring)."""
        with self.engine.connect() as conn:
            built = {r.slug: r for r in conn.execute(
                text("SELECT id, slug, draft_spec FROM agents WHERE tenant_id = :t AND builtin AND slug IN (:a, :d, :w)"),
                {"t": tenant_id, "a": ARCHITECT_SLUG, "d": DRAFTER_SLUG, "w": WORKFLOW_DRAFTER_SLUG},
            )}
            agents = _listing(conn, tenant_id, _AGENT_LIST, draft_hash, "agent_id")
            workflows = _listing(conn, tenant_id, _WORKFLOW_LIST, _workflow_draft_hash, "workflow_id")
        seeded = ARCHITECT_SLUG in built and DRAFTER_SLUG in built
        architect = built.get(ARCHITECT_SLUG)
        return {
            "seeded": seeded,
            "choose_model": (not seeded) and seed_model() is None,
            "architect_agent_id": str(architect.id) if architect else None,
            "drafter_agent_id": str(built[DRAFTER_SLUG].id) if DRAFTER_SLUG in built else None,
            "workflow_drafter_agent_id": str(built[WORKFLOW_DRAFTER_SLUG].id) if WORKFLOW_DRAFTER_SLUG in built else None,
            "model": _spec_model(architect.draft_spec) if architect else None,
            "agents": agents,
            "workflows": workflows,
        }

    # -------------------------------------------------------------------------------- allowlist

    async def _mcp_tools(self, tenant_id: str, servers: dict[str, str]) -> dict[str, tuple[str, ...]]:
        """Tool names per server name from a live `tools/list` (the server test's own path, bounded);
        a server that does not answer offers no tools."""

        async def one(name: str, server_id: str) -> tuple[str, tuple[str, ...]]:
            try:
                assert self.mcp is not None
                body = await asyncio.wait_for(self.mcp.test(tenant_id, uuid.UUID(server_id)), MCP_DISCOVERY_TIMEOUT_S)
                return name, tuple(sorted(t["name"] for t in body.get("tools", []))) if body.get("status") == "ok" else ()
            except Exception as exc:  # noqa: BLE001 - best effort: the server just offers no tools
                log.warning("architect options: mcp discovery failed %s", type(exc).__name__)
                return name, ()

        return dict(await asyncio.gather(*(one(n, i) for n, i in servers.items())))

    def build_allowlist(self, tenant_id: str, *, mcp_servers: set[str] | None = None) -> Allowlist:
        """The tenant's allowlist: what a created agent may use. `mcp_servers` limits the live MCP discovery
        to those server names (a save only needs the servers its spec names); None discovers every enabled one.
        Called from a worker thread (a sync route), never from the event loop."""
        allowed = allowed_tool_ids()
        tools = tuple(
            offered for decl in TOOL_DECLARATIONS.values()
            if (allowed is None or decl.id in allowed)
            and (offered := offer_tool(
                decl.id, decl.title, decl.description, side_effect=decl.side_effect,
                input_schema=decl.input_json_schema(), example=decl.example,
                result_example=decl.result_example, result_schema=decl.result_schema)) is not None
        )
        with self.engine.connect() as conn:
            servers = {
                r.name: str(r.id) for r in conn.execute(
                    text("SELECT id, name FROM mcp_servers WHERE tenant_id = :t AND enabled ORDER BY name"), {"t": tenant_id}
                )
            }
            agents = tuple(
                AllowlistAgent(id=r.slug, name=r.name, row_id=str(r.id)) for r in conn.execute(_CREATED_AGENTS, {"t": tenant_id})
            )
        wanted = {n: i for n, i in servers.items() if mcp_servers is None or n in mcp_servers}
        found = anyio.from_thread.run(self._mcp_tools, tenant_id, wanted) if wanted else {}
        kbs = self.knowledge.list_knowledge_bases(tenant_id)
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

    # ------------------------------------------------------------------------------------ draft

    def run_drafter(
        self, tenant_id: str, drafter_agent_id: str, prompt: str, what: str, timeout: float | None = None,
    ) -> tuple[dict[str, Any], list[str], str, str, str | None]:
        """One run of a built-in drafter and its answer: `(parsed object, assumptions, raw, run_id, model)`.
        The parsed object has `assumptions` removed; 502, 504 and 422 `draft_invalid` for the failures."""
        try:
            RunCreate(run_id=uuid.uuid4(), input=prompt)
        except ValidationError:
            raise InvalidInput("The description is too long.", code="description_too_long") from None
        started = self.runs.start_agent_run(tenant_id, uuid.UUID(drafter_agent_id), input_text=prompt)
        run_id = str(started.run_id)
        try:
            finished = self.runs.wait_for_run(
                tenant_id, started.run_id, timeout=DRAFT_TIMEOUT_S if timeout is None else timeout,
                poll_interval=DRAFT_POLL_S,
            )
        except RunWaitTimeout:
            raise DraftTimeout("The drafter did not answer in time; try again.", code="draft_timeout") from None
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

    def draft_agent(
        self, tenant_id: str, user_id: str, description: str, current: ArchitectAgentSpec | None
    ) -> dict[str, Any]:
        """Run the drafter once and wait for its structured answer, cut down to the allowlist."""
        ids, _ = self.ensure_seed(tenant_id, user_id)
        allowlist = self.build_allowlist(tenant_id)
        parsed, assumptions, raw, run_id, model = self.run_drafter(
            tenant_id, ids["drafter_agent_id"], drafter_input(description, current, allowlist), "agent",
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
        return {
            "spec": cleaned.model_dump(mode="json"),
            "assumptions": assumptions,
            "dropped": dropped,
            "run_id": run_id,
            "model": model,
        }

    def draft_workflow(
        self, tenant_id: str, user_id: str, description: str, current: ArchitectWorkflowSpec | None
    ) -> dict[str, Any]:
        """`draft_agent` for a workflow (the built-in workflow drafter)."""
        ids, _ = self.ensure_seed(tenant_id, user_id)
        allowlist = self.build_allowlist(tenant_id, mcp_servers=set())
        parsed, assumptions, raw, run_id, model = self.run_drafter(
            tenant_id, ids["workflow_drafter_agent_id"], workflow_drafter_input(description, current, allowlist),
            "workflow",
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
        return {
            "spec": cleaned.model_dump(mode="json"),
            "assumptions": assumptions,
            "dropped": dropped,
            "run_id": run_id,
            "model": model,
        }

    # ------------------------------------------------------------------------------- page save

    def save_agent(
        self, tenant_id: str, user_id: str, spec: ArchitectAgentSpec, *, expected_version: int | None, publish: bool
    ) -> dict[str, Any]:
        """Save an agent from the restricted format as the draft of the Architect-made agent with `spec.id`
        (a new one when it is empty), and with `publish` publish it as the user."""
        allowlist = self.build_allowlist(tenant_id, mcp_servers={r.server for r in spec.mcp_tools})
        try:
            compiled: AgentSpec = compile_agent(spec, allowlist)
        except ValueError as exc:
            raise _compile_refusal(exc) from None
        return self._save(
            tenant_id, user_id, AGENTS, slug=spec.id, name=compiled.name,
            spec_json=compiled.model_dump(mode="json"), expected_version=expected_version, publish=publish,
        )

    def save_workflow(
        self, tenant_id: str, user_id: str, spec: ArchitectWorkflowSpec, *, expected_version: int | None, publish: bool
    ) -> dict[str, Any]:
        """`save_agent` for a workflow: the restricted format compiles to a full workflow spec."""
        allowlist = self.build_allowlist(tenant_id, mcp_servers=set())
        try:
            compiled = compile_workflow(spec, allowlist)
        except ValueError as exc:
            raise _compile_refusal(exc) from None
        return self._save(
            tenant_id, user_id, WORKFLOWS, slug=spec.id, name=compiled.name,
            spec_json=compiled.model_dump(mode="json"), expected_version=expected_version, publish=publish,
        )

    def _save(
        self, tenant_id: str, user_id: str, kind: _Kind, *, slug: str | None, name: str, spec_json: dict[str, Any],
        expected_version: int | None, publish: bool,
    ) -> dict[str, Any]:
        """Write the draft of one Architect definition by slug (a new row, or the Architect-made row that has
        the slug) and optionally publish it, in one transaction."""
        with self.engine.begin() as conn:
            if slug is None:
                slug = _free_slug(conn, kind, tenant_id, name)
                row = None
                if expected_version is not None:
                    raise _no_versions()
            elif as_uuid(slug) is not None:
                row = conn.execute(kind.by_id, {"t": tenant_id, "s": as_uuid(slug)}).mappings().first()
                if row is None:
                    raise NotFound(f"No {kind.label} made by the Architect has the uuid {slug!r}.", code="not_found")
                slug = row["slug"]
            else:
                if slug in RESERVED_AGENT_IDS:
                    raise InvalidInput(f"{slug!r} is a built-in id; choose another id.", code="reserved_id")
                row = conn.execute(kind.by_slug, {"t": tenant_id, "s": slug}).mappings().first()
            if row is not None:
                if row["builtin"]:
                    raise InvalidInput(f"{slug!r} is a built-in agent; it is not edited here.", code="reserved_id")
                if row["archived"]:
                    raise Conflict(f"The {kind.label} {slug!r} is archived; unarchive it first.", code="archived")
                latest = conn.execute(kind.latest, {"d": row["id"], "t": tenant_id}).scalar()
                if latest != expected_version:
                    raise refuse(
                        409, f"The {kind.label} {slug!r} is at version {latest}, not {expected_version}; reload it.",
                        code="version_mismatch", latest_version=latest,
                    )
            elif expected_version is not None:
                raise _no_versions()
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
                    raise Conflict(f"The id {slug!r} was taken a moment ago; try again.", code="id_conflict") from None
            warnings: list[dict[str, str]] = []
            if publish:
                op = definitions.publish_agent if kind is AGENTS else definitions.publish_workflow
                result = op(conn, tenant_id, row_id, created_by=user_id, created_via=PAGE_MARKER)
                warnings = result.warnings
            version = conn.execute(kind.latest, {"d": row_id, "t": tenant_id}).scalar()
        return {
            "id": slug, f"{kind.label}_id": row_id, "version": version, "published": bool(publish),
            "warnings": warnings,
        }
