"""The membership source of the built-in admin: one member, one role, read only. Without it the Architect and personal
API keys, which ask for a person's current roles, would find none under a policy engine that grants by role."""

from __future__ import annotations

import asyncio

from eap_iam import ROLE_ADMIN, Member, MembershipError

from eap_iam_localadmin.settings import PROVIDER, SUBJECT_PREFIX, AccountSettings
from eap_iam_localadmin.store import AdminStore

READ_ONLY_DETAIL = "This deployment has one built-in admin account. For several users or roles, switch to ZITADEL."


class LocalAdminMembership:
    name = PROVIDER
    mutable = False
    configured = True

    def __init__(self, settings: AccountSettings, store: AdminStore | None = None) -> None:
        self._settings = settings
        self._store = store  # None (a tool with no database): the configured username stands in

    async def _admin(self, tenant_id: str) -> str | None:
        if tenant_id != self._settings.tenant_id:
            return None
        if self._store is None:
            return self._settings.username
        account = await asyncio.to_thread(self._store.account)
        return None if account is None else account.username

    async def roles_of(self, tenant_id: str, subject: str) -> frozenset[str]:
        admin = await self._admin(tenant_id)
        if admin is not None and subject == SUBJECT_PREFIX + admin:
            return frozenset({ROLE_ADMIN})
        return frozenset()

    async def list_members(self, tenant_id: str) -> list[Member]:
        admin = await self._admin(tenant_id)
        if admin is None:
            return []
        return [Member(id=admin, display_name=admin, roles=(ROLE_ADMIN,))]

    async def invite(self, tenant_id: str, email: str, role: str) -> Member:
        raise MembershipError("unavailable", READ_ONLY_DETAIL)

    async def set_role(self, tenant_id: str, member_id: str, role: str) -> Member:
        raise MembershipError("unavailable", READ_ONLY_DETAIL)

    async def remove(self, tenant_id: str, member_id: str) -> None:
        raise MembershipError("unavailable", READ_ONLY_DETAIL)
