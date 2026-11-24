"""The ZITADEL instance JWKS, cached in process (design 1.7; the same bounded-refresh rules as `eap-iam-cfaccess`).

- Fresh for `ttl` seconds. After that the next request refreshes it; if Cloudflare is unreachable the stale keys keep
  serving for `grace` more seconds, then the cache is empty and everything fails closed.
- An unknown `kid` refreshes at most once per `min_refresh` seconds, so a stream of forged `kid`s cannot make the API
  hammer Cloudflare. A refresh that fails counts as an attempt (the same limit).
- One refresh at a time (a lock); concurrent requests wait for it and read the result."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from jwt.algorithms import RSAAlgorithm

FETCH_TIMEOUT_SECONDS = 5
MAX_JWKS_BYTES = 256 * 1024


class JwksUnavailable(Exception):
    """The JWKS could not be fetched and no key of the cache may be used."""


def fetch_jwks(url: str, host: str | None = None) -> dict[str, Any]:
    """`host` overrides the Host header: ZITADEL routes by it, and in-cluster calls reach the Service by its own name."""
    if not url.startswith(("https://", "http://")):
        raise JwksUnavailable("the JWKS URL must be http(s)")
    headers = {"Accept": "application/json", "User-Agent": "eap-iam-zitadel"}
    if host:
        headers["Host"] = host
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310 (configured URL, http(s) only)
            body = response.read(MAX_JWKS_BYTES + 1)
    except Exception as exc:
        raise JwksUnavailable(type(exc).__name__) from None
    if len(body) > MAX_JWKS_BYTES:
        raise JwksUnavailable("JWKS too large")
    try:
        data = json.loads(body)
    except ValueError:
        raise JwksUnavailable("JWKS is not JSON") from None
    if not isinstance(data, dict):
        raise JwksUnavailable("JWKS is not an object")
    return data


class JwksCache:
    def __init__(
        self,
        fetcher: Callable[[], dict[str, Any]],
        *,
        ttl: float = 3600.0,
        min_refresh: float = 30.0,
        grace: float = 86400.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._ttl = ttl
        self._min_refresh = min_refresh
        self._grace = grace
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._loaded_at: float | None = None  # the last successful load
        self._attempted_at: float | None = None  # the last attempt, successful or not
        self._lock = asyncio.Lock()

    def _usable(self, now: float) -> bool:
        return self._loaded_at is not None and now - self._loaded_at < self._ttl + self._grace

    def _fresh(self, now: float) -> bool:
        return self._loaded_at is not None and now - self._loaded_at < self._ttl

    def _may_attempt(self, now: float) -> bool:
        return self._attempted_at is None or now - self._attempted_at >= self._min_refresh

    async def key_for(self, kid: str) -> Any | None:
        """The public key for `kid`, or None for an unknown one. Raises JwksUnavailable when nothing usable is cached
        and the fetch failed."""
        now = self._clock()
        if self._fresh(now) and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            now = self._clock()
            if self._fresh(now) and kid in self._keys:
                return self._keys[kid]
            stale_or_unknown = not self._fresh(now) or kid not in self._keys
            if stale_or_unknown and self._may_attempt(now):
                self._attempted_at = now
                try:
                    data = await asyncio.to_thread(self._fetcher)
                    self._keys = self._parse(data)
                    self._loaded_at = self._clock()
                except JwksUnavailable:
                    pass  # fall through: a stale cache inside the grace period still answers
                except Exception as exc:  # a malformed document is a failed fetch
                    raise JwksUnavailable(type(exc).__name__) from None
            now = self._clock()
            if self._usable(now):
                return self._keys.get(kid)
            raise JwksUnavailable("no usable JWKS")

    @staticmethod
    def _parse(data: dict[str, Any]) -> dict[str, Any]:
        keys: dict[str, Any] = {}
        for jwk in data.get("keys", []):
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or not isinstance(jwk.get("kid"), str):
                continue
            keys[jwk["kid"]] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        return keys
