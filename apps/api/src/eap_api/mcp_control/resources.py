"""Read-only resources, tenant-scoped and bounded (256 KiB, runs' text 32 KiB)."""

from __future__ import annotations

from typing import Any

from eap_api.mcp_control.helpers import require_agent, require_workflow, uid
from eap_api.mcp_control.routing import DB, call, call_service
from eap_api.mcp_control.server import current_ctx


def register_resources(resource: Any) -> None:
    @resource("pydanticos://agents/{agent_id}")
    async def agent_resource(agent_id: str) -> Any:
        """An agent's draft spec."""
        from eap_api.services.agents import AgentService

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        aid = uid(agent_id, "agent_id")
        agent, _revision = await call_service(ctx, AgentService, lambda s: s.get(ctx.tenant_id, aid))
        return agent

    @resource("pydanticos://workflows/{workflow_id}")
    async def workflow_resource(workflow_id: str) -> Any:
        """A workflow's draft spec."""
        from eap_api.services.workflows import WorkflowService

        ctx = current_ctx()
        require_workflow(ctx, workflow_id)
        return await call(ctx, lambda db, wid: WorkflowService(db).get(ctx.tenant_id, wid)[0], db=DB,
                          wid=str(uid(workflow_id, "workflow_id")))

    @resource("pydanticos://runs/{run_id}/report")
    async def run_report_resource(run_id: str) -> Any:
        """A run's bounded report: status, usage, input and output (cut at 32 KiB)."""
        from eap_api.mcp_control.tools_read import run_report

        return await run_report(run_id)

    @resource("pydanticos://knowledge-bases/{kb_id}")
    async def kb_resource(kb_id: str) -> Any:
        """A knowledge base with its counts and ingest spend."""
        from eap_api.service_deps.knowledge import knowledge_service_of

        ctx = current_ctx()
        return await call_service(
            ctx, lambda _db: knowledge_service_of(ctx.request),
            lambda s: s.get_knowledge_base(ctx.tenant_id, str(uid(kb_id, "kb_id"))))
