"""The Architect's read-only view of the tenant: the `platform.*` tools.

They answer "what is there?": knowledge bases and their sources, sync history, every workflow and agent
of the tenant (not only the ones the Architect made), schedules and recent runs. Like the `architect.*` tools
they belong to the built-in Architect only (a tenant agent that lists one is refused, and the tools are in the
code denylist of `list_approved_tools`), carry no approval (nothing here writes) and run in the Architect's
run with the run's tenant, over the app database; never through HTTP.

Each tool is a thin wrapper over a function of `eap_run_store.platform_reads`, the one implementation (the
source and sync shapes there are the ones the API and the MCP control surface return). Here the result is
also passed through `eap_observability.redact` (credential-looking text, length), so a stray token in a name
or an error never reaches the model. Lists are bounded (limit at most 50), strings are cut, and a run's
input and output are never returned.

A tool never raises for a problem the model can fix: it returns `{"error": {"code", "message"}}`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic_ai import RunContext
from sqlalchemy.engine import Engine

from eap_observability import redact
from eap_run_store import platform_reads as reads
from eap_runtime import RunDeps, ToolRegistry
from eap_schemas.architect import ARCHITECT_PLATFORM_TOOL_IDS, CONTROL_LIST_TOOL_ID

# `platform.list_capabilities` is the control-plane bridge's (eap_worker.control_bridge); the rest are these.
TOOL_IDS = tuple(t for t in ARCHITECT_PLATFORM_TOOL_IDS if t != CONTROL_LIST_TOOL_ID)
MAX_STRING = 300


def _clean(value: Any) -> Any:
    """`value` with every string redacted and bounded."""
    if isinstance(value, str):
        return redact(value, max_length=MAX_STRING)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


async def _run(fn: Callable[[], Any], not_found: str | None = None) -> dict[str, Any]:
    try:
        out = await asyncio.to_thread(fn)
    except ValueError as exc:
        return _error("invalid_argument", str(exc)[:200])
    if out is None:
        return _error("not_found", not_found or "Nothing with that id in this tenant.")
    return _clean(out)


async def list_knowledge_bases(engine: Engine, tenant_id: str, limit: int = reads.MAX_LIMIT) -> dict[str, Any]:
    return await _run(lambda: reads.list_knowledge_bases(engine, tenant_id, limit))


async def get_knowledge_base(engine: Engine, tenant_id: str, kb_id: str) -> dict[str, Any]:
    return await _run(lambda: reads.get_knowledge_base(engine, tenant_id, kb_id), "No knowledge base has that id.")


async def list_source_syncs(engine: Engine, tenant_id: str, source_id: str, limit: int = 10) -> dict[str, Any]:
    return await _run(lambda: reads.list_source_syncs(engine, tenant_id, source_id, limit), "No source has that id.")


async def list_workflows(engine: Engine, tenant_id: str, limit: int = reads.MAX_LIMIT, include_archived: bool = True) -> dict[str, Any]:
    return await _run(lambda: reads.list_workflows(engine, tenant_id, limit, include_archived))


async def get_workflow(engine: Engine, tenant_id: str, workflow_id: str) -> dict[str, Any]:
    return await _run(lambda: reads.get_workflow(engine, tenant_id, workflow_id), "No workflow has that id.")


async def list_agents(engine: Engine, tenant_id: str, limit: int = reads.MAX_LIMIT, include_archived: bool = False) -> dict[str, Any]:
    return await _run(lambda: reads.list_agents(engine, tenant_id, limit, include_archived))


async def list_schedules(engine: Engine, tenant_id: str, workflow_id: str | None = None, limit: int = reads.MAX_LIMIT) -> dict[str, Any]:
    return await _run(lambda: reads.list_schedules(engine, tenant_id, workflow_id, limit))


async def list_recent_runs(
    engine: Engine, tenant_id: str, limit: int = reads.DEFAULT_LIMIT, workflow_id: str | None = None,
    agent_id: str | None = None, kind: str | None = None, status: str | None = None,
) -> dict[str, Any]:
    return await _run(lambda: reads.list_recent_runs(
        engine, tenant_id, limit=limit, workflow_id=workflow_id, agent_id=agent_id, kind=kind, status=status))


async def get_run(engine: Engine, tenant_id: str, run_id: str) -> dict[str, Any]:
    return await _run(lambda: reads.get_run(engine, tenant_id, run_id), "No run has that id.")


async def list_runs(
    engine: Engine, tenant_id: str, limit: int = reads.MAX_LIMIT, workflow_id: str | None = None,
    agent_id: str | None = None, kind: str | None = None, status: str | None = None,
) -> dict[str, Any]:
    return await _run(lambda: reads.list_runs_with_previews(
        engine, tenant_id, limit=limit, workflow_id=workflow_id, agent_id=agent_id, kind=kind, status=status))


# ------------------------------------------------------------------------------------ registered tools


def _engine() -> Engine:
    from eap_worker.runtime import get_runtime

    return get_runtime().engine


async def _list_knowledge_bases(ctx: RunContext[RunDeps], limit: int = 50) -> dict[str, Any]:
    """The tenant's knowledge bases: name, id, documents, chunks, embedding model and how many sources each has."""
    return await list_knowledge_bases(_engine(), ctx.deps.tenant_id, limit)


