"""One classifier for failed outbound MCP connections, shared by the API (connection test) and the
worker (tool discovery and calls), so both report the same stable code for the same failure.

Codes, in precedence order (the first that matches anywhere in the exception chain wins):

- the outbound policy's own code (OutboundBlocked.code: address_blocked, url_scheme_not_allowed, ...)
- a guard's code (an OutboundGuardError subclass: redirect_refused, response_too_large,
  unsupported_encoding)
- the first non-2xx HTTP status the server answered (when the caller recorded it), or an
  httpx2.HTTPStatusError: 401/403 -> unauthorized, 3xx -> redirect_refused, other -> http_status
- timeout (TimeoutError, httpx2.TimeoutException)
- tls_failed (ssl.SSLError)
- connect_failed: the TCP connection was refused or could not be made (httpx2.ConnectError,
  ConnectionError) -- and only that
- transport_error (any other httpx2.TransportError)
- protocol_error (the MCP SDK's MCPError: the server did not speak MCP as expected)
- connection_failed: anything else

Exception text is never part of a code. This module imports no MCP SDK (the import contract keeps
eap_secrets free of it): MCP errors are recognised by class name.
"""

from __future__ import annotations

import ssl
from collections.abc import Iterator

import httpx2

from eap_secrets.outbound import OutboundBlocked

__all__ = [
    "OutboundGuardError",
    "RedirectRefused",
    "failure_code",
    "refuse_redirect",
    "walk_chain",
]

_MCP_ERROR_NAMES = frozenset({"MCPError", "McpError"})


class OutboundGuardError(Exception):
    """A response guard refused an answer; `code` is its stable failure code."""

    code = "connection_failed"


class RedirectRefused(OutboundGuardError):
    """The server answered with a redirect (3xx). Redirects are never followed (the policy's client
    has follow_redirects=False); `refuse_redirect` raises this before the body is read."""

    code = "redirect_refused"


async def refuse_redirect(response: httpx2.Response) -> None:
    """httpx response hook: a 3xx answer is refused before any body byte is read."""
    if 300 <= response.status_code < 400:
        raise RedirectRefused()


def walk_chain(exc: BaseException) -> Iterator[BaseException]:
    """`exc`, its exception-group members, causes and contexts, each once (breadth first)."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop(0)
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        stack.extend(e for e in (cur.__cause__, cur.__context__) if e is not None)


def _status_code(status: int) -> str:
    if status in (401, 403):
        return "unauthorized"
    if 300 <= status < 400:
        return "redirect_refused"
    return "http_status"


def failure_code(exc: BaseException, http_status: int | None = None) -> str:
    """The stable code for a failed MCP connection, listing or call (see the module docstring).
    `http_status` is the first non-2xx status the server answered with, if the caller recorded it
    (the MCP SDK reports those as a generic error)."""
    chain = list(walk_chain(exc))
    for e in chain:
        if isinstance(e, OutboundBlocked):
            return e.code
    for e in chain:
        if isinstance(e, OutboundGuardError):
            return e.code
    if http_status is not None and http_status >= 300:
        return _status_code(http_status)
    for e in chain:
        if isinstance(e, httpx2.HTTPStatusError):
            return _status_code(e.response.status_code)
    if any(isinstance(e, (TimeoutError, httpx2.TimeoutException)) for e in chain):
        return "timeout"
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return "tls_failed"
    if any(isinstance(e, (httpx2.ConnectError, ConnectionError)) for e in chain):
        return "connect_failed"
    if any(isinstance(e, httpx2.TransportError) for e in chain):
        return "transport_error"
    if any(type(e).__name__ in _MCP_ERROR_NAMES for e in chain):
        return "protocol_error"
    return "connection_failed"
