"""A small client for the ZITADEL Management API, authenticated as a machine user (JWT-profile grant).

The machine user's private key signs a short assertion that is exchanged for an access token at `/oauth/v2/token`; the
token is cached until a minute before it expires. ZITADEL routes by the `Host` header, so a call that reaches the
in-cluster Service by its own name sends the issuer's host. Nothing here logs a key, an assertion or a token."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx2
import jwt

from eap_iam_zitadel.settings import ServiceKey

TOKEN_SCOPE = "openid urn:zitadel:iam:org:project:id:zitadel:aud"
ASSERTION_LIFETIME_SECONDS = 300
TOKEN_MARGIN_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 15.0
TOKEN_CALL = "POST /oauth/v2/token (machine-user JWT-profile grant)"


class ZitadelApiError(Exception):
    """A call to ZITADEL failed. `status` is the HTTP status (0: no answer); `message` is ZITADEL's own text, which
    names no credential."""

    def __init__(self, status: int, message: str, call: str = "") -> None:
        where = f"{call} " if call else ""
        super().__init__(f"{where}-> HTTP {status}: {message}" if status else f"{where}failed: {message}")
        self.status = status
        self.message = message
        self.call = call


class ManagementClient:
    def __init__(
        self,
        *,
        api_url: str,
        issuer: str,
        host: str,
        key: ServiceKey,
        http: httpx2.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._issuer = issuer.rstrip("/")
        self._host = host
        self._key = key
        self._http = http or httpx2.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        self._clock = clock
        self.trace: list[tuple[str, str, int]] = []  # every call made: (method, path, HTTP status; 0 = no answer)
        self._token: str | None = None
        self._token_until = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        async with self._lock:
            now = self._clock()
            if self._token is not None and now < self._token_until - TOKEN_MARGIN_SECONDS:
                return self._token
            assertion = jwt.encode(
                {
                    "iss": self._key.user_id,
                    "sub": self._key.user_id,
                    "aud": self._issuer,
                    "iat": int(now),
                    "exp": int(now) + ASSERTION_LIFETIME_SECONDS,
                },
                self._key.private_key,
                algorithm="RS256",
                headers={"kid": self._key.key_id},
            )
            try:
                response = await self._http.post(
                    f"{self._api_url}/oauth/v2/token",
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "scope": TOKEN_SCOPE,
                        "assertion": assertion,
                    },
                    headers={"Host": self._host},
                )
            except httpx2.HTTPError as exc:
                raise ZitadelApiError(0, f"ZITADEL is unreachable ({type(exc).__name__})", TOKEN_CALL) from None
            self.trace.append(("POST", "/oauth/v2/token", response.status_code))
            if response.status_code != 200:
                raise ZitadelApiError(response.status_code, "the machine user's key was refused", TOKEN_CALL)
            body = response.json()
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise ZitadelApiError(response.status_code, "the token endpoint returned no access token", TOKEN_CALL)
            self._token = token
            self._token_until = now + float(body.get("expires_in", 300))
            return token

    async def request(
        self, method: str, path: str, *, org_id: str | None = None, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One API call; the JSON answer ({} for an empty body). `org_id` scopes the call to that organisation."""
        call = f"{method} {path}"
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Host": self._host, "Accept": "application/json"}
        if org_id:
            headers["x-zitadel-orgid"] = org_id
        try:
            response = await self._http.request(method, f"{self._api_url}{path}", headers=headers, json=json)
        except httpx2.HTTPError as exc:
            self.trace.append((method, path, 0))
            raise ZitadelApiError(0, f"ZITADEL is unreachable ({type(exc).__name__})", call) from None
        self.trace.append((method, path, response.status_code))
        if response.status_code >= 400:
            message = ""
            try:
                message = str(response.json().get("message", ""))[:200]
            except (ValueError, AttributeError):
                pass
            raise ZitadelApiError(response.status_code, message or "request failed", call)
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            raise ZitadelApiError(response.status_code, "ZITADEL answered with something that is not JSON", call) from None
        return data if isinstance(data, dict) else {}
