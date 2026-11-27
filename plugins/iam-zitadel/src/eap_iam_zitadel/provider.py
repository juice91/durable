"""ZITADEL access tokens as the identity of a person or a service user (design 1.7).

A bearer that is shaped like a JWT is this provider's credential: from then on it is verified or refused, and a refusal
never falls through to a weaker provider. A bearer that is not JWT-shaped (`pos_...` keys, `dev:...`) is not ours: None.

Verified on every request, in this order: header, `alg` (RS256 only), `kid`, signature through the cached JWKS (bounded
refresh on an unknown `kid`), `iss` exactly, `aud` containing the platform project id, `exp` / `nbf` with 30 s leeway.
Then the organisation maps to a tenant (an unmapped org is 403) and the roles come from the project-roles claim, only
those the principal's own organisation granted."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import jwt
from eap_iam import MEMBER_ROLES, AuthError, Principal, RequestView

from eap_iam_zitadel.jwks import JwksCache, JwksUnavailable
from eap_iam_zitadel.settings import ZitadelSettings

log = logging.getLogger(__name__)


def _kid_for_log(kid: str) -> str:
    """The `kid` header comes from an unverified token: truncate and escape it before it reaches a log line."""
    return repr(kid[:64])

PROVIDER = "zitadel"
SUBJECT_PREFIX = "zitadel:"  # the stable subject of every ZITADEL principal: zitadel:<sub>, never the changeable e-mail
MAX_TOKEN_BYTES = 16 * 1024
LEEWAY_SECONDS = 30
INVALID_DETAIL = "Invalid ZITADEL access token"
UNMAPPED_ORG_DETAIL = "Your organisation is not a tenant of this platform"
ORG_CLAIM = "urn:zitadel:iam:user:resourceowner:id"
ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"
PROJECT_ROLES_CLAIM = "urn:zitadel:iam:org:project:{project_id}:roles"
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
MAX_EMAIL = 254
USERINFO_CACHE_SECONDS = 300.0
USERINFO_CACHE_MAX = 1024
FOREIGN_GRANT_WARNED_MAX = 1024  # subjects remembered so each is warned about once; cleared when full
USERINFO_TIMEOUT_SECONDS = 5.0

Userinfo = Callable[[str], Awaitable[dict[str, Any]]]


def _segments(token: str) -> list[str] | None:
    parts = token.split(".")
    return parts if len(parts) == 3 and parts[0] and parts[1] else None


def _b64json(segment: str) -> Any:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def looks_like_jwt(token: str) -> bool:
    """Three dot-separated segments whose first is a base64url JSON object with an `alg`: this provider's business."""
    parts = _segments(token)
    if parts is None:
        return False
    try:
        header = _b64json(parts[0])
    except (ValueError, TypeError):
        return False
    return isinstance(header, dict) and "alg" in header


def _role_keys(claims: dict[str, Any], project_id: str, org_id: str) -> tuple[frozenset[str], frozenset[str]]:
    """(the roles that count, the roles ignored because only another organisation granted them).

    The claim is `{role: {granting organisation id: its primary domain}}` (ZITADEL v4.20.5,
    `internal/api/oidc/client.go`: `projectRoles.Add(roleKey, grant.ResourceOwner, grant.OrgPrimaryDomain)`). A role
    counts only when the principal's own organisation is one of the granting ones: a grant made in another
    organisation's context would otherwise carry its role into this principal's tenant. Any other shape grants
    nothing."""
    own: set[str] = set()
    seen: set[str] = set()
    for name in (ROLES_CLAIM, PROJECT_ROLES_CLAIM.format(project_id=project_id)):
        value = claims.get(name)
        if not isinstance(value, dict):
            continue
        for key, orgs in value.items():
            role = str(key)
            if role not in MEMBER_ROLES:  # a role this platform does not define grants nothing
                continue
            seen.add(role)
            if isinstance(orgs, dict) and org_id in orgs:
                own.add(role)
    return frozenset(own), frozenset(seen - own)


