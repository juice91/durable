"""The external MCP server: the run surface of external agents at `/api/v1/external/mcp` (design 5.1).

A second FastMCP server, stateless, apart from the control endpoint: it shares no tool with it and registers none of its
own. One middleware answers `tools/list` and `tools/call` from the caller's key: the fixed run tools plus one
`run_<export>` per export the key reaches. Every call is one or more calls into `ExternalRunService`, which holds every
rule (ownership, idempotency, quotas, deadlines, audit); a refusal is `{error: {code, message}}` with MCP isError.
There is no tool that decides an approval or answers a step.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult
from jsonschema import Draft202012Validator
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_REQUEST, ToolAnnotations
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from eap_api.auth import run_actor
from eap_api.deps import database_engine_of, external_limiter_of, open_session
from eap_api.external_mcp import schemas
from eap_api.external_mcp.guard import APP_SCOPE_KEY, PRINCIPAL_SCOPE_KEY, RETRY_SCOPE_KEY, ExternalMcpASGI
from eap_api.external_settings import ExternalSettings
from eap_api.service_deps.external_access import external_run_service_of
from eap_api.services.errors import Forbidden, InvalidInput, ServiceError, TooManyRequests
from eap_api.services.external_wait import wait_for_run

log = logging.getLogger("eap_api.external_mcp")

MOUNT_PATH = "/api/v1/external/mcp"
PROTOCOL = "mcp"
LIST_TTL_SECONDS = 30  # tools/list differs by key and changes when an admin changes an export
MAX_REPORTED_ERRORS = 10
INSTRUCTIONS = (
    "Run the workflows this tenant exported to you. Each `run_<name>` tool starts one workflow and answers a run view "
    "{run_id, export, state, done, result?, error?, waiting?, deadline, poll_after_seconds}. Follow a run with "
    "runs.wait or runs.get, list your own with runs.list, cancel one with runs.cancel. A run in `input_required` waits "
    "for a person in the platform: it cannot be approved or answered from here. Errors are {error: {code, message}}."
)


class _Call:
    """One request's caller. Each service call gets its own database session, in the threadpool."""

    def __init__(self) -> None:
        scope = get_http_request().scope
        self.app = scope[APP_SCOPE_KEY]
        self.principal = scope[PRINCIPAL_SCOPE_KEY]
        self.retry_after: int | None = scope[RETRY_SCOPE_KEY]

    async def service(self, method: str, *args: Any, **kwargs: Any) -> Any:
        def go() -> Any:
            with open_session(database_engine_of(self.app)) as session:
                return getattr(external_run_service_of(self.app, session), method)(self.principal, *args, **kwargs)

        return await run_in_threadpool(go)

    async def exports(self) -> list[dict[str, Any]]:
        return sorted(await self.service("exports", PROTOCOL), key=lambda export: export["name"])


def _tool(name: str, description: str, parameters: dict[str, Any], *, read_only: bool, title: str | None = None) -> Tool:
    return Tool(
        name=name, title=title, description=description, parameters=parameters, output_schema=None,
        annotations=ToolAnnotations(read_only_hint=read_only),
    )


def _check(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda e: [str(p) for p in e.absolute_path])
    if not errors:
        return
    # The location and the failing keyword only: jsonschema's message quotes the rejected value.
    details = [
        {"loc": [str(p)[:100] for p in error.absolute_path], "type": str(error.validator)}
        for error in errors[:MAX_REPORTED_ERRORS]
    ]
    raise InvalidInput("the arguments do not match the tool's input schema", code="invalid_arguments", details=details)


async def _follow(call: _Call, view: dict[str, Any], seconds: int) -> dict[str, Any]:
    return await wait_for_run(view, lambda: call.service("get", view["run_id"]), seconds)


async def _start(call: _Call, export: str, args: dict[str, Any]) -> dict[str, Any]:
    deadline = args.get("deadline_seconds")
    view = await call.service(
        "start", PROTOCOL, export, args["input"], idempotency_key=args.get("idempotency_key"),
        deadline_seconds=None if deadline is None else int(deadline),
    )
    try:
        return await _follow(call, view, int(args.get("wait_seconds", schemas.DEFAULT_WAIT_SECONDS)))
    except Forbidden:
        raise
    except Exception:  # noqa: BLE001 - the run exists: its id must reach the caller even if watching it failed
        log.exception("external mcp: waiting for a started run failed")
        return view