async def _get_knowledge_base(ctx: RunContext[RunDeps], kb_id: str) -> dict[str, Any]:
    """One knowledge base and its data sources: each source's name, provider, kind, non-secret config, last sync
    status and time, records and documents."""
    return await get_knowledge_base(_engine(), ctx.deps.tenant_id, kb_id)


async def _list_source_syncs(ctx: RunContext[RunDeps], source_id: str, limit: int = 10) -> dict[str, Any]:
    """The last syncs of a data source (id from get_knowledge_base), newest first: mode, status, discovered,
    inserted, updated, failed, budget used, errors count and whether diagnostics exist."""
    return await list_source_syncs(_engine(), ctx.deps.tenant_id, source_id, limit)


async def _list_workflows(ctx: RunContext[RunDeps], limit: int = 50, include_archived: bool = True) -> dict[str, Any]:
    """Every workflow of the tenant, whoever made it: id, name, archived, published version (null = never
    published), draft_pending and created_by_architect."""
    return await list_workflows(_engine(), ctx.deps.tenant_id, limit, include_archived)


async def _get_workflow(ctx: RunContext[RunDeps], workflow_id: str) -> dict[str, Any]:
    """One workflow, any maker's, read-only: its published spec as a summary (steps with kind and tool or agent).
    Edit only the ones you made, with the architect.* tools."""
    return await get_workflow(_engine(), ctx.deps.tenant_id, workflow_id)


async def _list_agents(ctx: RunContext[RunDeps], limit: int = 50, include_archived: bool = False) -> dict[str, Any]:
    """Every agent of the tenant, whoever made it: id, name, published version, draft_pending and model."""
    return await list_agents(_engine(), ctx.deps.tenant_id, limit, include_archived)


async def _list_schedules(ctx: RunContext[RunDeps], workflow_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    """The tenant's schedules (of one workflow when given): workflow, name, cron, timezone, active, next and last
    fire, last error."""
    return await list_schedules(_engine(), ctx.deps.tenant_id, workflow_id, limit)


async def _list_recent_runs(
    ctx: RunContext[RunDeps], limit: int = 20, workflow_id: str | None = None, agent_id: str | None = None,
    kind: str | None = None, status: str | None = None,
) -> dict[str, Any]:
    """The last runs, newest first: kind (agent, workflow, tool), status, start, end, the error's first line and
    the workflow or agent name. Filter by workflow_id, agent_id, kind or status. Never returns a run's input or output."""
    return await list_recent_runs(_engine(), ctx.deps.tenant_id, limit, workflow_id, agent_id, kind, status)


async def _get_run(ctx: RunContext[RunDeps], run_id: str) -> dict[str, Any]:
    """One run by ID: status, display_status, input (bounded 4 KiB, secrets masked), output (bounded 4 KiB),
    error, and the step list with status and a 200-grapheme output preview per step."""
    return await get_run(_engine(), ctx.deps.tenant_id, run_id)


async def _list_runs(
    ctx: RunContext[RunDeps], limit: int = 50, workflow_id: str | None = None, agent_id: str | None = None,
    kind: str | None = None, status: str | None = None,
) -> dict[str, Any]:
    """The last runs, newest first: kind, status, start, end, the error's first line, output_preview (120 graphemes),
    and the workflow or agent name. Filter by workflow_id, agent_id, kind or status. Never returns a run's input or output."""
    return await list_runs(_engine(), ctx.deps.tenant_id, limit, workflow_id, agent_id, kind, status)


_TOOLS: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("platform.list_knowledge_bases", _list_knowledge_bases),
    ("platform.get_knowledge_base", _get_knowledge_base),
    ("platform.list_source_syncs", _list_source_syncs),
    ("platform.list_workflows", _list_workflows),
    ("platform.get_workflow", _get_workflow),
    ("platform.list_agents", _list_agents),
    ("platform.list_schedules", _list_schedules),
    ("platform.list_recent_runs", _list_recent_runs),
    ("platform.get_run", _get_run),
    ("platform.list_runs", _list_runs),
)
assert tuple(t for t, _ in _TOOLS) == TOOL_IDS


def register_platform_read_tools(registry: ToolRegistry) -> None:
    """Register the `platform.*` read tools (no side effect, so no approval)."""
    for tool_id, func in _TOOLS:
        registry.register(tool_id, func, description=(func.__doc__ or tool_id).strip(), side_effect=False)
    from eap_worker.control_bridge import register_control_tools

    register_control_tools(registry)


__all__ = ["TOOL_IDS", "register_platform_read_tools"]
