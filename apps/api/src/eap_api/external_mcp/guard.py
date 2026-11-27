"""The door of `POST /api/v1/external/mcp`: the switch (`EAP_EXTERNAL_ACCESS_ENABLED`; off, the route answers like an
unknown one), then the checks every external call passes (`eap_api.external_door`). A `tools/call` over the per-key rate
is not refused at the door: it goes on flagged and only answers a `rate_limited` tool error."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from eap_api.external_door import enter, not_found, printable_label
from eap_api.external_settings import ExternalSettings

PRINCIPAL_SCOPE_KEY = "eap.external_principal"
RETRY_SCOPE_KEY = "eap.external_retry_after"
APP_SCOPE_KEY = "eap.app"
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"


def agent_label(message: Any, user_agent: str | None) -> str | None:
    """What the caller says it is (MCP `clientInfo`, else its user agent): bounded, printable, never trusted."""
    declared = None
    params = message.get("params") if isinstance(message, dict) else None
    if isinstance(params, dict):
        meta = params.get("_meta")
        info = meta.get(CLIENT_INFO_META) if isinstance(meta, dict) else None
        if info is None and message.get("method") == "initialize":
            info = params.get("clientInfo")
        if isinstance(info, dict) and isinstance(info.get("name"), str):
            version = info.get("version")
            declared = info["name"] + (f"/{version}" if isinstance(version, str) else "")
    return printable_label(declared or user_agent)


def _tool_call(message: Any) -> bool:
    return isinstance(message, dict) and message.get("method") == "tools/call"


class ExternalMcpASGI:
    def __init__(self, app: Any, inner: Any) -> None:
        self.app, self.inner = app, inner

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        settings = ExternalSettings.from_env()
        found = not_found() if not settings.enabled else await enter(
            self.app, Request(scope, receive), settings, label=agent_label, soft_limited=_tool_call
        )
        if isinstance(found, JSONResponse):
            await found(scope, receive, send)
            return
        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": found.body, "more_body": False}

        scope = {
            **scope, PRINCIPAL_SCOPE_KEY: found.principal, APP_SCOPE_KEY: self.app,
            RETRY_SCOPE_KEY: found.retry_after,
        }
        await self.inner(scope, replay, send)
