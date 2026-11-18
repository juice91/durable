"""GitHub HTTP client: guarded requests, bounded retries, capped bodies, rate-limit headers.

Hosts: `api.github.com` and `raw.githubusercontent.com` only (a base URL other than api.github.com is
accepted for GitHub Enterprise but untested; its host joins the allowlist from the connection). Every request
goes through the `httpx2.AsyncClient` it is given (production: `OutboundPolicy.http_client()`, https and public
addresses only, no automatic redirects) and, on top of that, through the host allowlist on the first URL and
on every redirect hop (followed by hand, at most 5; the token never follows a redirect to another host).

Status mapping: 401 -> `connection_auth_failed` (no retry, never the body); 403/429 with
`x-ratelimit-remaining: 0` (or Retry-After) -> `rate_limited` carrying the reset time; other 403 ->
`connection_auth_failed`; 404 -> `not_found`; 5xx and network errors -> 3 tries, then `provider_error` /
`connection_unreachable`; other 4xx -> `provider_error`. The token lives in this object and nowhere else:
it appears in no result, error, cursor or log line. Every response's `x-ratelimit-*` headers update
`rate_remaining` / `rate_reset`.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2

from eap_kb_sources.errors import (
    CONNECTION_AUTH_FAILED,
    CONNECTION_RESPONSE_INVALID,
    CONNECTION_UNREACHABLE,
    NOT_FOUND,
    PROVIDER_ERROR,
    RATE_LIMITED,
    RESPONSE_TOO_LARGE,
    URL_NOT_ALLOWED,
    SourceError,
)

API_HOST = "api.github.com"
RAW_HOST = "raw.githubusercontent.com"
MAX_JSON_BYTES = 8 * 1024 * 1024  # a recursive tree listing is the largest answer
MAX_REDIRECTS = 5
MAX_TRIES = 3
BACKOFF_BASE = 0.5
TIMEOUT = httpx2.Timeout(30.0, connect=10.0)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})

Sleep = Callable[[float], Awaitable[None]]


def has_control_chars(text: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def guard_url(url: str, allowed_hosts: frozenset[str]) -> str:
    if has_control_chars(url) or any(c.isspace() for c in url):
        raise SourceError(URL_NOT_ALLOWED, "the URL is malformed")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018  (validates the port)
    except ValueError:
        raise SourceError(URL_NOT_ALLOWED, "the URL is malformed") from None
    if parts.scheme.lower() != "https" or parts.username is not None or parts.password is not None:
        raise SourceError(URL_NOT_ALLOWED, "only plain https URLs are allowed")
    if host not in allowed_hosts:
        raise SourceError(URL_NOT_ALLOWED, f"host {host[:80]} is not allowed")
    return url


@dataclass
class Fetched:
    status: int
    headers: dict[str, str]  # lower-cased names
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class GithubHttp:
    def __init__(
        self,
        http: httpx2.AsyncClient,
        allowed_hosts: frozenset[str],
        *,
        user_agent: str,
        token: str | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.0, BACKOFF_BASE),
    ) -> None:
        self._http = http
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self._user_agent = user_agent
        self._token = token or None
        self._sleep = sleep
        self._jitter = jitter
        self.calls = 0  # attempts that reached the provider
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None  # epoch seconds

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": self._user_agent, "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _note_rate(self, headers: Mapping[str, str]) -> None:
        remaining, reset = _int(headers.get("x-ratelimit-remaining")), _int(headers.get("x-ratelimit-reset"))
        if remaining is not None:
            self.rate_remaining = remaining
        if reset is not None:
            self.rate_reset = reset

    async def _once(self, url: str, headers: dict[str, str], max_bytes: int) -> Fetched:
        current = guard_url(url, self.allowed_hosts)
        origin = (urlsplit(current).hostname or "").lower()
        for hop in range(MAX_REDIRECTS + 1):
            send = dict(headers)
            if (urlsplit(current).hostname or "").lower() != origin:
                send.pop("Authorization", None)
            async with self._http.stream("GET", current, headers=send, timeout=TIMEOUT) as response:
                lowered = {k.lower(): v for k, v in response.headers.items()}
                self._note_rate(lowered)
                if response.status_code in _REDIRECTS and lowered.get("location"):
                    if hop == MAX_REDIRECTS:
                        raise SourceError(URL_NOT_ALLOWED, "too many redirects")
                    current = guard_url(urljoin(current, lowered["location"]), self.allowed_hosts)
                    continue
                declared = lowered.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise SourceError(RESPONSE_TOO_LARGE, "the response is larger than the allowed size")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise SourceError(RESPONSE_TOO_LARGE, "the response is larger than the allowed size")
                return Fetched(response.status_code, lowered, bytes(body))
        raise SourceError(URL_NOT_ALLOWED, "too many redirects")  # unreachable

    @staticmethod
    def _rate_limited(fetched: Fetched) -> bool:
        if fetched.status == 429:
            return True
        return fetched.status == 403 and (
            fetched.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in fetched.headers
        )

    async def get(self, url: str, *, accept: str = "application/vnd.github+json", max_bytes: int = MAX_JSON_BYTES) -> Fetched:
        """2xx comes back; everything else is classified (module docstring)."""
        from eap_secrets.outbound import OutboundBlocked

        headers = self._headers(accept)
        for attempt in range(MAX_TRIES):
            last = attempt == MAX_TRIES - 1
            self.calls += 1
            try:
                fetched = await self._once(url, headers, max_bytes)
            except SourceError:
                raise
            except OutboundBlocked:
                raise SourceError(URL_NOT_ALLOWED, "the outbound policy refused the request") from None
            except httpx2.HTTPError as exc:
                if last:
                    raise SourceError(CONNECTION_UNREACHABLE, f"network error ({type(exc).__name__})") from None
                await self._sleep(BACKOFF_BASE * (2**attempt) + self._jitter())
                continue
            code = fetched.status
            if code < 300:
                return fetched
            if self._rate_limited(fetched):
                raise SourceError(RATE_LIMITED, "the provider's rate limit is spent")
            if code == 401:
                raise SourceError(CONNECTION_AUTH_FAILED, "the provider refused the credentials (401)")
            if code == 403:
                raise SourceError(CONNECTION_AUTH_FAILED, "the provider refused access (403)")
            if code == 404:
                raise SourceError(NOT_FOUND, "the provider has no such resource")
            if code >= 500:
                if last:
                    raise SourceError(PROVIDER_ERROR, f"the provider answered {code}")
                await self._sleep(BACKOFF_BASE * (2**attempt) + self._jitter())
                continue
            raise SourceError(PROVIDER_ERROR, f"the provider answered {code}")
        raise SourceError(PROVIDER_ERROR, "the provider did not answer")  # unreachable

    async def get_json(self, url: str, *, max_bytes: int = MAX_JSON_BYTES) -> Any:
        fetched = await self.get(url, max_bytes=max_bytes)
        ctype = fetched.content_type
        if ctype != "application/json" and not ctype.endswith("+json"):
            raise SourceError(CONNECTION_RESPONSE_INVALID, "the response is not JSON")
        try:
            return json.loads(fetched.body)
        except ValueError:
            raise SourceError(CONNECTION_RESPONSE_INVALID, "the response is not valid JSON") from None
