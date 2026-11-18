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
import os
from contextvars import ContextVar
from typing import Any, Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.base import ToolResult
from starlette.responses import JSONResponse
from starlette.routing import Route

from eap_api.mcp_control.audit import audit_call
from eap_api.mcp_control.auth import PRINCIPAL_SCOPE_KEY, McpAuthError, authenticate, has_manage
from eap_api.mcp_control.routing import Ctx, McpToolError
from eap_api.sse import StreamLimiter

log = logging.getLogger("eap_api.mcp_control")

MOUNT_PATH = "/api/v1/mcp"
# A result or resource is cut to this many bytes (the shared bound; tools bound their own lists further).
MAX_RESULT_BYTES = 256 * 1024
INSTRUCTIONS = (
    "PydanticOS control plane for one tenant. Tools are named by function: agent.*, workflow.*, kb.*, "
    "settings.*, mcp.*, approvals.*. Read tools need any API key of the tenant; tools that change anything "
    "need a key with the `manage` scope. Errors are {error: {code, message}}. Secrets are never exposed."
)

_ctx: ContextVar[Ctx] = ContextVar("eap_mcp_ctx")


def current_ctx() -> Ctx:
    """The calling tenant's context, inside a tool or resource body."""
    return _ctx.get()


def _limiter(app: Any) -> StreamLimiter:
    limiter = getattr(app.state, "mcp_limiter", None)
    if limiter is None:
        limiter = app.state.mcp_limiter = StreamLimiter(
            max_global=int(os.environ.get("EAP_MCP_MAX_CONCURRENT_GLOBAL", "200")),
            max_per_principal=int(os.environ.get("EAP_MCP_MAX_CONCURRENT_PER_KEY", "10")),
        )
    return limiter


def _bound(data: Any) -> Any:
    """A result over MAX_RESULT_BYTES is replaced by a truncation notice (tools page their lists well below it)."""
    text = json.dumps(data, default=str)
    if len(text.encode("utf-8")) <= MAX_RESULT_BYTES:
        return data
    return {"truncated": True, "bytes": len(text.encode("utf-8")), "preview": text[:2000]}


class _Guard:
    """The per-call envelope shared by tools and resources."""

    def __init__(self, name: str, manage: bool) -> None:
        self.name, self.manage = name, manage

    def enter(self) -> tuple[Ctx, Any, str]:
        request = get_http_request()
        principal = request.scope[PRINCIPAL_SCOPE_KEY]
        app = request.scope["eap.app"]
        ctx = Ctx(app=app, principal=principal)
        who = principal.key_id or f"dev:{principal.user_id}"
        if self.manage and not has_manage(principal):
            audit_call(tool=self.name, tenant_id=ctx.tenant_id, principal_id=who, outcome="denied", code="scope_required")
            raise McpToolError("scope_required", f"{self.name} needs an API key with the manage scope")
        lease = _limiter(app).acquire(f"{ctx.tenant_id}:{who}")
        if lease is None:
            audit_call(tool=self.name, tenant_id=ctx.tenant_id, principal_id=who, outcome="rate_limited", code="rate_limited")
            raise McpToolError("rate_limited", "too many concurrent calls for this key; retry shortly")
        return ctx, lease, who


def register(mcp: FastMCP) -> Callable[..., Callable[[Callable[..., Any]], Callable[..., Any]]]:
    """`tool(name, manage=False)` and `resource(uri)` decorators bound to `mcp`."""

    def tool(name: str, *, manage: bool = False, description: str | None = None):
        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            guard = _Guard(name, manage)

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> ToolResult:
                lease = None
                try:
                    ctx, lease, who = guard.enter()
                except McpToolError as exc:
                    return ToolResult(structured_content=exc.body, is_error=True)
                token = _ctx.set(ctx)
                try:
                    data = await fn(*args, **kwargs)
                    audit_call(tool=name, tenant_id=ctx.tenant_id, principal_id=who, outcome="ok")
                    return ToolResult(structured_content=_bound(data))
                except McpToolError as exc:
                    audit_call(tool=name, tenant_id=ctx.tenant_id, principal_id=who, outcome="error", code=exc.code)
                    return ToolResult(structured_content=exc.body, is_error=True)
                except Exception:  # noqa: BLE001 - never a traceback to the client
                    log.exception("mcp tool %s failed", name)
                    audit_call(tool=name, tenant_id=ctx.tenant_id, principal_id=who, outcome="error", code="internal")
                    return ToolResult(
                        structured_content={"error": {"code": "internal", "message": "the call failed; see the platform logs"}},
                        is_error=True,
                    )
                finally:
                    _ctx.reset(token)
                    lease.release()

            mcp.tool(name=name, description=description or (fn.__doc__ or "").strip() or None, output_schema=None,
                     tags={"manage"} if manage else {"read"})(wrapper)
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
            principal = await authenticate(self.app, headers.get("authorization"), headers.get("origin"))
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
    from eap_api.mcp_control import resources, tools_manage, tools_read

    mcp = FastMCP("PydanticOS", instructions=INSTRUCTIONS)
    tool, resource = register(mcp)
    tools_read.register_tools(tool)
    tools_manage.register_tools(tool)
    resources.register_resources(resource)
    return mcp


def mount_mcp_control(app: Any) -> None:
    """Serve the control endpoint at POST /api/v1/mcp (and the transport's GET/DELETE) on `app`."""
    mcp = build_server()
    inner = mcp.http_app(path=MOUNT_PATH, stateless_http=True)
    app.state.mcp_control_app = inner
    handler = McpControlASGI(app, inner)
    app.router.routes.append(Route(MOUNT_PATH, handler))
