"""Connection test and tool discovery for a registered MCP server.

FastMCP's client (the client Pydantic AI's MCP support is built on) over Streamable HTTP, with the
outbound policy's guarded HTTP client: the URL is re-checked on every request and the host is
re-resolved and re-checked on every new connection (DNS rebinding). The whole exchange (connect,
initialize, tools/list) is bounded by one deadline.

Failures are reduced to a stable error code (`error_code`); exception text never leaves this module
except through `log_failure`'s local-only debug record.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2

from eap_secrets.outbound import OutboundBlocked, OutboundPolicy

TIMEOUT_ENV = "EAP_MCP_TEST_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 30.0
MAX_TOOLS = 500


def connection_timeout_s() -> float:
    """EAP_MCP_TEST_TIMEOUT_SECONDS, clamped to [0.5, 30]; 10 when unset or unreadable."""
    try:
        value = float(os.environ.get(TIMEOUT_ENV, DEFAULT_TIMEOUT_S))
    except ValueError:
        value = DEFAULT_TIMEOUT_S
    return min(max(value, 0.5), MAX_TIMEOUT_S)


@dataclass
class ProbeResult:
    status: str  # "ok" | "error"
    tools: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None
    error: BaseException | None = None


def _walk(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack = [exc]
    while stack:
        e = stack.pop(0)
        if id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        if e.__cause__ is not None:
            stack.append(e.__cause__)
        if e.__context__ is not None:
            stack.append(e.__context__)


def error_code(exc: BaseException, http_status: int | None = None) -> str:
    """A stable code for a failed connection test (never exception text). `http_status` is the
    first non-2xx status the server answered with (the MCP SDK reports those as a generic error)."""
    chain = list(_walk(exc))
    for e in chain:
        if isinstance(e, OutboundBlocked):
            return e.code
    if http_status is not None:
        if http_status in (401, 403):
            return "unauthorized"
        if 300 <= http_status < 400:
            return "redirect_refused"
        return "http_status"
    for e in chain:
        if isinstance(e, httpx2.HTTPStatusError):
            return "unauthorized" if e.response.status_code in (401, 403) else "http_status"
    for e in chain:
        if isinstance(e, (TimeoutError, httpx2.TimeoutException)):
            return "timeout"
    for e in chain:
        if isinstance(e, ssl.SSLError):
            return "tls_failed"
    for e in chain:
        if isinstance(e, (httpx2.ConnectError, ConnectionError)):
            return "connect_failed"
    for e in chain:
        if isinstance(e, httpx2.TransportError):
            return "transport_error"
    for e in chain:
        if type(e).__name__ == "McpError":
            return "protocol_error"
    return "connection_failed"


def _tool_json(tool: Any) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema if isinstance(tool.input_schema, dict) else {},
    }


async def list_server_tools(
    url: str, headers: dict[str, str], policy: OutboundPolicy, timeout_s: float
) -> ProbeResult:
    """Connect, initialize and list the server's tools within `timeout_s`; never raises."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    statuses: list[int] = []

    async def record_status(response: httpx2.Response) -> None:
        if response.status_code >= 300:
            statuses.append(response.status_code)

    def client_factory(**kwargs: Any) -> httpx2.AsyncClient:
        client = policy.httpx_client_factory(**kwargs)
        client.event_hooks.setdefault("response", []).append(record_status)
        return client

    try:
        policy.check_url(url)
        transport = StreamableHttpTransport(url, headers=headers, httpx_client_factory=client_factory)
        with anyio.fail_after(timeout_s):
            async with Client(transport, timeout=timeout_s, init_timeout=timeout_s) as client:
                tools = await client.list_tools()
    except Exception as exc:  # noqa: BLE001 - reduced to a code; recorded by the caller
        code = error_code(exc, statuses[0] if statuses else None)
        return ProbeResult(status="error", error_code=code, error=exc)
    return ProbeResult(status="ok", tools=[_tool_json(t) for t in tools[:MAX_TOOLS]])
