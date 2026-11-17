"""The Architect's read and draft tools (Task 3): registry tools in the `architect.` namespace.

They run inside the Architect agent's run, in the worker, over the app database with the run's tenant
(`ctx.deps.tenant_id`); never through HTTP to our own API.

Read tools (no approval): `architect.list_approved_tools`, `list_created_agents`,
`list_agent_versions`, `list_created_workflows`, `list_workflow_versions`,
`list_workflow_step_kinds`. Draft tools (no approval, a draft is inert): `architect.save_agent`,
`architect.save_workflow`. Gated tools (publish, restore, archive, schedules) arrive in Task 4.

Rules every tool follows:

- Ids in tool inputs and outputs are slugs (`agents.slug`, `workflows.slug`), never row ids.
- A tool never raises for a problem the model can fix: it returns `{"error": {"code", "message"}}`.
- The Architect edits only what the Architect made: a definition is the Architect's when it has a
  slug (only these tools and the Architect page set one). It never edits a built-in agent or the
  reserved slugs (`architect`, `architect-drafter`, ...), and when the latest version of a
  definition has no `created_via` starting with `architect` (someone edited it by hand) a save
  returns `{"status": "needs_confirmation", "reason": "edited_by_hand"}` and writes nothing until
  it is called again with `confirm: true`.
- A save writes only the draft (`draft_spec`); the version with `created_via = architect:<run_id>`
  comes from the gated publish tool.

The functions with an `engine` argument hold the logic (and are what the tests call); the registered
tools are thin wrappers that take the engine from `get_runtime()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pydantic_ai import RunContext
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from eap_runtime import RunDeps, ToolRegistry
from eap_schemas import reserved_tool_name_conflicts
from eap_schemas.architect import (
    AGENT_ID_PATTERN,
    RESERVED_AGENT_IDS,
    Allowlist,
    ArchitectAgentSpec,
    ArchitectWorkflowSpec,
    compile_agent,
    compile_workflow,
)

from eap_worker.architect_allowlist import approved_for_tenant

log = logging.getLogger("eap_worker.architect_tools")

ARCHITECT_MARKER_PREFIX = "architect"
TOOL_IDS = (
    "architect.list_approved_tools",
    "architect.list_created_agents",
    "architect.list_agent_versions",
    "architect.list_created_workflows",
    "architect.list_workflow_versions",
    "architect.list_workflow_step_kinds",
    "architect.save_agent",
    "architect.save_workflow",
)

STEP_KINDS: list[dict[str, Any]] = [
    {
        "kind": "agent",
        "fields": {"id": "step id", "agent": "id of an agent from list_approved_tools.agents", "prompt": "text"},
        "description": "Run one agent with the prompt and pass its answer on.",
    },
    {
        "kind": "parallel",
        "fields": {"id": "step id", "branches": "2 to 4 agent steps (id, agent, prompt) that run together"},
        "description": "Run agent steps at the same time. The step after it starts when all have finished, "
        "so a parallel step cannot be last; that step reads each branch as {{steps.<branch id>}}.",
    },
    {
        "kind": "tool",
        "fields": {"id": "step id", "tool": "id of a tool from list_approved_tools.tools", "params": "object"},
        "description": "Call one approved tool. A param value may be exactly one placeholder.",
    },
]
PLACEHOLDERS = {
    "{{input}}": "the text the workflow was started with",
    "{{previous}}": "the answer of the step before this one (not after a parallel step)",
    "{{steps.<id>}}": "the answer of an earlier step, or of a branch of an earlier parallel step",
}
LIMITS = {"steps": "1 to 8, run in order", "branches": "2 to 4", "loops_conditions_code": "not supported"}


def error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _code(exc: ValueError) -> tuple[str, str]:
    """`code` and message of a compile ValueError that starts "code: ..."; other errors are invalid_spec."""
    message = str(exc)
    head, sep, _ = message.partition(":")
    if sep and re.fullmatch(r"[a-z_]+", head):
        return head, message
    return "invalid_spec", message


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"a-{slug}".strip("-")[:48].strip("-")
    return slug if AGENT_ID_PATTERN.fullmatch(slug) else "definition"


def _marked(created_via: str | None) -> bool:
    return bool(created_via) and created_via.startswith(ARCHITECT_MARKER_PREFIX)


class _Kind:
    """The SQL of one definition kind (agents or workflows); `table` and `versions` are fixed names."""

    def __init__(self, table: str, versions: str, fk: str) -> None:
        self.table, self.versions, self.fk = table, versions, fk
        self.by_slug = text(
            f"SELECT id, name, draft_spec, archived, {'builtin' if table == 'agents' else 'false AS builtin'} "
            f"FROM {table} WHERE tenant_id = :t AND slug = :s"
        )
        self.latest = text(
            f"SELECT version, created_via, spec FROM {versions} WHERE {fk} = :d AND tenant_id = :t "
            "ORDER BY version DESC LIMIT 1"
        )
        self.listing = text(
            f"SELECT id, slug, name, draft_spec FROM {table} WHERE tenant_id = :t AND slug IS NOT NULL "
            f"AND NOT archived{' AND NOT builtin' if table == 'agents' else ''} ORDER BY slug"
        )
        self.history = text(
            f"SELECT version, created_via, created_at FROM {versions} WHERE {fk} = :d AND tenant_id = :t "
            "ORDER BY version DESC"
        )
        self.insert = text(
            f"INSERT INTO {table} (id, tenant_id, name, draft_spec, archived, slug, created_at, updated_at"
            f"{', builtin' if table == 'agents' else ''}) VALUES (:id, :t, :name, CAST(:spec AS jsonb), false, "
            f":s, :now, :now{', false' if table == 'agents' else ''})"
        )
        self.update = text(
            f"UPDATE {table} SET name = :name, draft_spec = CAST(:spec AS jsonb), updated_at = :now "
            "WHERE id = :id AND tenant_id = :t"
        )


AGENTS = _Kind("agents", "agent_versions", "agent_id")
WORKFLOWS = _Kind("workflows", "workflow_versions", "workflow_id")


def _list_created(engine: Engine, tenant_id: str, kind: _Kind) -> list[dict[str, Any]]:
    out = []
    with engine.connect() as conn:
        for row in conn.execute(kind.listing, {"t": tenant_id}).mappings().all():
            history = conn.execute(kind.history, {"d": row["id"], "t": tenant_id}).mappings().all()
            latest = conn.execute(kind.latest, {"d": row["id"], "t": tenant_id}).mappings().first()
            out.append({
                "id": row["slug"],
                "name": row["name"],
                "versions": len(history),
                "latest_created_via": history[0]["created_via"] if history else None,
                "draft": latest is None or row["draft_spec"] != latest["spec"],
            })
    return out


def _list_versions(engine: Engine, tenant_id: str, kind: _Kind, slug: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(kind.by_slug, {"t": tenant_id, "s": slug}).mappings().first()
        if row is None or row["builtin"]:
            return error("not_found", f"No definition made by the Architect has the id {slug!r}.")
        history = conn.execute(kind.history, {"d": row["id"], "t": tenant_id}).mappings().all()
    return {
        "id": slug,
        "name": row["name"],
        "versions": [
            {"version": h["version"], "created_via": h["created_via"], "created_at": h["created_at"].isoformat()}
            for h in history
        ],
    }


def _save(
    engine: Engine, tenant_id: str, kind: _Kind, *, slug: str | None, derived_from: str, name: str,
    spec_json: dict[str, Any], confirm: bool,
) -> dict[str, Any]:
    """Write the draft of one definition (blocking). `slug` None derives one from the name."""
    label = "agent" if kind is AGENTS else "workflow"
    spec_text = json.dumps(spec_json)
    with engine.begin() as conn:
        row = None
        if slug is not None:
            if slug in RESERVED_AGENT_IDS:
                return error("reserved_id", f"{slug!r} is a built-in {label} id; choose another id.")
            row = conn.execute(kind.by_slug, {"t": tenant_id, "s": slug}).mappings().first()
        else:
            base = slugify(derived_from)
            for n in range(1, 50):
                candidate = base if n == 1 else f"{base[:44]}-{n}"
                if candidate in RESERVED_AGENT_IDS:
                    continue
                found = conn.execute(kind.by_slug, {"t": tenant_id, "s": candidate}).mappings().first()
                if found is None:
                    slug = candidate
                    break
                if found["draft_spec"] == spec_json and found["name"] == name and not found["builtin"]:
                    slug, row = candidate, found  # the same draft again: a retried call
                    break
            if slug is None:
                return error("id_conflict", "Could not find a free id; give the definition an id.")
        if row is not None:
            if row["builtin"]:
                return error("builtin_agent", f"{slug!r} is a built-in agent; the Architect does not edit it.")
            if row["archived"]:
                return error("archived", f"The {label} {slug!r} is archived; it cannot be edited.")
            latest = conn.execute(kind.latest, {"d": row["id"], "t": tenant_id}).mappings().first()
            if latest is not None and not _marked(latest["created_via"]) and not confirm:
                return {
                    "status": "needs_confirmation",
                    "reason": "edited_by_hand",
                    "id": slug,
                    "message": f"The latest version of {slug!r} (v{latest['version']}) was made or edited by hand. "
                    "Tell the person, and call this tool again with confirm: true only after they say yes.",
                }
            conn.execute(kind.update, {"id": row["id"], "t": tenant_id, "name": name, "spec": spec_text, "now": _now()})
        else:
            try:
                with conn.begin_nested():
                    conn.execute(kind.insert, {
                        "id": str(uuid.uuid4()), "t": tenant_id, "name": name, "spec": spec_text, "s": slug,
                        "now": _now(),
                    })
            except IntegrityError:
                return error("id_conflict", f"The id {slug!r} was taken a moment ago; call again.")
    return {"id": slug, "version": None, "status": "draft"}


async def list_approved_tools(engine: Engine, tenant_id: str) -> dict[str, Any]:
    return (await approved_for_tenant(engine, tenant_id)).model_dump(mode="json")


async def list_created_agents(engine: Engine, tenant_id: str) -> dict[str, Any]:
    return {"agents": await asyncio.to_thread(_list_created, engine, tenant_id, AGENTS)}


async def list_agent_versions(engine: Engine, tenant_id: str, id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_list_versions, engine, tenant_id, AGENTS, id)


async def list_created_workflows(engine: Engine, tenant_id: str) -> dict[str, Any]:
    return {"workflows": await asyncio.to_thread(_list_created, engine, tenant_id, WORKFLOWS)}


async def list_workflow_versions(engine: Engine, tenant_id: str, id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_list_versions, engine, tenant_id, WORKFLOWS, id)


async def list_workflow_step_kinds() -> dict[str, Any]:
    return {"step_kinds": STEP_KINDS, "placeholders": PLACEHOLDERS, "limits": LIMITS}


async def save_agent(
    engine: Engine, tenant_id: str, spec: ArchitectAgentSpec, confirm: bool = False,
    *, allowlist: Allowlist | None = None, run_id: str | None = None,
) -> dict[str, Any]:
    allowlist = allowlist or await approved_for_tenant(engine, tenant_id)
    try:
        compiled = compile_agent(spec, allowlist)
    except ValueError as exc:
        code, message = _code(exc)
        return error(code, message)
    result = await asyncio.to_thread(
        _save, engine, tenant_id, AGENTS, slug=spec.id, derived_from=spec.name, name=compiled.name,
        spec_json=compiled.model_dump(mode="json"), confirm=confirm,
    )
    if "error" in result or result.get("status") != "draft":
        return result
    warnings = [f"{name!r} is a reserved platform tool name" for name in reserved_tool_name_conflicts(compiled)]
    if compiled.approvals is not None:
        warnings.append("A person approves each call of: " + ", ".join(compiled.approvals.tools))
    log.info("architect.save_agent tenant=%s run=%s id=%s", tenant_id, run_id, result["id"])
    return {**result, "warnings": warnings}


async def save_workflow(
    engine: Engine, tenant_id: str, spec: ArchitectWorkflowSpec, confirm: bool = False,
    *, allowlist: Allowlist | None = None, run_id: str | None = None,
) -> dict[str, Any]:
    allowlist = allowlist or await approved_for_tenant(engine, tenant_id)
    try:
        compiled = compile_workflow(spec, allowlist)
    except ValueError as exc:
        code, message = _code(exc)
        return error(code, message)
    result = await asyncio.to_thread(
        _save, engine, tenant_id, WORKFLOWS, slug=spec.id, derived_from=spec.name, name=compiled.name,
        spec_json=compiled.model_dump(mode="json"), confirm=confirm,
    )
    if "error" in result or result.get("status") != "draft":
        return result
    log.info("architect.save_workflow tenant=%s run=%s id=%s", tenant_id, run_id, result["id"])
    return {**result, "warnings": []}


# ------------------------------------------------------------------------------------- registry


def _engine() -> Engine:
    from eap_worker.runtime import get_runtime  # runtime imports platform_tools, which registers these

    return get_runtime().engine


async def _list_approved_tools(ctx: RunContext[RunDeps]) -> dict[str, Any]:
    """What a created agent or workflow may use: tools (with requires_approval), MCP servers and their
    tools, knowledge bases, models, and agents a workflow step may run. Offer only these."""
    return await list_approved_tools(_engine(), ctx.deps.tenant_id)


async def _list_created_agents(ctx: RunContext[RunDeps]) -> dict[str, Any]:
    """The agents the Architect made: id, name, number of versions, who made the latest version, draft."""
    return await list_created_agents(_engine(), ctx.deps.tenant_id)


async def _list_agent_versions(ctx: RunContext[RunDeps], id: str) -> dict[str, Any]:
    """The versions of an agent the Architect made (by its id)."""
    return await list_agent_versions(_engine(), ctx.deps.tenant_id, id)


async def _list_created_workflows(ctx: RunContext[RunDeps]) -> dict[str, Any]:
    """The workflows the Architect made: id, name, number of versions, who made the latest version, draft."""
    return await list_created_workflows(_engine(), ctx.deps.tenant_id)


async def _list_workflow_versions(ctx: RunContext[RunDeps], id: str) -> dict[str, Any]:
    """The versions of a workflow the Architect made (by its id)."""
    return await list_workflow_versions(_engine(), ctx.deps.tenant_id, id)


async def _list_workflow_step_kinds(ctx: RunContext[RunDeps]) -> dict[str, Any]:
    """The step kinds a workflow may use, the prompt placeholders and the limits."""
    return await list_workflow_step_kinds()


async def _save_agent(ctx: RunContext[RunDeps], spec: ArchitectAgentSpec, confirm: bool = False) -> dict[str, Any]:
    """Save an agent definition as a draft (not live). New agent when `id` is unused, otherwise the next
    draft of that Architect-made agent. If the result is needs_confirmation, ask the person first and call
    again with confirm: true. Grant a tool only when the description names a capability."""
    return await save_agent(_engine(), ctx.deps.tenant_id, spec, confirm, run_id=str(ctx.deps.run_id))


async def _save_workflow(
    ctx: RunContext[RunDeps], spec: ArchitectWorkflowSpec, confirm: bool = False
) -> dict[str, Any]:
    """Save a workflow definition as a draft (not live). Same rules as save_agent."""
    return await save_workflow(_engine(), ctx.deps.tenant_id, spec, confirm, run_id=str(ctx.deps.run_id))


_TOOLS: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("architect.list_approved_tools", _list_approved_tools),
    ("architect.list_created_agents", _list_created_agents),
    ("architect.list_agent_versions", _list_agent_versions),
    ("architect.list_created_workflows", _list_created_workflows),
    ("architect.list_workflow_versions", _list_workflow_versions),
    ("architect.list_workflow_step_kinds", _list_workflow_step_kinds),
    ("architect.save_agent", _save_agent),
    ("architect.save_workflow", _save_workflow),
)


def register_architect_tools(registry: ToolRegistry) -> None:
    """Register the Architect's read and draft tools. No approval entries: none of them goes live."""
    for tool_id, func in _TOOLS:
        registry.register(tool_id, func, description=(func.__doc__ or tool_id).strip(), side_effect=False)


__all__ = ["TOOL_IDS", "register_architect_tools", "save_agent", "save_workflow", "slugify"]
