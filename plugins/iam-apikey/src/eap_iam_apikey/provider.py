"""The `apikey` identity provider.

Order of checks for a key, exactly as the entry points always did: authenticate (unknown, malformed, wrong secret,
revoked, expired: 401 `Invalid API key`), the scope the route's action needs (403), the origin (403), then the throttled
`last_used_at` write. A request whose bearer is neither a key nor a run token is not this provider's: it answers None."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Mapping
from typing import Any

from eap_iam import ACTION_SCOPES, AuthError, Principal, RequestView, has_required_scope, scopes_for_roles
from eap_iam.messages import INVALID_API_KEY_DETAIL, ORIGIN_NOT_ALLOWED_DETAIL
from eap_run_store import TERMINAL_RUN_STATUSES, looks_like_api_key, normalize_origin
from eap_run_store.run_tokens import looks_like_run_token, signing_key, verify_run_token
from sqlalchemy import text

ARCHITECT_SLUG = "architect"
ARCHITECT_RUN_PREFIX = "architect-run:"
PROVIDER_KEY = "api-key"
PROVIDER_RUN_TOKEN = "architect-run-token"
# How long an owner's roles, as the membership source answered, are reused for their personal keys. A member who is
# removed or demoted keeps their keys working (or keeps the old scopes) for at most this long; revoking a key is
# immediate. Documented in docs/iam.md.
OWNER_ROLES_TTL_SECONDS = 60.0
OWNER_ROLES_CACHE_MAX = 1024
MEMBERSHIP_UNAVAILABLE_DETAIL = "Membership could not be checked; try again"

log = logging.getLogger(__name__)


def _origin_allowed(allowed: tuple[str, ...] | None, origin: str | None) -> bool:
    """A present `Origin` must be in a non-NULL list (both sides normalized); no `Origin` (a server-side caller) passes."""
    if allowed is None or origin is None:
        return True
    try:
        normalized = normalize_origin(origin)
    except ValueError:
        return False
    entries: set[str] = set()
    for entry in allowed:
        try:
            entries.add(normalize_origin(entry))
        except ValueError:
            continue  # an entry that is not an origin matches nothing
    return normalized in entries


class ApiKeyIdentityProvider:
    name = "apikey"
    kinds = frozenset({"api_key", "run"})

    def __init__(
        self, store: Any, engine: Any, env: Mapping[str, str], membership: Any = None, clock: Any = time.monotonic
    ) -> None:
        self._store = store
        self._engine = engine
        self._env = env
        self._membership = membership if membership is not None and getattr(membership, "configured", True) else None
        self._clock = clock
        self._owner_roles: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}

    async def _roles_of_owner(self, tenant_id: str, subject: str) -> frozenset[str]:
        """The owner's CURRENT roles from the membership source, cached for OWNER_ROLES_TTL_SECONDS. A source that
        fails answers 503 (fail closed): a personal key never works on a guess."""
        key = (tenant_id, subject)
        now = self._clock()
        hit = self._owner_roles.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        try:
            roles = frozenset(await self._membership.roles_of(tenant_id, subject))
        except Exception as exc:  # noqa: BLE001 - any failure of the source is "cannot tell"
            log.warning("membership lookup for a personal API key failed: %s", type(exc).__name__)
            raise AuthError(MEMBERSHIP_UNAVAILABLE_DETAIL, 503, www_authenticate=False) from None
        if len(self._owner_roles) >= OWNER_ROLES_CACHE_MAX:
            self._owner_roles = {k: v for k, v in self._owner_roles.items() if v[0] > now}
            if len(self._owner_roles) >= OWNER_ROLES_CACHE_MAX:
                self._owner_roles.clear()
        self._owner_roles[key] = (now + OWNER_ROLES_TTL_SECONDS, roles)
        return roles

    async def authenticate(self, request: RequestView) -> Principal | None:
        authorization = request.header("authorization")
        if authorization is None:
            return None
        scheme, _, credential = authorization.partition(" ")
        credential = credential.strip()
        if scheme.lower() != "bearer":
            return None
        if "run" in request.accepts and looks_like_run_token(credential):
            return await self._run_token(credential)
        if looks_like_api_key(credential):
            return await self._api_key(request, credential)
        return None

    async def _api_key(self, request: RequestView, credential: str) -> Principal:
        # last_used_at is written only once the scope and origin checks pass, so a copied key that a foreign page
        # keeps using (always 403) does not look recently used.
        record = await asyncio.to_thread(self._store.authenticate, credential, touch=False)
        if record is None:
            raise AuthError(INVALID_API_KEY_DETAIL)
        scopes = tuple(record.scopes)
        roles: frozenset[str] = frozenset()
        extra: dict[str, Any] = {}
        if record.owner_subject is not None:
            # A personal key acts for its owner, with at most what the owner's roles allow right now. The subject
            # stays the key id (conversations, OTLP and the key-active check are keyed by it); `owner` is who audit
            # rows and runs name as the actor.
            if self._membership is not None:
                roles = await self._roles_of_owner(record.tenant_id, record.owner_subject)
                if not roles:  # no longer a member
                    raise AuthError(INVALID_API_KEY_DETAIL)
                allowed = scopes_for_roles(roles)
                scopes = tuple(s for s in scopes if s in allowed)
            extra["owner"] = record.owner_subject
            extra["via_api_key"] = record.prefix
            if record.owner_email:
                extra["email"] = record.owner_email
        extra["scope_order"] = scopes
        principal = Principal(
            tenant_id=record.tenant_id,
            kind="api_key",
            subject=record.id,
            provider=PROVIDER_KEY,
            on_behalf_of=record.owner_subject or record.created_by,
            roles=roles,
            scopes=frozenset(scopes),
            agent_ids=record.agent_ids,
            workflow_ids=record.workflow_ids,
            allowed_origins=record.allowed_origins,
            extra=extra,
        )
        action = request.action
        if action is not None and not has_required_scope(principal, action):
            raise AuthError(f"API key lacks the {ACTION_SCOPES[action][0]} scope", 403)
        if not _origin_allowed(record.allowed_origins, request.header("origin")):
            raise AuthError(ORIGIN_NOT_ALLOWED_DETAIL, 403)
        await asyncio.to_thread(self._store.touch_last_used, record.id)
        return principal

    async def _run_token(self, credential: str) -> Principal:
        """A live run of the built-in Architect: a signed, unexpired token AND a run row that still exists in the
        tenant, belongs to the built-in `architect` agent and has not ended. Anything else is a plain 401."""
        run = await asyncio.to_thread(self._architect_run, credential)
        if run is None:
            raise AuthError(INVALID_API_KEY_DETAIL)
        tenant_id, run_id = run
        return Principal(
            tenant_id=tenant_id,
            kind="run",
            subject=f"{ARCHITECT_RUN_PREFIX}{run_id}",
            provider=PROVIDER_RUN_TOKEN,
            scopes=frozenset({"mcp", "manage"}),
        )

    def _architect_run(self, credential: str) -> tuple[str, str] | None:
        secret = os.environ.get("EAP_SECRETS_KEY")  # read per call, as the lookup always did
        token = verify_run_token(signing_key(secret), credential) if secret else None
        if token is None:
            return None
        try:
            uuid.UUID(token.run_id)
        except ValueError:
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT r.status, a.slug, a.builtin FROM runs r JOIN agents a ON a.id = r.agent_id "
                    "WHERE r.id = :r AND r.tenant_id = :t"
                ),
                {"r": token.run_id, "t": token.tenant_id},
            ).first()
        if row is None or row[0] in TERMINAL_RUN_STATUSES or not row[2] or row[1] != ARCHITECT_SLUG:
            return None
        return token.tenant_id, token.run_id
