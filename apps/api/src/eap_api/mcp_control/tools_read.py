"""Read tools: any API key of the tenant (or the dev bearer) may call them.

Each body validates its arguments and calls the route function the HTTP API serves the same data with
(`eap_api.routes.*`), so the shapes are the platform's own. Lists are paged at most 100 items; a run's
input and output are cut at 32 KiB; connections show `credential_refs` (references, never values).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from eap_api.mcp_control.helpers import (
    DEFAULT_PAGE, MAX_REPORT_BYTES, MAX_WAIT_SECONDS, clip, not_found, page, require_agent, require_workflow,
    tenant_run, uid, wait_for_run,
)
from eap_api.mcp_control.routing import DB, call
from eap_api.mcp_control.server import current_ctx

# Fields of a run kept in a bounded report (never the request context or model settings).
RUN_FIELDS = (
    "run_id", "kind", "agent_id", "version", "workflow_id", "workflow_version", "parent_run_id", "status",
    "display_status", "error", "created_at", "updated_at", "started_at", "finished_at", "model_id",
    "input_tokens", "output_tokens", "cost_usd", "conversation_id", "turn", "trace_id",
)


async def run_report(run_id: str, *, kind: str | None = None) -> dict[str, Any]:
    """A run as a bounded report: its status and usage, and input/output cut at 32 KiB. Shared by the
    `workflow.run.result` tool and the `pydanticos://runs/{id}/report` resource."""
    from eap_api.routes.runs import get_run

    ctx = current_ctx()
    await tenant_run(ctx, run_id, kind=kind)  # not_found for another tenant's run or a disallowed one
    full = await call(ctx, get_run, run_id=uid(run_id, "run_id"), principal=ctx.actor, stores=ctx.stores)
    report = {k: full.get(k) for k in RUN_FIELDS}
    report["input"], report["input_truncated"] = clip(full.get("input"))
    report["output"], report["output_truncated"] = clip(full.get("output"))
    if "output_json" in full and not report["output_truncated"]:
        report["output_json"] = full["output_json"]
    return report


def register_tools(tool: Any) -> None:
    @tool("agent.list")
    async def agent_list(archived: bool = False, include_system: bool = False, limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """List the tenant's agents (id, name, draft_spec, latest_version, archived). Paged: limit <= 100."""
        from eap_api.routes import list_agents

        ctx = current_ctx()
        rows = await call(ctx, list_agents, principal=ctx.actor, db=DB, archived=archived, include_system=include_system)
        return page([r for r in rows if ctx.agent_allowed(r["id"])], limit, offset)

    @tool("agent.get")
    async def agent_get(agent_id: str) -> dict[str, Any]:
        """One agent with its draft spec and latest published version number."""
        from eap_api.routes import get_agent

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        return await call(ctx, get_agent, agent_id=uid(agent_id, "agent_id"), principal=ctx.actor, db=DB)

    @tool("agent.versions")
    async def agent_versions(agent_id: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Published versions of an agent, newest first (each with its full spec). Paged: limit <= 100."""
        from eap_api.routes import list_versions

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        rows = await call(ctx, list_versions, agent_id=uid(agent_id, "agent_id"), principal=ctx.actor, db=DB)
        return page(rows, limit, offset)

    @tool("workflow.list")
    async def workflow_list(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """List the tenant's workflows (not archived): id, name, draft_spec, latest_version. Paged: limit <= 100."""
        from eap_api.routes.workflows import list_workflows

        ctx = current_ctx()
        rows = await call(ctx, list_workflows, principal=ctx.actor, db=DB)
        return page([r for r in rows if ctx.workflow_allowed(r["id"])], limit, offset)

    @tool("workflow.get")
    async def workflow_get(workflow_id: str) -> dict[str, Any]:
        """One workflow with its draft spec and latest published version number."""
        from eap_api.routes.workflows import get_workflow

        ctx = current_ctx()
        require_workflow(ctx, workflow_id)
        return await call(ctx, get_workflow, workflow_id=uid(workflow_id, "workflow_id"), principal=ctx.actor, db=DB)

    @tool("workflow.versions")
    async def workflow_versions(workflow_id: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Published versions of a workflow, newest first (each with its spec). Paged: limit <= 100."""
        from eap_api.routes.workflows import list_workflow_versions

        ctx = current_ctx()
        require_workflow(ctx, workflow_id)
        rows = await call(ctx, list_workflow_versions, workflow_id=uid(workflow_id, "workflow_id"), principal=ctx.actor, db=DB)
        return page(rows, limit, offset)

    @tool("workflow.run.get")
    async def workflow_run_get(run_id: str) -> dict[str, Any]:
        """Status of a workflow run: status, display_status (waiting_approval, recovering, ...), error, usage."""
        ctx = current_ctx()
        run = await tenant_run(ctx, run_id, kind="workflow")
        return {k: run.get(k) for k in ("status", "display_status", "error")} | {"run_id": str(run["id"]), "workflow_id": run.get("workflow_id")}

    @tool("workflow.run.wait")
    async def workflow_run_wait(run_id: str, timeout_seconds: float = 30) -> dict[str, Any]:
        """Wait (at most 300 s) until the run ends or waits for an approval; `done` says whether it settled.
        Read the output with workflow.run.result."""
        ctx = current_ctx()
        run, settled = await wait_for_run(ctx, run_id, min(timeout_seconds, MAX_WAIT_SECONDS), kind="workflow")
        return {"run_id": str(run["id"]), "status": run["status"], "display_status": run["display_status"],
                "done": settled, "error": run.get("error")}

    @tool("workflow.run.result")
    async def workflow_run_result(run_id: str) -> dict[str, Any]:
        """The bounded report of a workflow run: status, usage, input and output (each cut at 32 KiB)."""
        return await run_report(run_id, kind="workflow")

    @tool("workflow.schedule.list")
    async def workflow_schedule_list(workflow_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        """The tenant's schedules (of one workflow when given), newest first; `next_before` pages."""
        from eap_api.routes.schedules import get_schedule_store, list_all_schedules, list_workflow_schedules

        ctx = current_ctx()
        store = get_schedule_store(ctx.request, ctx.stores)
        limit = max(1, min(limit, 100))
        if workflow_id is not None:
            require_workflow(ctx, workflow_id)
            return await call(ctx, list_workflow_schedules, workflow_id=uid(workflow_id, "workflow_id"),
                              principal=ctx.actor, db=DB, store=store, limit=limit, before=None)
        out = await call(ctx, list_all_schedules, principal=ctx.actor, db=DB, store=store, limit=limit, before=None)
        out["items"] = [s for s in out["items"] if ctx.workflow_allowed(str(s.get("workflow_id")))]
        return out

    @tool("kb.list")
    async def kb_list(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """The tenant's knowledge bases with document and chunk counts. Paged: limit <= 100."""
        from eap_api.routes.knowledge import get_knowledge_store, list_knowledge_bases

        ctx = current_ctx()
        out = await call(ctx, list_knowledge_bases, principal=ctx.actor, store=get_knowledge_store(ctx.request))
        return page(out["items"], limit, offset)

    @tool("kb.get")
    async def kb_get(kb_id: str) -> dict[str, Any]:
        """One knowledge base with counts and ingest spend."""
        from eap_api.routes.knowledge import get_knowledge_base, get_knowledge_store

        ctx = current_ctx()
        return await call(ctx, get_knowledge_base, kb_id=str(uid(kb_id, "kb_id")), principal=ctx.actor,
                          store=get_knowledge_store(ctx.request))

    @tool("kb.source.list")
    async def kb_source_list(kb_id: str) -> dict[str, Any]:
        """The data sources of a knowledge base (connection, provider, config, schedule state)."""
        from eap_api.routes.kb_sources import list_sources

        ctx = current_ctx()
        return await call(ctx, list_sources, kb_id=str(uid(kb_id, "kb_id")), request=ctx.request, principal=ctx.actor)

    @tool("kb.source.get")
    async def kb_source_get(kb_id: str, source_id: str) -> dict[str, Any]:
        """One data source of a knowledge base."""
        from eap_api.routes.kb_sources import get_source

        ctx = current_ctx()
        return await call(ctx, get_source, kb_id=str(uid(kb_id, "kb_id")), source_id=str(uid(source_id, "source_id")),
                          request=ctx.request, principal=ctx.actor)

    @tool("kb.source.syncs")
    async def kb_source_syncs(kb_id: str, source_id: str, limit: int = 20) -> dict[str, Any]:
        """Recent sync runs of a data source (status, counters, bounded errors), newest first."""
        from eap_api.routes.kb_sources import list_syncs

        ctx = current_ctx()
        return await call(ctx, list_syncs, kb_id=str(uid(kb_id, "kb_id")), source_id=str(uid(source_id, "source_id")),
                          request=ctx.request, principal=ctx.actor, limit=max(1, min(limit, 100)))

    @tool("settings.connection.list")
    async def connection_list() -> dict[str, Any]:
        """The tenant's connections to external providers. `credential_refs` are `secret:<name>` references only."""
        from eap_api.routes.connections import list_connections

        ctx = current_ctx()
        return await call(ctx, list_connections, request=ctx.request, principal=ctx.actor)

    @tool("settings.connection.check")
    async def connection_check(connection_id: str) -> dict[str, Any]:
        """Check a connection against its provider: {ok, checked_at, error_code}; never a token or a provider body."""
        from eap_api.routes.connections import check

        ctx = current_ctx()
        return await call(ctx, check, connection_id=str(uid(connection_id, "connection_id")), request=ctx.request,
                          principal=ctx.actor)

    @tool("mcp.server.list")
    async def mcp_server_list(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """The MCP servers registered for the tenant's agents (header and env variable names only, never values)."""
        from eap_api.routes.mcp_servers import list_mcp_servers

        ctx = current_ctx()
        rows = await call(ctx, list_mcp_servers, principal=ctx.actor, db=DB)
        return page(rows, limit, offset)

    @tool("mcp.catalog.search")
    async def mcp_catalog_search(query: str | None = None, limit: int = 20, after: str | None = None,
                                 source: Literal["toolhive", "official"] | None = None) -> dict[str, Any]:
        """Search the mirrored MCP registry: {items, next_after}."""
        from eap_api.routes.mcp_catalog import search_catalog

        ctx = current_ctx()
        return await call(ctx, search_catalog, request=ctx.request, principal=ctx.actor, q=query,
                          limit=max(1, min(limit, 50)), after=after, source=source)

    @tool("approvals.list")
    async def approvals_list(status: Literal["pending", "decided"] = "pending", kind: Literal["tool", "step"] | None = None,
                             agent_id: str | None = None, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """The tenant's approvals (gated tool calls and workflow approval steps): {items, next_cursor}."""
        from eap_api.routes.approvals import get_approval_store, list_approvals

        ctx = current_ctx()
        if agent_id is not None:
            require_agent(ctx, agent_id)
        out = await call(
            ctx, list_approvals, principal=ctx.actor, store=get_approval_store(ctx.request, ctx.stores), stores=ctx.stores,
            approval_status=status, kind=kind, agent_id=uid(agent_id, "agent_id") if agent_id else None,
            cursor=cursor, limit=max(1, min(limit, 100)),
        )
        out["items"] = [i for i in out["items"] if (i.get("agent_id") is None or ctx.agent_allowed(str(i["agent_id"])))
                        and (i.get("workflow_id") is None or ctx.workflow_allowed(str(i["workflow_id"])))]
        return out
