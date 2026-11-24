"""`GET /audit?actor=me`: any signed-in person reads their own rows (no admin needed); every other read still needs
`tenant.read_audit`. DB-free: the store and the policy are fakes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from eap_iam import Principal

from eap_api.services.audit import AuditService
from eap_api.services.errors import NotFound


class FakeStore:
    def __init__(self) -> None:
        self.listed: list[dict] = []
        self.recorded: list = []

    def list(self, tenant_id, **kw):
        self.listed.append({"tenant_id": tenant_id, **kw})
        row = {"id": "1", "at": datetime(2026, 1, 1, tzinfo=timezone.utc), "actor_subject": kw.get("actor")}
        return [row], None

    def record(self, entry) -> None:
        self.recorded.append(entry)


class DenyingAuthz:
    """The policy of a viewer: reading the audit log is refused."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def require(self, principal, action, resource) -> None:
        self.asked.append(action)
        raise NotFound("not found")


def person(subject: str = "zitadel:u1", tenant: str = "acme") -> Principal:
    return Principal(tenant_id=tenant, kind="user", subject=subject, provider="zitadel", roles=frozenset({"viewer"}))


def listing(service: AuditService, principal: Principal, actor: str | None) -> dict:
    return asyncio.run(
        service.list(principal, actor=actor, action=None, resource_kind=None, resource_id=None,
                     outcome=None, since=None, until=None, limit=50, cursor=None)
    )


def test_actor_me_is_the_callers_own_rows_without_the_admin_action_and_is_not_recorded():
    store, authz = FakeStore(), DenyingAuthz()
    page = listing(AuditService(store, authz), person(), "me")
    assert authz.asked == []  # no read_audit check
    assert store.listed == [{**store.listed[0], "tenant_id": "acme", "actor": "zitadel:u1"}]
    assert page["items"][0]["actor_subject"] == "zitadel:u1"
    assert store.recorded == []  # reading your own activity adds no audit.read row


def test_actor_me_cannot_be_pointed_at_someone_else_or_another_tenant():
    store = FakeStore()
    listing(AuditService(store, DenyingAuthz()), person("zitadel:u2", tenant="other"), "me")
    assert (store.listed[0]["tenant_id"], store.listed[0]["actor"]) == ("other", "zitadel:u2")


def test_any_other_actor_filter_still_needs_read_audit():
    store, authz = FakeStore(), DenyingAuthz()
    with pytest.raises(NotFound):
        listing(AuditService(store, authz), person(), "someone@example.com")
    with pytest.raises(NotFound):
        listing(AuditService(store, authz), person(), None)
    assert authz.asked == ["read_audit", "read_audit"] and store.listed == []
