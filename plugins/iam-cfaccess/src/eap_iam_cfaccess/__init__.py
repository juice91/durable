"""The Cloudflare Access JWT as the identity of a person (design 1.3). RS256 only, verified on every request."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import jwt
from eap_iam import AuthError, IamConfigError, PluginContext, Principal, RequestView

from eap_iam_cfaccess.jwks import JwksCache, JwksUnavailable, fetch_jwks

log = logging.getLogger(__name__)


def _kid_for_log(kid: str) -> str:
    """The `kid` header comes from an unverified token: truncate and escape it before it reaches a log line."""
    return repr(kid[:64])

PROVIDER = "cf-access"
JWT_HEADER = "cf-access-jwt-assertion"
MAX_TOKEN_BYTES = 8 * 1024
LEEWAY_SECONDS = 30
MAX_AGE_SECONDS = 24 * 3600 * 7  # `iat` older than this is absurd for an Access session token
INVALID_DETAIL = "Invalid Cloudflare Access token"
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
MAX_EMAIL = 254


class CfAccessProvider:
    name = "cfaccess"
    kinds = frozenset({"user"})

    def __init__(
        self,
        *,
        team_domain: str,
        audiences: tuple[str, ...],
        tenant_id: str,
        jwks: JwksCache,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.issuer = f"https://{team_domain}"
        self.audiences = audiences
        self.tenant_id = tenant_id
        self._jwks = jwks
        self._clock = clock

    async def authenticate(self, request: RequestView) -> Principal | None:
        token = request.header(JWT_HEADER)
        if token is None:
            return None  # not this provider's credential: the next one decides (the plain e-mail header is never read)
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise AuthError(INVALID_DETAIL, www_authenticate=False)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError(INVALID_DETAIL, www_authenticate=False) from None
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:  # `none`, HS256, ES256 ... never pass
            raise AuthError(INVALID_DETAIL, www_authenticate=False)
        try:
            key = await self._jwks.key_for(kid)
        except JwksUnavailable:
            log.warning("Cloudflare Access JWKS unavailable and no cached key for kid %s", _kid_for_log(kid))
            raise AuthError(INVALID_DETAIL, www_authenticate=False) from None
        if key is None:
            log.info("Cloudflare Access token refused: unknown kid %s", _kid_for_log(kid))
            raise AuthError(INVALID_DETAIL, www_authenticate=False)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=list(self.audiences),
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"], "verify_iat": False},
            )
        except jwt.PyJWTError as exc:
            log.info("Cloudflare Access token refused (kid %s): %s", _kid_for_log(kid), type(exc).__name__)
            raise AuthError(INVALID_DETAIL, www_authenticate=False) from None
        iat = claims.get("iat")
        if isinstance(iat, (int, float)) and self._clock() - iat > MAX_AGE_SECONDS:
            raise AuthError(INVALID_DETAIL, www_authenticate=False)
        email = claims.get("email")
        if not isinstance(email, str) or len(email) > MAX_EMAIL or not EMAIL_PATTERN.match(email.strip()):
            # A service-token JWT (no `email`, a `common_name`) is not a person.
            log.info("Cloudflare Access token refused (kid %s): no usable email claim", _kid_for_log(kid))
            raise AuthError(INVALID_DETAIL, www_authenticate=False)
        subject = email.strip().lower()
        log.debug("Cloudflare Access token accepted (kid %s)", _kid_for_log(kid))
        return Principal(tenant_id=self.tenant_id, kind="user", subject=subject, provider=PROVIDER)


def _settings(env: Mapping[str, str]) -> tuple[str, tuple[str, ...], str]:
    team = env.get("EAP_CF_ACCESS_TEAM_DOMAIN", "").strip()
    auds = tuple(a.strip() for a in env.get("EAP_CF_ACCESS_AUD", "").split(",") if a.strip())
    tenant = env.get("EAP_CF_ACCESS_TENANT", "").strip()
    missing = [
        name
        for name, value in (
            ("EAP_CF_ACCESS_TEAM_DOMAIN", team),
            ("EAP_CF_ACCESS_AUD", auds),
            ("EAP_CF_ACCESS_TENANT", tenant),
        )
        if not value
    ]
    if missing:
        raise IamConfigError(f"the cfaccess identity plug-in needs {', '.join(missing)}")
    if "/" in team or ":" in team or " " in team:
        raise IamConfigError("EAP_CF_ACCESS_TEAM_DOMAIN must be a bare host name such as <team>.cloudflareaccess.com")
    return team, auds, tenant


def create(ctx: PluginContext, *, fetcher: Callable[[], Any] | None = None) -> CfAccessProvider:
    env = ctx.env
    team, auds, tenant = _settings(env)
    cache = JwksCache(
        fetcher or (lambda: fetch_jwks(f"https://{team}/cdn-cgi/access/certs")),
        ttl=float(env.get("EAP_CF_ACCESS_JWKS_TTL_SECONDS", "3600")),
        min_refresh=float(env.get("EAP_CF_ACCESS_JWKS_MIN_REFRESH_SECONDS", "30")),
        grace=float(env.get("EAP_CF_ACCESS_JWKS_GRACE_SECONDS", "86400")),
    )
    return CfAccessProvider(team_domain=team, audiences=auds, tenant_id=tenant, jwks=cache)


__all__ = ["CfAccessProvider", "JwksCache", "create"]
