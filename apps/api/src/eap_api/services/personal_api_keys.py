"""Personal API keys: a signed-in person creates, lists and revokes keys of their own (docs/iam.md, "Personal API keys").

A personal key is an `api_keys` row with an owner. It acts as its owner (audit and runs name the person) with at most
the scopes the owner's roles allow, checked here at creation and again by the `apikey` identity plug-in on every use.
Only a person may hold one: a call made with an API key or the loopback dev bearer is refused with a clear message."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from eap_iam import ROLE_ADMIN, MembershipSource, Principal, scopes_for_roles
from eap_run_store import ApiKeyLimitExceeded, ApiKeyRecord, ApiKeyStore

from eap_api.services.errors import Conflict, Forbidden, InvalidInput, NotFound, Unavailable

API_KEY_NOT_FOUND_DETAIL = "API key not found"
DEFAULT_EXPIRY_DAYS = 90
MAX_EXPIRY_DAYS = 365
DEFAULT_SCOPES = ("mcp",)
# The identity providers that authenticate a person. The loopback dev bearer is a user kind too, but it is a
# developer's shell, not a person's identity: its keys would have no real owner.
PERSON_PROVIDERS = frozenset({"zitadel", "cfaccess"})
NOT_A_PERSON_DETAIL = (
    "Personal API keys belong to a signed-in person. Sign in to Studio (not an API key or the dev bearer) to manage yours."
)
NOT_A_MEMBER_DETAIL = "You are not a member of this tenant, so you cannot hold API keys here."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def owner_email(principal: Principal) -> str | None:
    """The person's e-mail: its own attribute when the subject is a stable id, else the subject when that is one."""
    extra = principal.extra.get("email")
    if isinstance(extra, str) and extra:
        return extra.lower()
    return principal.subject.lower() if "@" in principal.subject else None


class PersonalApiKeyService:
    def __init__(self, store: ApiKeyStore, membership: MembershipSource | None) -> None:
        self._store = store
        self._membership = membership if membership is not None and getattr(membership, "configured", True) else None

    @staticmethod
    def _require_person(principal: Principal) -> None:
        if principal.kind != "user" or principal.provider not in PERSON_PROVIDERS:
            raise Forbidden(NOT_A_PERSON_DETAIL, code="not_a_person")

    async def _roles(self, principal: Principal) -> frozenset[str]:
        """The caller's roles now. Without a membership source nothing records roles for a person, and the
        transitional policy engine already treats every authenticated user as admin: the same applies here."""
        if self._membership is None:
            return principal.roles or frozenset({ROLE_ADMIN})
        try:
            roles = await self._membership.roles_of(principal.tenant_id, principal.subject)
        except Exception:  # noqa: BLE001 - MembershipError or a transport failure: either way, cannot tell
            raise Unavailable("Membership could not be checked; try again", code="unavailable") from None
        if not roles:
            raise Forbidden(NOT_A_MEMBER_DETAIL, code="not_a_member")
        return frozenset(roles)

    async def create(
        self, principal: Principal, *, name: str, scopes: list[str], expires_at: datetime | None
    ) -> Any:
        """The new key (`.record`, and the plaintext `.key` shown once). 403: not a person, not a member, or a scope
        beyond the owner's roles; 422: a bad expiry or name; 409: past the per-person or tenant cap."""
        self._require_person(principal)
        allowed = scopes_for_roles(await self._roles(principal))
        beyond = [s for s in dict.fromkeys(scopes) if s not in allowed]
        if beyond:
            raise Forbidden(
                f"your role does not allow the {', '.join(beyond)} scope; allowed: {', '.join(sorted(allowed)) or 'none'}",
                code="scope_not_allowed",
            )
        now = _now()
        if expires_at is None:
            expires_at = now + timedelta(days=DEFAULT_EXPIRY_DAYS)
        elif expires_at.tzinfo is None:
            raise InvalidInput("expires_at must include a timezone")
        elif expires_at > now + timedelta(days=MAX_EXPIRY_DAYS):
            raise InvalidInput(f"expires_at is at most {MAX_EXPIRY_DAYS} days from now")
        try:
            return await asyncio.to_thread(
                lambda: self._store.create(
                    principal.tenant_id, name=name, created_by=principal.subject, scopes=scopes,
                    expires_at=expires_at, owner_subject=principal.subject, owner_email=owner_email(principal),
                )
            )
        except ApiKeyLimitExceeded as exc:
            raise Conflict(str(exc), code="key_limit") from None
        except ValueError as exc:
            raise InvalidInput(str(exc)) from None

    async def list(self, principal: Principal) -> list[ApiKeyRecord]:
        self._require_person(principal)
        return await asyncio.to_thread(self._store.list_owned, principal.tenant_id, principal.subject)

    async def revoke(self, principal: Principal, key_id: str) -> None:
        """Revoke one of the caller's own keys (idempotent). Another person's key, a tenant key and an unknown id
        are all the same 404, so a caller learns nothing about keys that are not theirs."""
        self._require_person(principal)
        revoked = await asyncio.to_thread(self._store.revoke_owned, principal.tenant_id, principal.subject, key_id)
        if revoked is None:
            raise NotFound(API_KEY_NOT_FOUND_DETAIL)
