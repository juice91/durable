"""Outbound URL policy: what an MCP server URL may point at, checked at registration and again at
connection time (DNS rebinding)."""

from __future__ import annotations

import ipaddress
import socket
import threading

import anyio
import pytest

from eap_secrets.outbound import (
    DEV_ALLOW_HOSTS_ENV,
    MAX_URL_LENGTH,
    OutboundBlocked,
    OutboundPolicy,
    blocked_address_class,
)


def fixed(*addresses: str):
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


def run(coro_fn, *args):
    return anyio.run(coro_fn, *args)


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("127.0.0.1", "loopback"),
        ("127.255.0.9", "loopback"),
        ("10.1.2.3", "private"),
        ("172.16.0.1", "private"),
        ("192.168.1.1", "private"),
        ("169.254.169.254", "link_local"),
        ("224.0.0.1", "multicast"),
        ("100.64.0.1", "cgnat"),
        ("100.127.255.254", "cgnat"),
        ("0.0.0.0", "unspecified"),
        ("240.0.0.1", "reserved"),
        ("255.255.255.255", "reserved"),
        ("198.18.0.1", "private"),  # benchmarking range
        ("::1", "loopback"),
        ("::", "unspecified"),
        ("fe80::1", "link_local"),
        ("fc00::1", "private"),
        ("fd12:3456::1", "private"),
        ("ff02::1", "multicast"),
        ("::ffff:127.0.0.1", "loopback"),
        ("::ffff:10.0.0.1", "private"),
        ("::ffff:169.254.169.254", "link_local"),
        ("::ffff:100.64.0.1", "cgnat"),
        ("2002:7f00:1::", "loopback"),  # 6to4 wrapping 127.0.0.1
        ("64:ff9b::a00:1", "private"),  # NAT64 wrapping 10.0.0.1
        ("8.8.8.8", None),
        ("93.184.215.14", None),
        ("2606:4700:4700::1111", None),
        ("::ffff:8.8.8.8", None),
    ],
)
def test_blocked_address_classes(address, expected):
    assert blocked_address_class(ipaddress.ip_address(address)) == expected


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://example.com/mcp", "url_scheme_not_allowed"),
        ("ftp://example.com/mcp", "url_scheme_not_allowed"),
        ("example.com/mcp", "url_scheme_not_allowed"),
        ("https://user:pass@example.com/mcp", "url_credentials"),
        ("https://user@example.com/mcp", "url_credentials"),
        ("https://:pass@example.com/mcp", "url_credentials"),
        ("https:///mcp", "url_host_missing"),
        ("https://exa mple.com/mcp", "url_invalid"),
        ("https://example.com/mc\np", "url_invalid"),
        ("https://example.com:99999/mcp", "url_invalid"),
        ("https://example.com/" + "a" * MAX_URL_LENGTH, "url_too_long"),
    ],
)
def test_url_shape_is_refused(url, code):
    policy = OutboundPolicy(resolver=fixed("93.184.215.14"))
    with pytest.raises(OutboundBlocked) as err:
        run(policy.check, url)
    assert err.value.code == code
    assert "pass" not in str(err.value)


def test_public_https_url_passes():
    policy = OutboundPolicy(resolver=fixed("93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"))
    target = run(policy.check, "https://Example.COM./mcp")
    assert (target.scheme, target.host, target.port) == ("https", "example.com", 443)


@pytest.mark.parametrize(
    ("url", "resolved", "blocked"),
    [
        ("https://internal.example/mcp", ["10.0.0.5"], "private"),
        ("https://meta.example/mcp", ["169.254.169.254"], "link_local"),
        ("https://v6.example/mcp", ["::1"], "loopback"),
        ("https://mapped.example/mcp", ["::ffff:127.0.0.1"], "loopback"),
        ("https://cgnat.example/mcp", ["100.64.10.10"], "cgnat"),
        ("https://mixed.example/mcp", ["93.184.215.14", "192.168.0.10"], "private"),
    ],
)
def test_resolved_private_addresses_are_refused(url, resolved, blocked):
    policy = OutboundPolicy(resolver=fixed(*resolved))
    with pytest.raises(OutboundBlocked) as err:
        run(policy.check, url)
    assert err.value.code == "address_blocked" and err.value.address_class == blocked


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://[::ffff:127.0.0.1]/mcp",
        "https://localhost/mcp",
        "https://127.1/mcp",  # shorthand IPv4, resolved by the system resolver
        "https://2130706433/mcp",  # 127.0.0.1 as a decimal integer
        "https://0x7f.0.0.1/mcp",
        "https://0.0.0.0/mcp",
    ],
)
def test_literal_and_numeric_loopback_forms_are_refused_by_the_real_resolver(url):
    with pytest.raises(OutboundBlocked) as err:
        run(OutboundPolicy().check, url)
    assert err.value.code in {"address_blocked", "host_unresolvable"}


