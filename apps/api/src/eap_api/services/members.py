"""The Members page behind the membership source (design 7.3): list, invite, change a role, remove.

The change is made in the source (ZITADEL), the single source of truth: this service holds no member data. Every call
is admin only (`tenant.manage_members`, decided by the policy engine; the transitional `openaccess` engine allows
every user). With no membership source configured the whole surface answers 404, the same as an unknown route, so a
client cannot tell a missing plug-in from a missing page. Refusals of the source map to the service errors."""

from __future__ import annotations

from typing import Any

from eap_iam import Member, MembershipError, MembershipSource, Principal, ResourceRef

from eap_api.services.authz import AuthzService
from eap_api.services.errors import Conflict, Forbidden, InvalidInput, NotFound, Unavailable

ACTION_MANAGE_MEMBERS = "manage_members"
_ERRORS: dict[str, type[Exception]] = {
    "not_found": NotFound,
    "conflict": Conflict,
    "invalid": InvalidInput,
    "unavailable": Unavailable,
}


def _view(member: Member) -> dict[str, Any]:
    return member.model_dump()


class MembersService:
    def __init__(self, source: MembershipSource | None, authz: AuthzService) -> None:
        self._source = source
        self._authz = authz

    async def _guard(self, principal: Principal) -> MembershipSource:
        if self._source is None or not getattr(self._source, "configured", True):
            raise NotFound("not found")
        await self._authz.require(principal, ACTION_MANAGE_MEMBERS, ResourceRef("tenant", None, principal.tenant_id))
        if principal.kind != "user":
            raise Forbidden("only a person may manage members", code="forbidden")
        return self._source

    @staticmethod
    def _refuse(exc: MembershipError) -> Exception:
        if exc.code == "last_admin":
            return Conflict(exc.detail, code="last_admin")
        return _ERRORS.get(exc.code, Unavailable)(exc.detail, code=exc.code)

    async def list(self, principal: Principal) -> dict[str, Any]:
        source = await self._guard(principal)
        try:
            members = await source.list_members(principal.tenant_id)
        except MembershipError as exc:
            raise self._refuse(exc) from None
        return {"items": [_view(m) for m in members], "mutable": bool(source.mutable)}

    async def invite(self, principal: Principal, email: str, role: str) -> dict[str, Any]:
        source = await self._guard(principal)
        self._require_mutable(source)
        try:
            return _view(await source.invite(principal.tenant_id, email, role))
        except MembershipError as exc:
            raise self._refuse(exc) from None

    async def set_role(self, principal: Principal, member_id: str, role: str) -> dict[str, Any]:
        source = await self._guard(principal)
        self._require_mutable(source)
        try:
            return _view(await source.set_role(principal.tenant_id, member_id, role))
        except MembershipError as exc:
            raise self._refuse(exc) from None

    async def remove(self, principal: Principal, member_id: str) -> None:
        source = await self._guard(principal)
        self._require_mutable(source)
        try:
            await source.remove(principal.tenant_id, member_id)
        except MembershipError as exc:
            raise self._refuse(exc) from None

    @staticmethod
    def _require_mutable(source: MembershipSource) -> None:
        if not source.mutable:
            raise Conflict("members are managed in the identity provider", code="read_only")
