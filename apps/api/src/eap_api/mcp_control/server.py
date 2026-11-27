"""The MCP control server: PydanticOS's functional namespace over Streamable HTTP at `/api/v1/mcp`.

One FastMCP server per process, stateless (every request is authenticated again; no state beyond the
transport). `McpControlASGI` authenticates each HTTP request from `Authorization: Bearer` (a tenant API
key, or the dev bearer in dev mode), then hands the request to FastMCP with the principal in the ASGI
scope. `register` wraps every tool and resource: it reads the principal, enforces the `manage` scope
on mutations, takes a per-key concurrency slot, writes the audit record and turns every refusal into
`{error: {code, message}}` with MCP isError. A tool body is validation plus one call into the shared
service (`eap_api.mcp_control.routing`).
"""

from __future__ import annotations

import functools
import json
import logging
from contextvars import ContextVar
from typing import Any, Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.base import ToolResult
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from eap_api import control_catalog
from eap_api.audit import record_mcp_change
from eap_api.auth import bind_agui_actor
from eap_api.mcp_control.audit import audit_call
from eap_api.mcp_control.auth import PRINCIPAL_SCOPE_KEY, McpAuthError, actor_id, authenticate, has_manage
from eap_api.mcp_control.routing import Ctx, McpToolError
from eap_api.deps import mcp_limiter_of

log = logging.getLogger("eap_api.mcp_control")

DENIED_CODES = frozenset({"scope_required", "human_only", "forbidden"})

MOUNT_PATH = "/api/v1/mcp"
# A result or resource is cut to this many bytes (the shared bound; tools bound their own lists further).
MAX_RESULT_BYTES = 256 * 1024
INSTRUCTIONS = (
    "PydanticOS control plane for one tenant. Tools are named by function: agent.*, workflow.*, kb.*, "
    "settings.*, mcp.*, approvals.*, webhook.*. Read tools need any API key of the tenant; tools that change anything "
    "need a key with the `manage` scope. Errors are {error: {code, message}}. Secrets are never exposed."
)

_ctx: ContextVar[Ctx] = ContextVar("eap_mcp_ctx")


def current_ctx() -> Ctx:
    """The calling tenant's context, inside a tool or resource body."""
    return _ctx.get()


def _bound(data: Any) -> Any:
    """A result over MAX_RESULT_BYTES is replaced by a truncation notice (tools page their lists well below it)."""
    text = json.dumps(data, default=str)
    if len(text.encode("utf-8")) <= MAX_RESULT_BYTES:
        return data
    return {"truncated": True, "bytes": len(text.encode("utf-8")), "preview": text[:2000]}


class _Guard:
    """The per-call envelope shared by tools and resources."""

    def __init__(self, name: str, manage: bool, human_only: bool = False) -> None:
        self.name, self.manage, self.human_only = name, manage, human_only

    def enter(self) -> tuple[Ctx, Any, str]:
        request = get_http_request()
        principal = request.scope[PRINCIPAL_SCOPE_KEY]
        app = request.scope["eap.app"]
        ctx = Ctx(app=app, principal=principal)
        who = actor_id(principal)
        if self.human_only and principal.kind == "architect_run":
            # A tool that would let the Architect approve its own cards or drive itself.
            audit_call(tool=self.name, tenant_id=ctx.tenant_id, principal_id=who, outcome="denied", code="human_only")
            raise McpToolError("human_only", f"{self.name} is for a person or an external agent, not for the Architect")
        if self.manage and not has_manage(principal):
            audit_call(tool=self.name, tenant_id=ctx.tenant_id, principal_id=who, outcome="denied", code="scope_required")
            raise McpToolError("scope_required", f"{self.name} needs an API key with the manage scope")
        lease = mcp_limiter_of(app).acquire(f"{ctx.tenant_id}:{who}")
        if lease is None:
            audit_call(tool=self.name, tenant_id=ctx.tenant_id, principal_id=who, outcome="rate_limited", code="rate_limited")
            raise McpToolError("rate_limited", "too many concurrent calls for this key; retry shortly")
        return ctx, lease, who