def _clean_email(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= MAX_EMAIL and EMAIL_PATTERN.match(value.strip()):
        return value.strip().lower()
    return None


class ZitadelProvider:
    name = "zitadel"
    kinds = frozenset({"user", "service"})

    def __init__(
        self,
        settings: ZitadelSettings | None,
        *,
        jwks: JwksCache | None = None,
        userinfo: Userinfo | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._jwks = jwks
        self._userinfo = userinfo or (self._fetch_userinfo if settings else None)
        self._clock = clock
        self._userinfo_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._http: httpx2.AsyncClient | None = None
        self._foreign_warned: set[str] = set()

    @property
    def dormant(self) -> bool:
        return self._settings is None

    async def authenticate(self, request: RequestView) -> Principal | None:
        raw = request.header("authorization")
        if raw is None:
            return None
        scheme, _, token = raw.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not looks_like_jwt(token) or self._settings is None or self._jwks is None:
            return None
        settings = self._settings
        if len(token) > MAX_TOKEN_BYTES:
            raise AuthError(INVALID_DETAIL)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError(INVALID_DETAIL) from None
        kid = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:  # `none`, HS256, ES256 ... never pass
            raise AuthError(INVALID_DETAIL)
        try:
            key = await self._jwks.key_for(kid)
        except JwksUnavailable:
            log.warning("ZITADEL JWKS unavailable and no cached key for kid %s", _kid_for_log(kid))
            raise AuthError(INVALID_DETAIL) from None
        if key is None:
            log.info("ZITADEL token refused: unknown kid %s", _kid_for_log(kid))
            raise AuthError(INVALID_DETAIL)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=settings.project_id,
                issuer=settings.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud", "sub"], "verify_iat": False},
            )
        except jwt.PyJWTError as exc:
            log.info("ZITADEL token refused (kid %s): %s", _kid_for_log(kid), type(exc).__name__)
            raise AuthError(INVALID_DETAIL) from None
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise AuthError(INVALID_DETAIL)

        org = claims.get(ORG_CLAIM)
        tenant = settings.org_tenants.get(org) if isinstance(org, str) else None
        if tenant is None:
            log.info("ZITADEL token refused: its organisation is not mapped to a tenant")
            raise AuthError(UNMAPPED_ORG_DETAIL, status=403, www_authenticate=False)

        email = _clean_email(claims.get("email"))
        display = claims.get("name") if isinstance(claims.get("name"), str) else None
        if email is None and self._userinfo is not None:
            info = await self._lookup_userinfo(token)
            email = _clean_email(info.get("email"))
            display = display or (info.get("name") if isinstance(info.get("name"), str) else None)
        roles, foreign = _role_keys(claims, settings.project_id, org)
        if foreign:
            self._warn_foreign_grant(f"{SUBJECT_PREFIX}{sub}", foreign)
        extra = {"sub": sub, "org_id": org, "email": email, "display_name": display, "kid": kid}
        if email is None:
            # A machine user (client-credentials or JWT-profile grant) has no e-mail: a service, not a person.
            log.debug("ZITADEL service token accepted")
            return Principal(tenant_id=tenant, kind="service", subject=f"{SUBJECT_PREFIX}{sub}", provider=PROVIDER, roles=roles, extra=extra)
        log.debug("ZITADEL token accepted (kid %s)", _kid_for_log(kid))
        return Principal(tenant_id=tenant, kind="user", subject=f"{SUBJECT_PREFIX}{sub}", provider=PROVIDER, roles=roles, extra=extra)

    def _warn_foreign_grant(self, subject: str, roles: frozenset[str]) -> None:
        if subject in self._foreign_warned:
            return
        if len(self._foreign_warned) >= FOREIGN_GRANT_WARNED_MAX:
            self._foreign_warned.clear()
        self._foreign_warned.add(subject)
        log.warning(
            "ZITADEL roles ignored for %r: %s granted only by an organisation other than the user's own",
            subject[:96], ", ".join(sorted(roles)),
        )

    async def _lookup_userinfo(self, token: str) -> dict[str, Any]:
        assert self._userinfo is not None
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        hit = self._userinfo_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        try:
            info = await self._userinfo(token)
        except Exception as exc:  # a person must not be misread as a service because userinfo was down
            log.warning("ZITADEL userinfo failed: %s", type(exc).__name__)
            raise AuthError(INVALID_DETAIL) from None
        if len(self._userinfo_cache) >= USERINFO_CACHE_MAX:
            self._userinfo_cache = {k: v for k, v in self._userinfo_cache.items() if v[0] > now}
            if len(self._userinfo_cache) >= USERINFO_CACHE_MAX:
                self._userinfo_cache.clear()
        self._userinfo_cache[key] = (now + USERINFO_CACHE_SECONDS, info)
        return info

    async def _fetch_userinfo(self, token: str) -> dict[str, Any]:
        assert self._settings is not None
        if self._http is None:
            self._http = httpx2.AsyncClient(timeout=USERINFO_TIMEOUT_SECONDS)
        response = await self._http.get(
            f"{self._settings.api_url}/oidc/v1/userinfo",
            headers={"Authorization": f"Bearer {token}", "Host": self._settings.issuer_host, "Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("userinfo is not an object")
        return data