async def _fixed(call: _Call, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "exports.list":
        return {"exports": await call.exports()}
    if name == "runs.list":
        return await call.service(
            "list", state=args.get("status"), cursor=args.get("cursor"),
            limit=int(args.get("limit", schemas.DEFAULT_PAGE)),
        )
    if name == "runs.cancel":
        return (await call.service("cancel", args["run_id"]))["run"]
    view = await call.service("get", args["run_id"])
    if name == "runs.wait":
        return await _follow(call, view, int(args.get("timeout_seconds", schemas.DEFAULT_TIMEOUT_SECONDS)))
    return view


def _result(data: dict[str, Any], *, is_error: bool = False) -> ToolResult:
    return ToolResult(structured_content=json.loads(json.dumps(data, default=str)), is_error=is_error)


def _error(code: str, message: str, **extra: Any) -> ToolResult:
    return _result({"error": {"code": code, "message": message, **extra}}, is_error=True)


class RunSurface(Middleware):
    async def on_list_tools(self, context: MiddlewareContext, call_next: Any) -> list[Tool]:
        call = _Call()
        try:
            exports = await call.exports()
        except ServiceError as exc:
            raise MCPError(code=INVALID_REQUEST, message=exc.message) from None
        except Exception:  # noqa: BLE001 - never an internal message to the caller
            log.exception("external mcp: tools/list failed")
            raise MCPError(code=INTERNAL_ERROR, message="the call failed") from None
        fixed = [
            _tool(name, description, parameters, read_only=read_only)
            for name, (description, parameters, read_only) in schemas.FIXED_TOOLS.items()
        ]
        return fixed + [
            _tool(
                schemas.RUN_PREFIX + export["name"], schemas.run_tool_description(export),
                schemas.run_tool_schema(export), read_only=False, title=export["title"],
            )
            for export in exports
        ]

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> ToolResult:
        name = context.message.name
        arguments = context.message.arguments or {}
        export = name.removeprefix(schemas.RUN_PREFIX) if name.startswith(schemas.RUN_PREFIX) else None
        if export is None and name not in schemas.FIXED_TOOLS:
            return _error("unknown_tool", "Unknown tool")
        call = _Call()
        who = run_actor(call.principal).subject
        label = "run_<export>" if export is not None else name  # the log never carries a name off the wire
        if call.retry_after is not None:
            log.info("external mcp call tool=%s outcome=rate_limited key=%s", label, who)
            return _error("rate_limited", "rate limit exceeded", retry_after_seconds=call.retry_after)
        settings = ExternalSettings.from_env()
        limiter = external_limiter_of(call.app, settings.concurrent, settings.key_concurrent)
        lease = limiter.acquire(f"{call.principal.tenant_id}:{who}")
        if lease is None:
            log.info("external mcp call tool=%s outcome=rate_limited key=%s", label, who)
            return _error("rate_limited", "too many concurrent calls for this key; retry shortly", retry_after_seconds=1)
        try:
            if export is not None:
                _check(schemas.run_tool_arguments({"type": "object"}), arguments)
                data = await _start(call, export, arguments)
            else:
                _check(schemas.FIXED_TOOLS[name][1], arguments)
                data = await _fixed(call, name, arguments)
        except ServiceError as exc:
            log.info("external mcp call tool=%s outcome=error code=%s key=%s", label, exc.code, who)
            extra: dict[str, Any] = {}
            if exc.details:
                extra["details"] = exc.details
            if isinstance(exc, TooManyRequests):
                extra["retry_after_seconds"] = exc.retry_after
            return _error(exc.code, exc.message, **extra)
        except Exception:  # noqa: BLE001 - never a traceback or an internal message to the caller
            log.exception("external mcp tool %s failed", label)
            return _error("internal", "the call failed")
        finally:
            lease.release()
        log.info("external mcp call tool=%s outcome=ok key=%s", label, who)
        return _result(data)


def build_server() -> FastMCP:
    return FastMCP(
        "Durable workflows", instructions=INSTRUCTIONS, middleware=[RunSurface()], mask_error_details=True,
        cache_ttl=LIST_TTL_SECONDS, cache_scope="private",
    )


def mount_external_mcp(app: Any) -> Any:
    """Serve the run surface at `/api/v1/external/mcp` on `app`; the inner app, whose lifespan the app must run."""
    inner = build_server().http_app(path=MOUNT_PATH, stateless_http=True)
    app.router.routes.append(Route(MOUNT_PATH, ExternalMcpASGI(app, inner)))
    return inner