def register(mcp: FastMCP) -> Callable[..., Callable[[Callable[..., Any]], Callable[..., Any]]]:
    """`tool(name, manage=False)` and `resource(uri)` decorators bound to `mcp`."""

    def tool(
        name: str, *, manage: bool = False, description: str | None = None, human_only: bool = False,
        internal: bool = False,
    ):
        """`human_only` keeps the tool away from the Architect's own `control__*` bridge (tag `human_only`).
        `internal` keeps it away from the Architect's model but lets the worker's own code call it as the
        Architect's run (tag `internal`): the bridge hides it from the model's toolset."""

        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            guard = _Guard(name, manage, human_only)
            control_catalog.record(
                name, manage=manage, description=description or fn.__doc__, human_only=human_only, internal=internal
            )

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> ToolResult:
                lease = None
                try:
                    ctx, lease, who = guard.enter()
                except McpToolError as exc:
                    return ToolResult(structured_content=exc.body, is_error=True)
                token = _ctx.set(ctx)
                bind_agui_actor(ctx.principal)  # a run this tool starts names who started it
                try:
                    if manage and ctx.principal.kind == "architect_run":
                        # Design 8: the Architect acts with the requesting user's rights and never more. A viewer's
                        # Architect turn gets the read tools only; the services check each resource for the same user.
                        if not await run_in_threadpool(ctx.access.allowed, "write", "tenant", None):
                            raise McpToolError("forbidden", f"{name} changes things, and the person this turn acts for may only read")
                    data = await fn(*args, **kwargs)
                    audit_call(tool=name, tenant_id=ctx.tenant_id, principal_id=who, outcome="ok")
                    if manage:
                        await record_mcp_change(ctx, name, "success")
                    return ToolResult(structured_content=_bound(data))
                except McpToolError as exc:
                    audit_call(tool=name, tenant_id=ctx.tenant_id, principal_id=who, outcome="error", code=exc.code)
                    if manage:
                        await record_mcp_change(ctx, name, "denied" if exc.code in DENIED_CODES else "failed")
                    return ToolResult(structured_content=exc.body, is_error=True)
                except Exception:  # noqa: BLE001 - never a traceback to the client
                    log.exception("mcp tool %s failed", name)
                    audit_call(tool=name, tenant_id=ctx.tenant_id, principal_id=who, outcome="error", code="internal")
                    if manage:
                        await record_mcp_change(ctx, name, "failed")
                    return ToolResult(
                        structured_content={"error": {"code": "internal", "message": "the call failed; see the platform logs"}},
                        is_error=True,
                    )
                finally:
                    _ctx.reset(token)
                    lease.release()

            mcp.tool(name=name, description=description or (fn.__doc__ or "").strip() or None, output_schema=None,
                     tags=({"manage"} if manage else {"read"}) | ({"human_only"} if human_only else set())
                     | ({"internal"} if internal else set()))(wrapper)
            return wrapper

        return decorate

    def resource(uri: str):
        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            guard = _Guard(uri, False)

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> str:
                try:
                    ctx, lease, who = guard.enter()
                except McpToolError as exc:
                    raise ResourceError(json.dumps(exc.body)) from None
                token = _ctx.set(ctx)
                try:
                    data = await fn(*args, **kwargs)
                    audit_call(tool="resource:" + uri.split("{")[0], tenant_id=ctx.tenant_id, principal_id=who, outcome="ok")
                    return json.dumps(_bound(data), default=str)
                except McpToolError as exc:
                    audit_call(tool="resource:" + uri.split("{")[0], tenant_id=ctx.tenant_id, principal_id=who,
                               outcome="error", code=exc.code)
                    raise ResourceError(json.dumps(exc.body)) from None
                finally:
                    _ctx.reset(token)
                    lease.release()

            mcp.resource(uri, mime_type="application/json")(wrapper)
            return wrapper

        return decorate

    return tool, resource  # type: ignore[return-value]


class McpControlASGI:
    """Authenticates a request, puts the principal in the scope and hands it to the FastMCP app."""

    def __init__(self, app: Any, inner: Any) -> None:
        self.app, self.inner = app, inner

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        try:
            client = scope.get("client")
            principal = await authenticate(
                self.app,
                headers.get("authorization"),
                headers.get("origin"),
                headers=headers,
                peer_host=client[0] if client else None,
            )
        except McpAuthError as exc:
            code = "unauthorized" if exc.status == 401 else "forbidden"
            response = JSONResponse(
                {"error": {"code": code, "message": exc.message}},
                status_code=exc.status,
                headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
            )
            await response(scope, receive, send)
            return
        scope = {**scope, PRINCIPAL_SCOPE_KEY: principal, "eap.app": self.app}
        await self.inner(scope, receive, send)


def build_server() -> FastMCP:
    from eap_api.mcp_control import resources, tools_evals, tools_manage, tools_read, tools_webhooks

    mcp = FastMCP("PydanticOS", instructions=INSTRUCTIONS)
    tool, resource = register(mcp)
    tools_read.register_tools(tool)
    tools_manage.register_tools(tool)
    tools_evals.register_tools(tool)
    tools_webhooks.register_tools(tool)
    resources.register_resources(resource)
    return mcp


def mount_mcp_control(app: Any) -> Any:
    """Serve the control endpoint at POST /api/v1/mcp (and the transport's GET/DELETE) on `app`."""
    mcp = build_server()
    inner = mcp.http_app(path=MOUNT_PATH, stateless_http=True)
    handler = McpControlASGI(app, inner)
    app.router.routes.append(Route(MOUNT_PATH, handler))
    return inner
