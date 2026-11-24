"""Read tools: any API key of the tenant (or the dev bearer) may call them.

Each body validates its arguments and calls the service the HTTP API serves the same data with, so the
shapes are the platform's own. Lists are paged at most 100 items; a run's
input and output are cut at 32 KiB; connections show `credential_refs` (references, never values).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from eap_api.mcp_control.helpers import (
    DEFAULT_PAGE, MAX_REPORT_BYTES, MAX_WAIT_SECONDS, clip, not_found, page, require_agent, require_workflow,
    run_service, schedules, approvals, tenant_run, uid, wait_for_run,
)
from eap_api.mcp_control.routing import call_service, call_service_async
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
    ctx = current_ctx()
    await tenant_run(ctx, run_id, kind=kind)  # not_found for another tenant's run or a disallowed one
    full = await call_service(
        ctx, run_service(ctx), lambda s: s.read_run_json(ctx.tenant_id, uid(run_id, "run_id")))
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
        from eap_api.services.agents import AgentService

        ctx = current_ctx()
        rows = await call_service(
            ctx, AgentService, lambda s: s.list(ctx.tenant_id, archived=archived, include_system=include_system))
        return page([r for r in rows if ctx.agent_allowed(r["id"])], limit, offset)

    @tool("agent.get")
    async def agent_get(agent_id: str) -> dict[str, Any]:
        """One agent with its draft spec and latest published version number."""
        from eap_api.services.agents import AgentService

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        aid = uid(agent_id, "agent_id")
        agent, _revision = await call_service(ctx, AgentService, lambda s: s.get(ctx.tenant_id, aid))
        return agent

    @tool("agent.versions")
    async def agent_versions(agent_id: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Published versions of an agent, newest first (each with its full spec). Paged: limit <= 100."""
        from eap_api.services.agents import AgentService

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        aid = uid(agent_id, "agent_id")
        rows = await call_service(ctx, AgentService, lambda s: s.list_versions(ctx.tenant_id, aid))
        return page(rows, limit, offset)

    @tool("workflow.list")
    async def workflow_list(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """List the tenant's workflows (not archived): id, name, draft_spec, latest_version. Paged: limit <= 100."""
        from eap_api.services.workflows import WorkflowService

        ctx = current_ctx()
        rows = await call_service(ctx, WorkflowService, lambda s: s.list(ctx.tenant_id))
        return page([r for r in rows if ctx.workflow_allowed(r["id"])], limit, offset)

    @tool("workflow.get")
    async def workflow_get(workflow_id: str) -> dict[str, Any]:
        """One workflow with its draft spec and latest published version number."""
        from eap_api.services.workflows import WorkflowService

        ctx = current_ctx()
        require_workflow(ctx, workflow_id)
        wid = str(uid(workflow_id, "workflow_id"))
        workflow, _revision = await call_service(ctx, WorkflowService, lambda s: s.get(ctx.tenant_id, wid))
        return workflow

    @tool("workflow.versions")
    async def workflow_versions(workflow_id: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Published versions of a workflow, newest first (each with its spec). Paged: limit <= 100."""
        from eap_api.services.workflows import WorkflowService

        ctx = current_ctx()
        require_workflow(ctx, workflow_id)
        wid = str(uid(workflow_id, "workflow_id"))
        rows = await call_service(ctx, WorkflowService, lambda s: s.list_versions(ctx.tenant_id, wid))
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
        ctx = current_ctx()
        limit = max(1, min(limit, 100))
        if workflow_id is not None:
            require_workflow(ctx, workflow_id)
            wid = str(uid(workflow_id, "workflow_id"))
            return await call_service(ctx, schedules(ctx), lambda s: s.page(ctx.tenant_id, wid, limit, None))
        out = await call_service(ctx, schedules(ctx), lambda s: s.page(ctx.tenant_id, None, limit, None))
        out["items"] = [s for s in out["items"] if ctx.workflow_allowed(str(s.get("workflow_id")))]
        return out

    @tool("kb.list")
    async def kb_list(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """The tenant's knowledge bases with document and chunk counts. Paged: limit <= 100."""
        from eap_api.service_deps.knowledge import knowledge_service_of

        ctx = current_ctx()
        items = await call_service(
            ctx, lambda _db: knowledge_service_of(ctx.app, ctx.launcher), lambda s: s.list_knowledge_bases(ctx.tenant_id))
        return page(items, limit, offset)

    @tool("kb.get")
    async def kb_get(kb_id: str) -> dict[str, Any]:
        """One knowledge base with counts and ingest spend."""
        from eap_api.service_deps.knowledge import knowledge_service_of

        ctx = current_ctx()
        return await call_service(
            ctx, lambda _db: knowledge_service_of(ctx.app, ctx.launcher),
            lambda s: s.get_knowledge_base(ctx.tenant_id, str(uid(kb_id, "kb_id"))))

    @tool("kb.source.list")
    async def kb_source_list(kb_id: str) -> dict[str, Any]:
        """The data sources of a knowledge base (connection, provider, config, schedule state)."""
        from eap_api.service_deps.knowledge import kb_source_service_of

        ctx = current_ctx()
        return await call_service(
            ctx, lambda _db: kb_source_service_of(ctx.app, ctx.launcher),
            lambda s: s.list_sources(ctx.tenant_id, str(uid(kb_id, "kb_id"))))

    @tool("kb.source.get")
    async def kb_source_get(kb_id: str, source_id: str) -> dict[str, Any]:
        """One data source of a knowledge base."""
        from eap_api.service_deps.knowledge import kb_source_service_of

        ctx = current_ctx()
        view, _revision = await call_service(
            ctx, lambda _db: kb_source_service_of(ctx.app, ctx.launcher),
            lambda s: s.get_source(ctx.tenant_id, str(uid(kb_id, "kb_id")), str(uid(source_id, "source_id"))))
        return view

    @tool("kb.source.syncs")
    async def kb_source_syncs(kb_id: str, source_id: str, limit: int = 20) -> dict[str, Any]:
        """Recent sync runs of a data source (status, counters, bounded errors), newest first."""
        from eap_api.service_deps.knowledge import kb_source_service_of

        ctx = current_ctx()
        return await call_service(
            ctx, lambda _db: kb_source_service_of(ctx.app, ctx.launcher),
            lambda s: s.list_syncs(ctx.tenant_id, str(uid(kb_id, "kb_id")), str(uid(source_id, "source_id")),
                                   max(1, min(limit, 100))))

    @tool("settings.connection.list")
    async def connection_list() -> dict[str, Any]:
        """The tenant's connections to external providers. `credential_refs` are `secret:<name>` references only."""
        from eap_api.service_deps.connections import build_connection_service

        ctx = current_ctx()
        return await call_service(
            ctx, lambda _db: build_connection_service(ctx.engine), lambda s: s.list(tenant_id=ctx.tenant_id))

    @tool("settings.connection.check")
    async def connection_check(connection_id: str) -> dict[str, Any]:
        """Check a connection against its provider: {ok, checked_at, error_code}; never a token or a provider body."""
        from eap_api.service_deps.connections import build_connection_service

        ctx = current_ctx()
        cid = str(uid(connection_id, "connection_id"))
        return await call_service_async(
            ctx, lambda _db: build_connection_service(ctx.engine),
            lambda s: s.check(tenant_id=ctx.tenant_id, connection_id=cid))

    def _capability_service(ctx: Any) -> Any:
        from eap_api.mcp_servers.managed import get_mcp_runtime
        from eap_api.service_deps.capabilities import build_capability_service
        from eap_api.service_deps.connections import get_outbound_policy

        return lambda db: build_capability_service(db, ctx.engine, get_outbound_policy(), get_mcp_runtime())

    def _viewer(ctx: Any) -> Any:
        from eap_api.services.capabilities import Viewer

        return Viewer(ctx.tenant_id, ctx.principal.kind, ctx.principal.user_id or ctx.principal.key_id)

    @tool("capability.list")
    async def capability_list(
        kind: str | None = None, status: str | None = None, query: str | None = None, include_schemas: bool = False,
    ) -> dict[str, Any]:
        """What the platform can do now (the capability registry): entries with kind, status (available,
        designed_not_built, disabled), approval and limits, the known gaps, and the snapshot hash. Filter by
        kind, status or a query; include_schemas adds each tool's input and result schema. Availability comes
        from here, never from a document."""
        ctx = current_ctx()
        return await call_service_async(
            ctx, _capability_service(ctx),
            lambda s: s.list(_viewer(ctx), kind=kind, status=status, query=query, include_schemas=include_schemas))

    @tool("capability.get")
    async def capability_get(capability_id: str) -> dict[str, Any]:
        """One capability with its schemas, limits, needs and docs reference, and the gaps that concern it
        (for example tool.http.request, step.foreach, trigger.schedule). unknown_capability lists the nearest ids."""
        ctx = current_ctx()
        return await call_service_async(ctx, _capability_service(ctx), lambda s: s.get(_viewer(ctx), capability_id))

    @tool("system.context")
    async def system_context() -> dict[str, Any]:
        """The system context for the caller, as the Architect's run start reads it: the capability lines, the
        known gaps (what is designed or cannot be written), the tenant's resource NAMES and counts (secrets by
        name only), operational facts (always-gated tools, recent failures, runtime) and the principal. A layer
        that could not be built is missing and named in degraded. Availability still comes from capability.list."""
        from eap_api.deps import iam_of
        from eap_api.mcp_servers.managed import get_mcp_runtime
        from eap_api.service_deps.connections import get_outbound_policy
        from eap_api.service_deps.system_context import build_system_context_service
        from eap_api.services.authz import AuthzService

        ctx = current_ctx()
        authz = AuthzService(iam_of(ctx.app).policy)
        return await call_service_async(
            ctx, lambda db: build_system_context_service(db, ctx.engine, get_outbound_policy(), get_mcp_runtime(), authz),
            lambda s: s.context(_viewer(ctx)))

    @tool("provenance.get")
    async def provenance_get(kind: Literal["agent", "workflow"], definition_id: str, spec_hash: str | None = None) -> dict[str, Any]:
        """What a draft or publish card of an agent or workflow was based on: platform version, manifest and
        capability hashes, the principal summary and the Architect run's tool-call names (never arguments), plus
        the capabilities the draft uses that changed since it was drafted."""
        from eap_api.mcp_servers.managed import get_mcp_runtime
        from eap_api.service_deps.connections import get_outbound_policy
        from eap_api.service_deps.system_context import build_provenance_service

        ctx = current_ctx()
        return await call_service_async(
            ctx, lambda db: build_provenance_service(db, ctx.engine, get_outbound_policy(), get_mcp_runtime()),
            lambda s: s.get(_viewer(ctx), kind=kind, definition_id=definition_id, spec_hash=spec_hash))

    @tool("mcp.server.list")
    async def mcp_server_list(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """The MCP servers registered for the tenant's agents (header and env variable names only, never values)."""
        from eap_api.mcp_servers.managed import get_mcp_runtime
        from eap_api.service_deps.connections import build_mcp_server_service, get_outbound_policy

        ctx = current_ctx()
        rows = await call_service(
            ctx, lambda db: build_mcp_server_service(db, ctx.engine, get_outbound_policy(), get_mcp_runtime()),
            lambda s: s.list(ctx.tenant_id))
        return page(rows, limit, offset)

    @tool("mcp.catalog.search")
    async def mcp_catalog_search(query: str | None = None, limit: int = 20, after: str | None = None,
                                 source: Literal["toolhive", "official"] | None = None) -> dict[str, Any]:
        """Search the mirrored MCP registry: {items, next_after}."""
        from eap_api.service_deps.connections import build_mcp_catalog_service

        ctx = current_ctx()
        return await call_service(
            ctx, lambda _db: build_mcp_catalog_service(ctx.engine),
            lambda s: s.search(q=query, limit=max(1, min(limit, 50)), after=after, source=source))

    @tool("approvals.list")
    async def approvals_list(status: Literal["pending", "decided"] = "pending", kind: Literal["tool", "step"] | None = None,
                             agent_id: str | None = None, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """The tenant's approvals (gated tool calls and workflow approval steps): {items, next_cursor}."""
        ctx = current_ctx()
        if agent_id is not None:
            require_agent(ctx, agent_id)
        out = await call_service(
            ctx, approvals(ctx), lambda s: s.list(
                ctx.tenant_id, status=status, kind=kind, agent_id=str(uid(agent_id, "agent_id")) if agent_id else None,
                cursor=cursor, limit=max(1, min(limit, 100))),
        )
        out["items"] = [i for i in out["items"] if (i.get("agent_id") is None or ctx.agent_allowed(str(i["agent_id"])))
                        and (i.get("workflow_id") is None or ctx.workflow_allowed(str(i["workflow_id"])))]
        return out
