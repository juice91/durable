"""Outbound connection policy for tenant-supplied URLs (MCP servers): the SSRF guard.

One module, used by the API (registration and connection test) and the worker (tool calls):

- `OutboundPolicy.check(url)` at registration/update: https only (http only for hosts listed in
  EAP_MCP_DEV_ALLOW_HOSTS), no credentials in the URL, at most MAX_URL_LENGTH characters, a host is
  required, and every address the host resolves to must be public. Refused: private (RFC 1918, ULA),
  loopback, link-local (including cloud metadata 169.254.169.254), multicast, CGNAT (100.64.0.0/10),
  unspecified, reserved and any other non-global range, for IPv4 and IPv6, including IPv4 wrapped in
  IPv6 (IPv4-mapped ::ffff:a.b.c.d, 6to4 2002::/16, NAT64 64:ff9b::/96).
- `OutboundPolicy.http_client()` for the connection itself: an `httpx2.AsyncClient` whose transport
  re-checks the URL on every request and whose network backend resolves the host again at connect
  time, refuses blocked addresses and connects to the checked address itself (TLS still verifies the
  hostname). A name that resolved to a public address at registration and to 127.0.0.1 later (DNS
  rebinding) is refused before any connection is made. The client ignores proxy environment
  variables (a proxy would make the address check meaningless) and does not follow redirects.

Hosts in EAP_MCP_DEV_ALLOW_HOSTS (comma-separated, exact host names or IP literals, for local
development and tests only) may use http and may resolve to any address.

Errors are `OutboundBlocked` with a stable `code` (and `address_class` for blocked addresses); the
message never contains the URL, so it is safe to log and to return.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import typing
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import anyio
import httpcore2
import httpx2

DEV_ALLOW_HOSTS_ENV = "EAP_MCP_DEV_ALLOW_HOSTS"
MAX_URL_LENGTH = 2048

Resolver = Callable[[str, int], Awaitable[list[str]]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_ALLOWED_SCHEMES = {"https"}
_DEFAULT_PORTS = {"https": 443, "http": 80}


class OutboundBlocked(Exception):
    """A URL or address the outbound policy refuses. `code` is a stable, loggable identifier."""

    def __init__(self, code: str, address_class: str | None = None) -> None:
        self.code = code
        self.address_class = address_class
        super().__init__(f"outbound connection refused: {code}" + (f" ({address_class})" if address_class else ""))


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int


def _ipv4_inside(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def blocked_address_class(ip: IPAddress) -> str | None:
    """Why `ip` may not be connected to (a class name), or None when it is a public address."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _ipv4_inside(ip)
        if inner is not None:
            return blocked_address_class(inner)
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return "cgnat"
    if ip.is_reserved:
        return "reserved"
    if ip.is_private:
        return "private"
    if not ip.is_global:
        return "non_global"
    return None


async def system_resolver(host: str, port: int) -> list[str]:
    """Every address the system resolver returns for `host` (A and AAAA)."""
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _normalise_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        raise OutboundBlocked("url_invalid") from None


