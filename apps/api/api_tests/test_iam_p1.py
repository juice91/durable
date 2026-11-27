"""IAM P1 through the API: a ZITADEL token end to end (chain order zitadel,apikey,devloopback), the Members routes over a
membership source, and the paths that must not change (API keys, the loopback bearer). The ZITADEL token tests use a
JWKS fake with generated keys, not a ZITADEL container."""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from eap_iam import GROUP_MEMBERSHIP, GROUP_POLICY, Member, MembershipError, register_local
from eap_iam.registry import clear_local
from eap_iam.testing import FakePolicyEngine
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text

import eap_iam_zitadel
from eap_api.app import create_app

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg

pytestmark = requires_pg

ISSUER, PROJECT, ORG = "https://auth.example.test", "proj-p1", "org-p1"
CAPABILITIES = "/api/v1/iam/capabilities"
MEMBERS = "/api/v1/iam/members"


class MemoryMembership:
    """A membership source in memory with the plug-in's rules (last admin, read-only mode)."""

    name = "memory"

    def __init__(self, mutable: bool = True) -> None:
        self.mutable = mutable
        self.members = {
            "u1": Member(id="u1", email="ada@example.test", display_name="Ada", roles=("admin",)),
            "u2": Member(id="u2", email="bob@example.test", roles=("viewer",)),
        }

    async def roles_of(self, tenant_id, subject):
        return frozenset()

    async def list_members(self, tenant_id):
        return list(self.members.values())

    async def invite(self, tenant_id, email, role):
        if any(m.email == email for m in self.members.values()):
            raise MembershipError("conflict", "already a member")
        member = Member(id=f"u{len(self.members) + 1}", email=email, roles=(role,), status="invited")
        self.members[member.id] = member
        return member

    async def set_role(self, tenant_id, member_id, role):
        if member_id not in self.members:
            raise MembershipError("not_found", "no such member")
        admins = [m for m in self.members.values() if "admin" in m.roles]
        if role != "admin" and [m.id for m in admins] == [member_id]:
            raise MembershipError("last_admin", "the last admin cannot be demoted")
        self.members[member_id] = self.members[member_id].model_copy(update={"roles": (role,)})
        return self.members[member_id]

    async def remove(self, tenant_id, member_id):
        if member_id not in self.members:
            raise MembershipError("not_found", "no such member")
        del self.members[member_id]


def make_app(monkeypatch, tenant, **env):
    from eap_secrets import KEY_ENV, generate_key

    monkeypatch.setenv(KEY_ENV, generate_key())
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return create_app(database_url=PG_URL)


@pytest.fixture(autouse=True)
def _clean():
    clear_local()
    with hard_timeout(90):
        yield
    clear_local()


@pytest.fixture
def members_app(monkeypatch):
    tenant = f"t{uuid.uuid4().hex[:10]}"
    source = MemoryMembership()
    register_local(GROUP_MEMBERSHIP, "memory", lambda ctx: source)
    app = make_app(monkeypatch, tenant, EAP_IAM_MEMBERSHIP="memory")
    with TestClient(app, client=("127.0.0.1", 40000)) as client:
        yield SimpleNamespace(client=client, tenant=tenant, source=source, headers=get_auth_header(tenant, "alice"), app=app)
    app.state.database_engine.dispose()


def test_capabilities_offer_the_membership_source_and_the_manage_action(members_app):
    body = members_app.client.get(CAPABILITIES, headers=members_app.headers).json()
    assert body["membership"] == {"name": "memory", "mutable": True}
    assert "tenant.manage_members" in body["actions"]


def test_list_invite_change_role_and_remove(members_app):
    c, h = members_app.client, members_app.headers
    listed = c.get(MEMBERS, headers=h)
    assert listed.status_code == 200 and listed.json()["mutable"] is True
    assert [m["email"] for m in listed.json()["items"]] == ["ada@example.test", "bob@example.test"]
    invited = c.post(MEMBERS, json={"email": "cy@example.test", "role": "editor"}, headers=h)
    assert invited.status_code == 201 and invited.json()["status"] == "invited"
    assert c.post(MEMBERS, json={"email": "cy@example.test", "role": "editor"}, headers=h).status_code == 409
    changed = c.patch(f"{MEMBERS}/u2", json={"role": "editor"}, headers=h)
    assert changed.status_code == 200 and changed.json()["roles"] == ["editor"]
    assert c.delete(f"{MEMBERS}/u2", headers=h).status_code == 204
    assert c.delete(f"{MEMBERS}/u2", headers=h).status_code == 404


