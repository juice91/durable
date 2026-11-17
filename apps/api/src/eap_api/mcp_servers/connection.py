"""Connection test and tool discovery for a registered MCP server.

FastMCP's client (the client Pydantic AI's MCP support is built on) over Streamable HTTP, with the
outbound policy's guarded HTTP client: the URL is re-checked on every request and the host is
re-resolved and re-checked on every new connection (DNS rebinding). The whole exchange (connect,
initialize, tools/list) is bounded by one deadline.

Failures are reduced to a stable error code (`error_code`); exception text never leaves this module
except through `log_failure`'s local-only debug record.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2

from eap_secrets.outbound import OutboundPolicy
from eap_secrets.outbound_errors import failure_code
from eap_secrets.redaction import SecretVariants, mask_split_secrets, redact, secret_variants

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


def error_code(exc: BaseException, http_status: int | None = None) -> str:
    """A stable code for a failed connection test (never exception text): the classifier the worker
    uses too (eap_secrets.outbound_errors). `http_status` is the first non-2xx status the server
    answered with (the MCP SDK reports those as a generic error)."""
    return failure_code(exc, http_status)


def _tool_json(tool: Any, variants: SecretVariants) -> dict[str, Any]:
    """One listed tool for the response, with every form of the server's header values scrubbed
    (a server may echo a header into a name, description or schema, including split across fields
    and base64/hex of a larger blob)."""
    listed = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema if isinstance(tool.input_schema, dict) else {},
    }
    # Split pieces first (they are invisible to a per-string scrub), then encodings and whole values.
    return redact(mask_split_secrets(listed, variants), variants)


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
    variants = secret_variants(headers.values())
    return ProbeResult(status="ok", tools=[_tool_json(t, variants) for t in tools[:MAX_TOOLS]])


MAX_RESULT_CHARS = 100_000  # a run's stored output; the worker bounds MCP results the same way


@dataclass
class CallResult:
    """One tools/call. `sent` is True once the request may have reached the server: a failure then
    leaves the outcome unknown (the tool may have run)."""

    status: str  # "ok" | "error"
    content: Any = None
    sent: bool = False
    error_code: str | None = None
    error: BaseException | None = None


def _result_content(result: Any, variants: SecretVariants) -> Any:
    """A CallToolResult's content as JSON, secrets scrubbed, bounded (as the worker stores it):
    structured content wins; text blocks become strings (one block: a bare string); other blocks
    only their type. An oversized result is cut before redaction."""
    from pydantic_core import to_jsonable_python

    if result.structured_content is not None:
        content: Any = to_jsonable_python(result.structured_content, fallback=str)
    else:
        parts: list[Any] = [
            b.text if getattr(b, "type", None) == "text" else {"type": str(getattr(b, "type", "unknown")), "omitted": True}
            for b in result.content
        ]
        content = parts[0] if len(parts) == 1 and isinstance(parts[0], str) else parts
    content = mask_split_secrets(content, variants)
    if len(json.dumps(content, default=str)) > MAX_RESULT_CHARS:
        text = redact(json.dumps(content, default=str)[: MAX_RESULT_CHARS + 256], variants)[:MAX_RESULT_CHARS]
        return {"truncated": True, "text": text}
    return redact(content, variants)


async def call_server_tool(
    url: str, headers: dict[str, str], policy: OutboundPolicy, timeout_s: float, tool: str, arguments: dict[str, Any]
) -> CallResult:
    """Connect, initialize and call one tool within `timeout_s`; never raises. A tool-reported error
    (`is_error`) is an error result without the server's text (it may quote anything)."""
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

    sent = False
    result: Any = None
    content: Any = None
    variants = secret_variants(headers.values())
    try:
        policy.check_url(url)
        transport = StreamableHttpTransport(url, headers=headers, httpx_client_factory=client_factory)
        with anyio.fail_after(timeout_s):
            async with Client(transport, timeout=timeout_s, init_timeout=timeout_s) as client:
                sent = True
                result = await client.call_tool_mcp(tool, arguments)
                if not result.is_error:
                    content = _result_content(result, variants)
    except Exception as exc:  # noqa: BLE001 - reduced to a code; recorded by the caller
        if result is not None and (result.is_error or content is not None):
            pass  # the answer arrived; only closing the session failed
        else:
            code = error_code(exc, statuses[0] if statuses else None) if not sent else "call_failed"
            return CallResult(status="error", sent=sent, error_code=code, error=exc)
    if result.is_error:
        return CallResult(status="error", sent=True, error_code="tool_error")
    return CallResult(status="ok", content=content, sent=True)