class OutboundPolicy:
    def __init__(
        self,
        *,
        dev_allow_hosts: Iterable[str] = (),
        resolver: Resolver | None = None,
        max_url_length: int = MAX_URL_LENGTH,
    ) -> None:
        self.dev_allow_hosts = frozenset(h.strip().lower().rstrip(".") for h in dev_allow_hosts if h.strip())
        self._resolver = resolver or system_resolver
        self.max_url_length = max_url_length

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kwargs: typing.Any) -> OutboundPolicy:
        env = os.environ if env is None else env
        return cls(dev_allow_hosts=env.get(DEV_ALLOW_HOSTS_ENV, "").split(","), **kwargs)

    def _dev_allowed(self, host: str) -> bool:
        return host in self.dev_allow_hosts

    def check_url(self, url: str) -> Target:
        """The URL-shape rules (no DNS): scheme, credentials, length, host, port."""
        if len(url) > self.max_url_length:
            raise OutboundBlocked("url_too_long")
        if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
            raise OutboundBlocked("url_invalid")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise OutboundBlocked("url_invalid") from None
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            raise OutboundBlocked("url_scheme_not_allowed")
        if "@" in parts.netloc or parts.username is not None or parts.password is not None:
            raise OutboundBlocked("url_credentials")
        if not parts.hostname:
            raise OutboundBlocked("url_host_missing")
        host = _normalise_host(parts.hostname)
        if not host:
            raise OutboundBlocked("url_host_missing")
        if scheme not in _ALLOWED_SCHEMES and not self._dev_allowed(host):
            raise OutboundBlocked("url_scheme_not_allowed")
        return Target(scheme=scheme, host=host, port=port or _DEFAULT_PORTS[scheme])

    async def resolve_checked(self, host: str, port: int) -> list[str]:
        """The addresses to connect to for host:port, after refusing any blocked one.

        Every resolved address must be allowed (not just the first), so a name with one public and
        one private record is refused."""
        host = _normalise_host(host.strip("[]"))
        try:
            literal: IPAddress | None = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            addresses = [str(literal)]
        else:
            try:
                addresses = await self._resolver(host, port)
            except (OSError, UnicodeError):
                raise OutboundBlocked("host_unresolvable") from None
            if not addresses:
                raise OutboundBlocked("host_unresolvable")
        if self._dev_allowed(host):
            return addresses
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address.split("%", 1)[0])
            except ValueError:
                raise OutboundBlocked("host_unresolvable") from None
            blocked = blocked_address_class(ip)
            if blocked is not None:
                raise OutboundBlocked("address_blocked", blocked)
        return addresses

    async def check(self, url: str) -> Target:
        """Registration-time check: URL shape plus DNS resolution to public addresses only."""
        target = self.check_url(url)
        await self.resolve_checked(target.host, target.port)
        return target

    def http_client(self, **kwargs: typing.Any) -> httpx2.AsyncClient:
        """An AsyncClient that enforces this policy on every request and every new connection.

        Takes AsyncClient keyword arguments (headers, timeout, auth, ...); `transport`, `proxy`,
        `trust_env`, `mounts` and `follow_redirects` are fixed by the policy."""
        for fixed in ("transport", "mounts", "proxy"):
            if kwargs.pop(fixed, None) is not None:
                raise ValueError(f"{fixed} is set by the outbound policy")
        kwargs.pop("follow_redirects", None)
        kwargs.pop("trust_env", None)
        return httpx2.AsyncClient(
            transport=GuardedTransport(self), trust_env=False, follow_redirects=False, **kwargs
        )

    def httpx_client_factory(self, **kwargs: typing.Any) -> httpx2.AsyncClient:
        """The `httpx_client_factory` shape FastMCP/MCP transports accept."""
        kwargs.setdefault("timeout", httpx2.Timeout(30.0, read=300.0))
        return self.http_client(**kwargs)


class _GuardedBackend(httpcore2.AsyncNetworkBackend):
    """Resolves and checks at connect time, then connects to the checked address (no second lookup)."""

    def __init__(self, policy: OutboundPolicy) -> None:
        self._policy = policy
        self._inner = httpcore2.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        addresses = await self._policy.resolve_checked(host, port)
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except httpcore2.ConnectError as exc:  # try the next checked address
                last = exc
        assert last is not None
        raise last

    async def connect_unix_socket(self, *args: typing.Any, **kwargs: typing.Any) -> httpcore2.AsyncNetworkStream:
        raise OutboundBlocked("url_scheme_not_allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedTransport(httpx2.AsyncHTTPTransport):
    """httpx2's default transport with the policy's URL check per request and a guarded backend."""

    def __init__(self, policy: OutboundPolicy) -> None:
        super().__init__(trust_env=False)
        self._policy = policy
        # Same pool httpx2 builds, with our network backend: every new TCP connection goes through
        # _GuardedBackend. (Covered by the DNS-rebinding test, which fails if this stops applying.)
        self._pool = httpcore2.AsyncConnectionPool(
            ssl_context=httpx2.create_ssl_context(trust_env=False),
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=_GuardedBackend(policy),
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self._policy.check_url(str(request.url))
        return await super().handle_async_request(request)


__all__ = [
    "DEV_ALLOW_HOSTS_ENV",
    "MAX_URL_LENGTH",
    "GuardedTransport",
    "OutboundBlocked",
    "OutboundPolicy",
    "Resolver",
    "Target",
    "blocked_address_class",
    "system_resolver",
]
