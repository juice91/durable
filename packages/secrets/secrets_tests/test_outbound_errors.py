"""The shared MCP failure classifier (API connection test and worker): one code per failure."""

from __future__ import annotations

import ssl

import httpx2
import pytest

from eap_secrets.outbound import OutboundBlocked
from eap_secrets.outbound_errors import OutboundGuardError, RedirectRefused, failure_code, refuse_redirect

REQ = httpx2.Request("POST", "http://127.0.0.1:1/mcp")


def status_error(code: int) -> httpx2.HTTPStatusError:
    return httpx2.HTTPStatusError(str(code), request=REQ, response=httpx2.Response(code, request=REQ))


class MCPError(Exception):
    """Stands in for mcp.shared.exceptions.MCPError (matched by class name; no MCP SDK here)."""


class McpError(Exception):
    """FastMCP's older spelling."""


class Guard(OutboundGuardError):
    code = "response_too_large"


def wrapped(inner: BaseException) -> BaseException:
    try:
        try:
            raise inner
        except BaseException as exc:
            raise RuntimeError("Connection closed") from exc
    except RuntimeError as outer:
        return outer


@pytest.mark.parametrize(
    ("exc", "http_status", "code"),
    [
        (OutboundBlocked("address_blocked"), None, "address_blocked"),
        (RedirectRefused(), None, "redirect_refused"),
        (Guard(), None, "response_too_large"),
        (RuntimeError("x"), 302, "redirect_refused"),
        (RuntimeError("x"), 401, "unauthorized"),
        (RuntimeError("x"), 403, "unauthorized"),
        (RuntimeError("x"), 500, "http_status"),
        (status_error(307), None, "redirect_refused"),
        (status_error(401), None, "unauthorized"),
        (status_error(404), None, "http_status"),
        (TimeoutError(), None, "timeout"),
        (httpx2.ReadTimeout("t", request=REQ), None, "timeout"),
        (ssl.SSLError("bad cert"), None, "tls_failed"),
        (httpx2.ConnectError("refused", request=REQ), None, "connect_failed"),
        (ConnectionRefusedError(), None, "connect_failed"),
        (httpx2.ReadError("reset", request=REQ), None, "transport_error"),
        (MCPError("bad"), None, "protocol_error"),
        (McpError("bad"), None, "protocol_error"),
        (RuntimeError("anything else"), None, "connection_failed"),
    ],
)
def test_each_failure_has_one_code_even_when_wrapped(exc, http_status, code):
    assert failure_code(exc, http_status) == code
    assert failure_code(wrapped(exc), http_status) == code
    assert failure_code(BaseExceptionGroup("tg", [ValueError("noise"), exc]), http_status) == code


def test_connect_failed_is_only_a_failed_tcp_connection():
    """Redirects, auth, TLS, HTTP and protocol failures are never reported as connect_failed."""
    for exc in (RedirectRefused(), status_error(302), status_error(403), ssl.SSLError("x"), MCPError("x"),
                RuntimeError("x")):
        assert failure_code(exc) != "connect_failed"


def test_policy_and_guard_codes_win_over_transport_noise():
    group = BaseExceptionGroup("tg", [httpx2.ConnectError("x", request=REQ), RedirectRefused()])
    assert failure_code(group) == "redirect_refused"
    assert failure_code(BaseExceptionGroup("tg", [TimeoutError(), OutboundBlocked("address_blocked")])) == (
        "address_blocked"
    )


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_refuse_redirect_hook(status):
    import asyncio

    with pytest.raises(RedirectRefused):
        asyncio.run(refuse_redirect(httpx2.Response(status)))
    asyncio.run(refuse_redirect(httpx2.Response(200)))


def test_redaction_module_scrubs_without_the_worker():
    from eap_secrets.redaction import REDACTED, redact, secret_variants

    variants = secret_variants(["Bearer tok-Sentinel-123"])
    out = redact({"description": "uses tok-Sentinel-123", "tok-Sentinel-123": 1}, variants)
    assert "tok-Sentinel-123" not in str(out) and REDACTED in out["description"]