def test_unresolvable_host_is_refused():
    async def fail(host, port):
        raise socket.gaierror(socket.EAI_NONAME, "nope")

    with pytest.raises(OutboundBlocked) as err:
        run(OutboundPolicy(resolver=fail).check, "https://nonexistent.invalid/mcp")
    assert err.value.code == "host_unresolvable"


def test_dev_allowlist_permits_http_and_loopback_only_for_listed_hosts():
    policy = OutboundPolicy.from_env({DEV_ALLOW_HOSTS_ENV: "127.0.0.1, Dev.Local"})
    assert run(policy.check, "http://127.0.0.1:8123/mcp").host == "127.0.0.1"
    assert run(policy.check, "https://127.0.0.1:8123/mcp").port == 8123
    with pytest.raises(OutboundBlocked) as err:
        run(policy.check, "http://example.com/mcp")
    assert err.value.code == "url_scheme_not_allowed"
    with pytest.raises(OutboundBlocked):
        run(policy.check, "http://localhost:8123/mcp")  # not listed, even though it is 127.0.0.1
    assert OutboundPolicy.from_env({}).dev_allow_hosts == frozenset()


class _CountingListener:
    """A TCP listener on 127.0.0.1 that counts accepted connections."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


def test_dns_rebinding_is_caught_at_connection_time():
    """The name resolves to a public address when checked, then to 127.0.0.1 when connecting."""
    listener = _CountingListener()
    answers = iter([["93.184.215.14"], ["127.0.0.1"], ["127.0.0.1"]])

    async def rebinding(host, port):
        return next(answers)

    policy = OutboundPolicy(resolver=rebinding)
    url = f"https://rebind.example:{listener.port}/mcp"

    async def scenario():
        await policy.check(url)  # registration-time check passes
        async with policy.http_client(timeout=5) as client:
            await client.post(url, json={})

    try:
        with pytest.raises(OutboundBlocked) as err:
            run(scenario)
        assert err.value.code == "address_blocked" and err.value.address_class == "loopback"
        assert listener.accepted == 0
    finally:
        listener.close()


def test_guarded_client_rechecks_scheme_and_credentials_per_request():
    policy = OutboundPolicy(resolver=fixed("93.184.215.14"))

    async def scenario(url):
        async with policy.http_client(timeout=5) as client:
            await client.get(url)

    with pytest.raises(OutboundBlocked) as err:
        run(scenario, "http://example.com/mcp")
    assert err.value.code == "url_scheme_not_allowed"
    with pytest.raises(OutboundBlocked) as err:
        run(scenario, "https://u:p@example.com/mcp")
    assert err.value.code == "url_credentials"


def test_guarded_client_ignores_proxy_environment_and_redirects(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    policy = OutboundPolicy(resolver=fixed("93.184.215.14"))

    async def scenario():
        async with policy.http_client() as client:
            return client.follow_redirects, client.trust_env

    assert run(scenario) == (False, False)


def test_guarded_client_connects_to_allowlisted_dev_host():
    """End to end through the pinned connection: a plain HTTP server on 127.0.0.1."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    policy = OutboundPolicy.from_env({DEV_ALLOW_HOSTS_ENV: "127.0.0.1"})

    async def scenario():
        async with policy.http_client(timeout=5) as client:
            response = await client.get(f"http://127.0.0.1:{server.server_port}/")
            return response.status_code, response.text

    try:
        assert run(scenario) == (200, "ok")
    finally:
        server.shutdown()
        server.server_close()
