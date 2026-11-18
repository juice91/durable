"""Mutation tools: they need an API key with the `manage` scope (the dev bearer has it in dev mode).

Each body validates its arguments with the platform's own request models, then calls what the HTTP
route calls: the route function itself or the shared service under it (publish, restore and archive in
`eap_run_store.definitions`, source syncs in `eap_run_store.kb_source_launch`, runs through the
launcher, decisions through `eap_api.approvals`). So a refusal here is exactly the refusal Studio
gets. Nothing here stores or reads a secret: connection credentials are `secret:<name>` references,
managed-server variables the catalog marks secret must be references too, and no tool writes
`/secrets`.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from eap_api.mcp_control.helpers import (
    MAX_WAIT_SECONDS, clip, guard_json, require_agent, require_unrestricted, require_workflow, tenant_run, uid,
    wait_for_run,
)
from eap_api.mcp_control.routing import DB, McpToolError, call, translate
from eap_api.mcp_control.server import current_ctx
from eap_run_store.store import TERMINAL_RUN_STATUSES

CHAT_DEFAULT_WAIT_SECONDS = 120
MAX_CHAT_MESSAGE_BYTES = 32 * 1024


def _model(cls: Any, **fields: Any) -> Any:
    try:
        return cls(**fields)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError; translate() re-raises anything else
        raise translate(exc) from None


def register_tools(tool: Any) -> None:
    # ------------------------------------------------------------------------------- agents

    @tool("agent.save_draft", manage=True)
    async def agent_save_draft(spec: dict[str, Any], agent_id: str | None = None) -> dict[str, Any]:
        """Create an agent from `spec` (an AgentSpec), or replace the draft of `agent_id`. Returns the agent.
        Publishing is a separate step (agent.publish)."""
        from eap_api.routes import create_agent, patch_agent
        from eap_api.schemas import AgentCreate, AgentUpdate

        ctx = current_ctx()
        guard_json(spec, "spec")
        if agent_id is None:
            require_unrestricted(ctx, "create agents")
            body = _model(AgentCreate, spec=spec)
            return await call(ctx, create_agent, request=body, principal=ctx.actor, db=DB)
        require_agent(ctx, agent_id)
        body = _model(AgentUpdate, spec=spec)
        return await call(ctx, patch_agent, agent_id=uid(agent_id, "agent_id"), request=body, principal=ctx.actor, db=DB)

    @tool("agent.publish", manage=True)
    async def agent_publish(agent_id: str) -> dict[str, Any]:
        """Publish the agent's draft as the next immutable version, with the same checks as Studio. Returns
        the version and `warnings`."""
        from eap_api.routes import publish_version

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        return await call(ctx, publish_version, agent_id=uid(agent_id, "agent_id"), principal=ctx.actor, db=DB)

    @tool("agent.archive", manage=True)
    async def agent_archive(agent_id: str, archived: bool = True, force: bool = False) -> dict[str, Any]:
        """Archive (or, with archived=false, unarchive) an agent. Archiving is refused (409) while published
        workflows run the agent, unless force=true."""
        from eap_api.routes import archive_agent, unarchive_agent

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        aid = uid(agent_id, "agent_id")
        if archived:
            return await call(ctx, archive_agent, agent_id=aid, principal=ctx.actor, db=DB, force=force)
        return await call(ctx, unarchive_agent, agent_id=aid, principal=ctx.actor, db=DB)

    @tool("agent.chat", manage=True)
    async def agent_chat(agent_id: str, message: str, conversation_id: str | None = None,
                         wait_seconds: float = CHAT_DEFAULT_WAIT_SECONDS) -> dict[str, Any]:
        """One turn with an agent (its latest published version, for example the built-in Architect). Starts a
        turn in a conversation (a new one unless `conversation_id` continues one), waits at most
        `wait_seconds` (<= 300) and returns {text, run_id, conversation_id, status, display_status, done,
        pending_approvals}. A run that waits for approval is not done: decide each pending approval with
        approvals.decide, then read the finished run at pydanticos://runs/{run_id}/report (or send the next
        message on the same conversation once it is done). `done` false otherwise means the run goes on."""
        from eap_api.routes.approvals import approval_json, get_approval_store
        from eap_api.routes.conversations import create_conversation, start_turn
        from eap_api.schemas import ConversationCreate, TurnCreate

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        if len(message.encode("utf-8")) > MAX_CHAT_MESSAGE_BYTES:
            raise McpToolError("payload_too_large", "message must be at most 32 KiB")
        aid = uid(agent_id, "agent_id")
        if conversation_id is None:
            created = await call(
                ctx, create_conversation, agent_id=aid, body=_model(ConversationCreate), principal=ctx.actor,
                db=DB, stores=ctx.stores,
            )
            conversation_id = created["id"]
        started = await call(
            ctx, start_turn, conversation_id=uid(conversation_id, "conversation_id"),
            body=_model(TurnCreate, run_id=uuid.uuid4(), input=message), principal=ctx.actor, db=DB, stores=ctx.stores,
            launcher=ctx.launcher,
        )
        run_id = started["run_id"]
        run, _settled = await wait_for_run(ctx, run_id, min(wait_seconds, MAX_WAIT_SECONDS))
        text, truncated = clip(run.get("output"))
        terminal = run["status"] in TERMINAL_RUN_STATUSES
        pending: list[dict[str, Any]] = []
        if run.get("display_status") == "waiting_approval":
            store = get_approval_store(ctx.request, ctx.stores)
            rows, _ = store.list(ctx.tenant_id, status="pending", kind=None, agent_id=str(agent_id), cursor=None, limit=100)
            for row in rows:
                if str(row["run_id"]) == str(run_id):
                    item = approval_json(row)
                    pending.append({k: item[k] for k in ("kind", "approval_key", "tool_name", "prompt", "args", "deadline")}
                                   | {"run_id": run_id})
        return {
            "text": text, "text_truncated": truncated, "run_id": run_id, "conversation_id": conversation_id,
            "status": run["status"], "display_status": run["display_status"], "done": terminal,
            "error": run.get("error"), "pending_approvals": pending,
        }

    # ------------------------------------------------------------------------------- workflows

    @tool("workflow.save_draft", manage=True)
    async def workflow_save_draft(spec: dict[str, Any], workflow_id: str | None = None) -> dict[str, Any]:
        """Create a workflow from `spec` (a WorkflowSpec), or replace the draft of `workflow_id`."""
        from eap_api.routes.workflows import create_workflow, patch_workflow
        from eap_api.workflows.schemas import WorkflowCreate, WorkflowUpdate

        ctx = current_ctx()
        guard_json(spec, "spec")
        if workflow_id is None:
            require_unrestricted(ctx, "create workflows")
            return await call(ctx, create_workflow, request=_model(WorkflowCreate, spec=spec), principal=ctx.actor, db=DB)
        require_workflow(ctx, workflow_id)
        return await call(ctx, patch_workflow, workflow_id=uid(workflow_id, "workflow_id"),
                          request=_model(WorkflowUpdate, spec=spec), principal=ctx.actor, db=DB)

    @tool("workflow.publish", manage=True)
    async def workflow_publish(workflow_id: str) -> dict[str, Any]:
        """Publish the workflow's draft as the next immutable version (409 when it equals the latest)."""
        from eap_api.routes.workflows import publish_workflow_version

        ctx = current_ctx()
        require_workflow(ctx, workflow_id)
        return await call(ctx, publish_workflow_version, workflow_id=uid(workflow_id, "workflow_id"),
                          principal=ctx.actor, db=DB)

    @tool("workflow.archive", manage=True)
    async def workflow_archive(workflow_id: str, archived: bool = True, force: bool = False) -> dict[str, Any]:
        """Archive (or, with archived=false, unarchive) a workflow, any workflow of the tenant. Archiving
        pauses its schedules and is refused (409 runs_active) while runs are queued or running, unless
        force=true; an archived workflow refuses new runs and schedules (workflow_archived). Unarchiving
        does not resume schedules. Returns the workflow with `archived`, `archived_at`, `paused_schedules`."""
        from eap_api.routes.workflows import WorkflowArchive, archive_workflow

        ctx = current_ctx()
        require_workflow(ctx, workflow_id)
        return await call(ctx, archive_workflow, workflow_id=uid(workflow_id, "workflow_id"), principal=ctx.actor,
                          db=DB, body=WorkflowArchive(archived=archived, force=force))

    @tool("workflow.run.start", manage=True)
    async def workflow_run_start(workflow_id: str, input: dict[str, Any], version: int | None = None,
                                 run_id: str | None = None) -> dict[str, Any]:
        """Start a run of a published workflow version (the latest unless `version`). `run_id` makes a retry
        idempotent. Returns {run_id, status, ...}; follow it with workflow.run.wait and workflow.run.result."""
        from eap_api.routes.workflow_runs import WorkflowRunCreate, start_workflow_run

        ctx = current_ctx()
        require_workflow(ctx, workflow_id)
        guard_json(input, "input", max_bytes=32 * 1024)
        body = _model(WorkflowRunCreate, input=input, version=version, run_id=uid(run_id, "run_id") if run_id else None)
        return await call(ctx, start_workflow_run, workflow_id=uid(workflow_id, "workflow_id"), principal=ctx.actor,
                          body=body, db=DB, stores=ctx.stores, launcher=ctx.launcher)

    @tool("workflow.run.cancel", manage=True)
    async def workflow_run_cancel(run_id: str) -> dict[str, Any]:
        """Cancel a workflow run and its whole tree (idempotent)."""
        from eap_api.routes.workflow_runs import cancel_run

        ctx = current_ctx()
        await tenant_run(ctx, run_id, kind="workflow")
        return await call(ctx, cancel_run, run_id=uid(run_id, "run_id"), principal=ctx.actor, stores=ctx.stores,
                          launcher=ctx.launcher)

    @tool("workflow.schedule.set", manage=True)
    async def workflow_schedule_set(workflow_id: str, name: str | None = None, cron: str | None = None,
                                    timezone: str = "UTC", input: dict[str, Any] | None = None,
                                    schedule_id: str | None = None, active: bool | None = None) -> dict[str, Any]:
        """Create a schedule (name and cron required; it runs the latest version with `input`), or with
        `schedule_id` change that schedule's fields or pause/resume it with `active`."""
        from eap_api.routes.schedules import create_schedule, get_schedule_store, update_schedule
        from eap_schemas.workflows import WorkflowScheduleSpec, WorkflowScheduleUpdate

        ctx = current_ctx()
        require_workflow(ctx, workflow_id)
        store = get_schedule_store(ctx.request, ctx.stores)
        if input is not None:
            guard_json(input, "input", max_bytes=32 * 1024)
        if schedule_id is None:
            if name is None or cron is None:
                raise McpToolError("invalid", "name and cron are required to create a schedule")
            body = _model(WorkflowScheduleSpec, name=name, cron=cron, timezone=timezone, input=input or {})
            return await call(ctx, create_schedule, workflow_id=uid(workflow_id, "workflow_id"), body=body,
                              principal=ctx.actor, db=DB, store=store)
        given = {k: v for k, v in (("name", name), ("cron", cron), ("input", input), ("active", active)) if v is not None}
        if timezone != "UTC":
            given["timezone"] = timezone
        existing = store.get(ctx.tenant_id, str(uid(schedule_id, "schedule_id")))
        if existing is None or existing["workflow_id"] != str(uid(workflow_id, "workflow_id")):
            raise McpToolError("not_found", "Schedule not found")
        return await call(ctx, update_schedule, schedule_id=str(uid(schedule_id, "schedule_id")),
                          body=_model(WorkflowScheduleUpdate, **given), principal=ctx.actor, db=DB, store=store)

    # ------------------------------------------------------------------------------- knowledge

    @tool("kb.create", manage=True)
    async def kb_create(name: str, embedding_model: str, description: str | None = None,
                        chunking: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create a knowledge base. `embedding_model` must be on the platform's allowlist (kb.list shows the
        models existing bases use)."""
        from eap_api.routes.knowledge import create_knowledge_base, get_knowledge_store
        from eap_api.schemas import KnowledgeBaseCreate

        ctx = current_ctx()
        body = _model(KnowledgeBaseCreate, name=name, embedding_model=embedding_model, description=description,
                      chunking=chunking)
        return await call(ctx, create_knowledge_base, body=body, principal=ctx.actor,
                          store=get_knowledge_store(ctx.request))

    @tool("kb.source.create", manage=True)
    async def kb_source_create(kb_id: str, connection_id: str, name: str, provider: str,
                               config: dict[str, Any] | None = None, kind: str | None = None,
                               enabled: bool = True) -> dict[str, Any]:
        """Add a data source to a knowledge base over a connection (provider = the adapter name, config as that
        adapter validates it)."""
        from eap_api.routes.kb_sources import create_source

        ctx = current_ctx()
        guard_json(config or {}, "config", max_bytes=64 * 1024, max_depth=6)
        payload: dict[str, Any] = {"connection_id": connection_id, "name": name, "provider": provider,
                                   "config": config or {}, "enabled": enabled}
        if kind is not None:
            payload["kind"] = kind
        return await call(ctx, create_source, kb_id=str(uid(kb_id, "kb_id")), request=ctx.request,
                          principal=ctx.actor, payload=payload)

    @tool("kb.source.sync", manage=True)
    async def kb_source_sync(kb_id: str, source_id: str, mode: Literal["targeted", "incremental", "full"],
                             part_numbers: list[str] | None = None, restart: bool = False,
                             run_id: str | None = None) -> dict[str, Any]:
        """Start a sync of a data source (the shared launcher: eap_run_store.kb_source_launch.launch_sync).
        Read progress with kb.source.syncs."""
        from eap_api.routes.kb_sources import start_sync

        ctx = current_ctx()
        payload: dict[str, Any] = {"mode": mode, "part_numbers": part_numbers or [], "restart": restart}
        if run_id:
            payload["run_id"] = run_id
        return await call(ctx, start_sync, kb_id=str(uid(kb_id, "kb_id")), source_id=str(uid(source_id, "source_id")),
                          request=ctx.request, principal=ctx.actor, launcher=ctx.launcher, payload=payload)

    # ------------------------------------------------------------------------------- settings and MCP servers

    @tool("settings.connection.create", manage=True)
    async def connection_create(name: str, config: dict[str, Any], credential_refs: dict[str, str] | None = None,
                                base_url: str | None = None, kind: str | None = None, enabled: bool = True) -> dict[str, Any]:
        """Register a connection to an external provider (`config.provider` names the adapter). Credentials are
        `secret:<name>` references to secrets stored through Studio; a value is refused, and no tool stores
        or reads a secret."""
        from eap_api.routes.connections import create_connection

        ctx = current_ctx()
        guard_json(config, "config", max_bytes=64 * 1024, max_depth=6)
        payload: dict[str, Any] = {"name": name, "config": config, "credential_refs": credential_refs or {},
                                   "enabled": enabled}
        for key, value in (("kind", kind), ("base_url", base_url)):
            if value is not None:
                payload[key] = value
        return await call(ctx, create_connection, request=ctx.request, principal=ctx.actor, payload=payload)

    @tool("mcp.server.install", manage=True)
    async def mcp_server_install(catalog_name: str, package_index: int | None = None, remote_index: int | None = None,
                                 name: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
        """Install a catalog entry for the tenant's agents: a package becomes a managed server (started at
        once), a remote registers as an external one. `env` carries variables the entry declares; a variable
        the entry marks secret must be a `secret:<name>` reference (values are refused, headers are not
        accepted here): store the secret in Studio first."""
        from eap_api.routes.mcp_servers import install_from_catalog
        from eap_run_store.mcp_catalog import CatalogStore

        ctx = current_ctx()
        env = env or {}
        entry = CatalogStore(ctx.engine).get(catalog_name) if env else None
        if env and entry is not None:
            index = package_index or 0
            declared = {v.name: v for v in entry.packages[index].environment_variables} if index < len(entry.packages) else {}
            for var, value in env.items():
                spec = declared.get(var)
                if spec is not None and spec.secret and not str(value).startswith("secret:"):
                    raise McpToolError(
                        "secret_value_refused",
                        f"{var} is a secret variable: store it in Studio and pass a secret:<name> reference",
                    )
        payload: dict[str, Any] = {"catalog_name": catalog_name}
        for key, value in (("package_index", package_index), ("remote_index", remote_index), ("name", name)):
            if value is not None:
                payload[key] = value
        if env:
            payload["env"] = env
        from eap_api.mcp_servers.managed import get_mcp_runtime
        from eap_api.routes.mcp_servers import get_outbound_policy

        return await call(ctx, install_from_catalog, request=ctx.request, principal=ctx.actor, db=DB,
                          policy=_override(ctx, get_outbound_policy), runtime=_override(ctx, get_mcp_runtime),
                          payload=payload)

    @tool("mcp.server.runtime", manage=True)
    async def mcp_server_runtime(server_id: str, action: Literal["start", "stop", "restart"]) -> dict[str, Any]:
        """Start, stop or restart a managed MCP server's pod; returns the runtime state."""
        from eap_api.mcp_servers.managed import get_mcp_runtime
        from eap_api.routes.mcp_servers import control_runtime

        ctx = current_ctx()
        return await call(ctx, control_runtime, server_id=uid(server_id, "server_id"), action=action,
                          principal=ctx.actor, db=DB, runtime=_override(ctx, get_mcp_runtime))

    # ------------------------------------------------------------------------------- approvals

    @tool("approvals.decide", manage=True)
    async def approvals_decide(run_id: str, kind: Literal["tool", "step"], key: str,
                               decision: Literal["approve", "deny"], comment: str | None = None,
                               resume_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Decide a pending approval: `kind` tool (key = tool_call_id) or step (key = step_id of a workflow
        approval or input step; an input step takes `resume_data` instead of a bare approve). A denial's
        comment reaches the model. Returns {decision, decided_by, notified, ...}; 409 when already decided."""
        from eap_api.routes.approvals import ToolApprovalDecision, approve_tool_call, deny_tool_call
        from eap_api.routes.workflow_runs import (
            ApprovalDecision, ResumeSubmission, approve_step, reject_step, resume_step,
        )

        ctx = current_ctx()
        await tenant_run(ctx, run_id)
        rid = uid(run_id, "run_id")
        common = {"run_id": rid, "principal": ctx.actor, "stores": ctx.stores, "launcher": ctx.launcher}
        if kind == "tool":
            body = _model(ToolApprovalDecision, comment=comment)
            fn = approve_tool_call if decision == "approve" else deny_tool_call
            return await call(ctx, fn, tool_call_id=key, body=body, **common)
        if resume_data is not None:
            guard_json(resume_data, "resume_data", max_bytes=32 * 1024)
            return await call(ctx, resume_step, step_id=key, body=_model(ResumeSubmission, data=resume_data, comment=comment), **common)
        fn = approve_step if decision == "approve" else reject_step
        return await call(ctx, fn, step_id=key, body=_model(ApprovalDecision, comment=comment), **common)


def _override(ctx: Any, dependency: Any) -> Any:
    """What FastAPI would inject for `dependency` (honours a dependency override)."""
    override = ctx.app.dependency_overrides.get(dependency)
    return (override or dependency)()
