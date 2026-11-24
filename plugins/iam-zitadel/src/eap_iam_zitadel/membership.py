"""The tenant's members, from ZITADEL user grants on the platform project (design 1.7, 7.3).

The change is made in ZITADEL, the single source of truth: no member table here. A tenant is one ZITADEL organisation
(`EAP_ZITADEL_ORG_TENANT_MAP`); a member is a user of that org holding at least one grant on the project, and the
grant's role keys are the roles. Writes need the machine user (`EAP_ZITADEL_MACHINE_KEY`); without it the source is
not `configured` and the capabilities answer does not offer it. The last admin can never be demoted or removed."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from eap_iam import MEMBER_ROLES, ROLE_ADMIN, Member, MembershipError

from eap_iam_zitadel.management import ManagementClient, ZitadelApiError
from eap_iam_zitadel.settings import ZitadelSettings

log = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
MAX_GRANTS = 1000
GRANT_ACTIVE = "USER_GRANT_STATE_ACTIVE"
NOT_CONFIGURED = "ZITADEL membership is not configured (the machine user's key is missing)"


def _wrap(exc: ZitadelApiError) -> MembershipError:
    if exc.status == 404:
        return MembershipError("not_found", "not found in ZITADEL")
    if exc.status == 409:
        return MembershipError("conflict", exc.message or "conflict in ZITADEL")
    log.warning("ZITADEL management call failed: %s", exc)
    return MembershipError("unavailable", "ZITADEL could not be reached or refused the platform's machine user")


def _member(grants: list[dict[str, Any]], *, invited: bool = False) -> Member:
    first = grants[0]
    roles: list[str] = []
    for grant in grants:
        roles.extend(str(r) for r in grant.get("roleKeys", []) if str(r) not in roles)
    ordered = tuple(r for r in MEMBER_ROLES if r in roles) + tuple(r for r in roles if r not in MEMBER_ROLES)
    name = first.get("displayName") or " ".join(p for p in (first.get("firstName"), first.get("lastName")) if p) or None
    active = any(g.get("state", GRANT_ACTIVE) == GRANT_ACTIVE for g in grants)
    return Member(
        id=str(first["userId"]),
        email=(str(first["email"]).lower() if first.get("email") else None),
        display_name=name,
        roles=ordered,
        status="invited" if invited else ("active" if active else "inactive"),
    )


class ZitadelMembership:
    name = "zitadel"

    def __init__(self, settings: ZitadelSettings | None, client: ManagementClient | None) -> None:
        self._settings = settings
        self._client = client
        self._write_lock = asyncio.Lock()  # one writer at a time: the last-admin check is read-then-write

    @property
    def configured(self) -> bool:
        return self._settings is not None and self._client is not None

    @property
    def mutable(self) -> bool:
        return self.configured

    def _need(self, tenant_id: str) -> tuple[ZitadelSettings, ManagementClient, str]:
        if self._settings is None or self._client is None:
            raise MembershipError("unavailable", NOT_CONFIGURED)
        org = self._settings.org_for_tenant(tenant_id)
        if org is None:
            raise MembershipError("not_found", "this tenant has no ZITADEL organisation")
        return self._settings, self._client, org

    async def _grants(self, tenant_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
        settings, client, org = self._need(tenant_id)
        queries: list[dict[str, Any]] = [{"projectIdQuery": {"projectId": settings.project_id}}]
        if user_id:
            queries.append({"userIdQuery": {"userId": user_id}})
        try:
            answer = await client.request(
                "POST", "/management/v1/users/grants/_search", org_id=org, json={"query": {"limit": MAX_GRANTS}, "queries": queries}
            )
        except ZitadelApiError as exc:
            raise _wrap(exc) from None
        return [g for g in answer.get("result", []) if isinstance(g, dict) and g.get("userId")]

    @staticmethod
    def _group(grants: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        by_user: dict[str, list[dict[str, Any]]] = {}
        for grant in grants:
            by_user.setdefault(str(grant["userId"]), []).append(grant)
        return by_user

    async def roles_of(self, tenant_id: str, subject: str) -> frozenset[str]:
        """The roles of a member by user id (or `zitadel:<id>`) or e-mail; empty for a stranger."""
        wanted = subject.lower()
        subject = subject.removeprefix("zitadel:")  # a principal subject is zitadel:<user id>
        for user_id, grants in self._group(await self._grants(tenant_id)).items():
            member = _member(grants)
            if user_id == subject or member.email == wanted:
                return frozenset(member.roles)
        return frozenset()

    async def list_members(self, tenant_id: str) -> list[Member]:
        members = [_member(g) for g in self._group(await self._grants(tenant_id)).values()]
        return sorted(members, key=lambda m: ((m.email or m.display_name or m.id).lower(), m.id))

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in MEMBER_ROLES:
            raise MembershipError("invalid", f"role must be one of {', '.join(MEMBER_ROLES)}")

    @staticmethod
    def _admins(by_user: dict[str, list[dict[str, Any]]]) -> set[str]:
        return {
            uid
            for uid, grants in by_user.items()
            if any(ROLE_ADMIN in g.get("roleKeys", []) and g.get("state", GRANT_ACTIVE) == GRANT_ACTIVE for g in grants)
        }

    async def invite(self, tenant_id: str, email: str, role: str) -> Member:
        email = email.strip().lower()
        if not EMAIL_PATTERN.match(email):
            raise MembershipError("invalid", "enter a valid e-mail address")
        self._check_role(role)
        settings, client, org = self._need(tenant_id)
        async with self._write_lock:
            try:
                found = await client.request(
                    "POST",
                    "/management/v1/users/_search",
                    org_id=org,
                    json={"queries": [{"emailQuery": {"emailAddress": email, "method": "TEXT_QUERY_METHOD_EQUALS_IGNORE_CASE"}}]},
                )
                users = [u for u in found.get("result", []) if isinstance(u, dict) and u.get("id")]
                created = False
                if users:
                    user_id = str(users[0]["id"])
                else:
                    local = email.split("@", 1)[0]
                    made = await client.request(
                        "POST",
                        "/v2/users/human",
                        json={
                            "organization": {"orgId": org},
                            "username": email,
                            "profile": {"givenName": local, "familyName": local},
                            "email": {"email": email, "sendCode": {}},
                        },
                    )
                    user_id = str(made.get("userId") or "")
                    if not user_id:
                        raise MembershipError("unavailable", "ZITADEL did not return the new user")
                    created = True
                if not created and await self._grants(tenant_id, user_id):
                    raise MembershipError("conflict", f"{email} is already a member")
                await client.request(
                    "POST",
                    f"/management/v1/users/{user_id}/grants",
                    org_id=org,
                    json={"projectId": settings.project_id, "roleKeys": [role]},
                )
            except ZitadelApiError as exc:
                raise _wrap(exc) from None
        return Member(id=user_id, email=email, roles=(role,), status="invited" if created else "active")

    async def _target(self, tenant_id: str, member_id: str) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        by_user = self._group(await self._grants(tenant_id))
        if member_id not in by_user:
            raise MembershipError("not_found", "no such member")
        return by_user[member_id], by_user

    async def set_role(self, tenant_id: str, member_id: str, role: str) -> Member:
        self._check_role(role)
        _, client, org = self._need(tenant_id)
        async with self._write_lock:
            grants, by_user = await self._target(tenant_id, member_id)
            if role != ROLE_ADMIN and self._admins(by_user) == {member_id}:
                raise MembershipError("last_admin", "the last admin cannot be demoted; promote someone else first")
            try:
                # One grant per user and project is what ZITADEL allows; the first carries the roles.
                grant = grants[0]
                await client.request(
                    "PUT", f"/management/v1/users/{member_id}/grants/{grant['id']}", org_id=org, json={"roleKeys": [role]}
                )
            except ZitadelApiError as exc:
                raise _wrap(exc) from None
        return _member([{**grants[0], "roleKeys": [role]}])

    async def remove(self, tenant_id: str, member_id: str) -> None:
        _, client, org = self._need(tenant_id)
        async with self._write_lock:
            grants, by_user = await self._target(tenant_id, member_id)
            if self._admins(by_user) == {member_id}:
                raise MembershipError("last_admin", "the last admin cannot be removed; promote someone else first")
            try:
                for grant in grants:
                    await client.request("DELETE", f"/management/v1/users/{member_id}/grants/{grant['id']}", org_id=org)
            except ZitadelApiError as exc:
                raise _wrap(exc) from None