def test_the_list_says_whether_an_invitation_may_create_a_user(members_app):
    """A source that does not say may create; one that says no (the ZITADEL operator organisation) is passed on, and its
    refusal to create is a 409 with a stable code and what to do instead."""
    c, h = members_app.client, members_app.headers
    assert c.get(MEMBERS, headers=h).json()["can_create_users"] is True

    async def cannot(tenant_id):
        return False

    async def refuse(tenant_id, email, role):
        raise MembershipError("no_user_creation", "create the user in the ZITADEL console first, then add them here")

    members_app.source.can_create_users = cannot
    members_app.source.invite = refuse
    assert c.get(MEMBERS, headers=h).json()["can_create_users"] is False
    r = c.post(MEMBERS, json={"email": "cy@example.test", "role": "viewer"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "no_user_creation" and "ZITADEL console" in r.json()["detail"]


def test_the_last_admin_cannot_be_demoted_or_removed_over_http(members_app):
    c, h = members_app.client, members_app.headers
    r = c.patch(f"{MEMBERS}/u1", json={"role": "viewer"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "last_admin"
    assert members_app.source.members["u1"].roles == ("admin",)


def test_a_bad_role_or_email_is_a_422(members_app):
    c, h = members_app.client, members_app.headers
    assert c.patch(f"{MEMBERS}/u2", json={"role": "owner"}, headers=h).status_code == 422
    assert c.post(MEMBERS, json={"email": "x", "role": "viewer"}, headers=h).status_code == 422


def test_a_read_only_source_lists_but_refuses_changes(monkeypatch):
    tenant = f"t{uuid.uuid4().hex[:10]}"
    register_local(GROUP_MEMBERSHIP, "memory", lambda ctx: MemoryMembership(mutable=False))
    app = make_app(monkeypatch, tenant, EAP_IAM_MEMBERSHIP="memory")
    h = get_auth_header(tenant, "alice")
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        assert c.get(MEMBERS, headers=h).json()["mutable"] is False
        r = c.patch(f"{MEMBERS}/u2", json={"role": "editor"}, headers=h)
        assert r.status_code == 409 and r.json()["code"] == "read_only"
    app.state.database_engine.dispose()


def test_without_a_membership_source_the_page_is_hidden_and_the_routes_are_404(client, tenant):
    h = get_auth_header(tenant, "alice")
    assert client.get(CAPABILITIES, headers=h).json()["membership"] is None
    for call in (client.get(MEMBERS, headers=h), client.post(MEMBERS, json={"email": "a@b.co", "role": "viewer"}, headers=h),
                 client.patch(f"{MEMBERS}/u1", json={"role": "viewer"}, headers=h), client.delete(f"{MEMBERS}/u1", headers=h)):
        assert call.status_code == 404


def test_a_non_admin_is_refused_and_an_unauthenticated_call_is_401(monkeypatch):
    tenant = f"t{uuid.uuid4().hex[:10]}"
    register_local(GROUP_MEMBERSHIP, "memory", lambda ctx: MemoryMembership())
    register_local(GROUP_POLICY, "deny", lambda ctx: FakePolicyEngine(allow=False))
    app = make_app(monkeypatch, tenant, EAP_IAM_MEMBERSHIP="memory", EAP_IAM_POLICY="deny")
    with TestClient(app, client=("127.0.0.1", 40000)) as c:
        h = get_auth_header(tenant, "alice")
        assert c.get(MEMBERS, headers=h).status_code == 404  # may not even read: the same answer as "no such page"
        assert "tenant.manage_members" not in c.get(CAPABILITIES, headers=h).json()["actions"]
        assert c.get(MEMBERS).status_code == 401
    app.state.database_engine.dispose()


# --- a ZITADEL token through the real chain -------------------------------------------------------------


@pytest.fixture
def zt(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tenant = f"t{uuid.uuid4().hex[:10]}"
    jwks = {"keys": [{**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": "k1", "alg": "RS256", "use": "sig"}]}
    monkeypatch.setattr(eap_iam_zitadel, "fetch_jwks", lambda url, host=None: jwks)
    app = make_app(
        monkeypatch, tenant, EAP_IAM_IDENTITY="zitadel,apikey,devloopback", EAP_ZITADEL_ISSUER=ISSUER,
        EAP_ZITADEL_PROJECT_ID=PROJECT, EAP_ZITADEL_ORG_TENANT_MAP=f"{ORG}={tenant}",
    )

    def mint(roles=("admin",), **claims) -> str:
        now = int(time.time())
        body = {
            "iss": ISSUER, "aud": ["c1", PROJECT], "sub": "user-1", "email": "Zed@Example.test", "exp": now + 600, "iat": now,
            "urn:zitadel:iam:user:resourceowner:id": ORG,
            "urn:zitadel:iam:org:project:roles": {r: {ORG: "x"} for r in roles},
        }
        body.update(claims)
        return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm="RS256", headers={"kid": "k1"})

    with TestClient(app, client=("10.42.0.9", 40000)) as client:
        yield SimpleNamespace(client=client, tenant=tenant, mint=mint, app=app)
    engine = app.state.database_engine
    cleanup_tenant(engine, tenant)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": tenant})
    engine.dispose()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_a_zitadel_token_authenticates_through_the_chain(zt):
    r = zt.client.get(CAPABILITIES, headers=auth(zt.mint()))
    assert r.status_code == 200
    body = r.json()
    assert [p["name"] for p in body["identity"]] == ["zitadel", "apikey", "devloopback", "webhook"]  # webhook: always added when installed
    assert body["me"] == {
        "subject": "zitadel:user-1", "email": "zed@example.test", "tenant_id": zt.tenant, "kind": "user",
        "roles": ["admin"], "provider": "zitadel",
    }
    assert zt.client.get("/api/v1/agents", headers=auth(zt.mint())).status_code == 200


def test_the_actor_of_a_change_is_the_zitadel_user(zt):
    spec = {"name": "P1", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}
    created = zt.client.post("/api/v1/agents", json={"spec": spec}, headers=auth(zt.mint()))
    assert created.status_code == 201, created.text
    with zt.app.state.database_engine.connect() as conn:
        row = conn.execute(text("SELECT actor_subject, actor_kind FROM audit_log WHERE tenant_id = :t"), {"t": zt.tenant}).first()
    assert tuple(row) == ("zitadel:user-1", "user")  # the stable subject is recorded...
    with zt.app.state.database_engine.connect() as conn:
        detail = conn.execute(text("SELECT detail FROM audit_log WHERE tenant_id = :t"), {"t": zt.tenant}).scalar()
    assert detail["actor_email"] == "zed@example.test"  # ...and the e-mail rides along for display
    listed = zt.client.get("/api/v1/audit", headers=auth(zt.mint())).json()["items"]
    assert listed[0]["actor_subject"] == "zitadel:user-1" and listed[0]["detail"]["actor_email"] == "zed@example.test"


def test_invalid_tokens_are_401_and_never_fall_through(zt):
    now = int(time.time())
    for label, token in {
        "wrong aud": zt.mint(aud=["c1", "other"]), "wrong iss": zt.mint(iss="https://evil.example"), "expired": zt.mint(exp=now - 600),
    }.items():
        assert zt.client.get("/api/v1/agents", headers=auth(token)).status_code == 401, label
    # An invalid ZITADEL token beside an Access JWT or the dev bearer is not rescued by a later provider.
    assert zt.client.get("/api/v1/agents", headers=auth(zt.mint(exp=now - 600))).status_code == 401


def test_an_unmapped_org_is_403(zt):
    token = zt.mint(**{"urn:zitadel:iam:user:resourceowner:id": "org-stranger"})
    assert zt.client.get("/api/v1/agents", headers=auth(token)).status_code == 403


def test_the_loopback_bearer_and_pod_ip_rules_are_unchanged(zt):
    h = get_auth_header(zt.tenant, "alice")
    assert zt.client.get("/api/v1/agents", headers=h).status_code == 401  # a pod IP never gets the dev bearer
    with TestClient(zt.app, client=("127.0.0.1", 40000)) as local:
        assert local.get("/api/v1/agents", headers=h).status_code == 200


def test_api_keys_still_work_beside_zitadel(zt):
    # A `pos_` key is not JWT-shaped: zitadel returns None and the apikey provider decides. The key is made by a
    # ZITADEL admin (user routes), then used on a key route; a junk key is that provider's 401.
    made = zt.client.post("/api/v1/api-keys", json={"name": "ci", "scopes": ["otlp"]}, headers=auth(zt.mint()))
    assert made.status_code == 201, made.text
    key = made.json()["key"]
    ok = zt.client.post("/api/v1/otlp/v1/traces", json={"resourceSpans": []}, headers=auth(key))
    assert ok.status_code != 401, ok.text
    assert zt.client.post("/api/v1/otlp/v1/traces", json={"resourceSpans": []}, headers=auth("pos_unknown_secret")).status_code == 401
