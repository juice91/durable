"""Personal API keys through the API: create / list / revoke own keys, the role cap on scopes, expiry, who may hold one,
attribution in the audit log, a removed member's keys, and the admin listing's owner column. The identity is a fake
provider named like a real person provider (`zitadel` as the principal's provider) and the membership source is in memory."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from eap_iam import GROUP_IDENTITY, GROUP_MEMBERSHIP, AuthError, Member, Principal, register_local
from eap_iam.registry import clear_local
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.auth import authenticate_request

from .conftest import get_auth_header
from .run_helpers import PG_URL, hard_timeout, requires_pg

pytestmark = requires_pg

ME = "/api/v1/iam/me/api-keys"
ADMIN_KEYS = "/api/v1/api-keys"


class PersonIdentity:
    """`Authorization: Bearer person:<tenant>:<subject>` is a person of the tenant (provider `zitadel`, kind user)."""

    name = "person"
    kinds = frozenset({"user"})

    async def authenticate(self, request):
        header = request.header("authorization") or ""
        if not header.startswith("Bearer person:"):
            return None
        _, tenant, sub = header.removeprefix("Bearer ").split(":", 2)
        return Principal(
            tenant_id=tenant, kind="user", subject=f"zitadel:{sub}", provider="zitadel",
            extra={"email": f"{sub}@example.test"},
        )


class Members:
    name = "memory"
    mutable = True

    def __init__(self):
        self.roles = {"zitadel:ada": ("admin",), "zitadel:ed": ("editor",), "zitadel:vi": ("viewer",), "zitadel:bo": ("admin",)}
        self.down = False

    async def roles_of(self, tenant_id, subject):
        if self.down:
            raise RuntimeError("boom")
        return frozenset(self.roles.get(subject, ()))

    async def list_members(self, tenant_id):
        return [Member(id=k, roles=v) for k, v in self.roles.items()]


@pytest.fixture(autouse=True)
def _clean():
    clear_local()
    with hard_timeout(90):
        yield
    clear_local()


@pytest.fixture
def pk(monkeypatch):
    from eap_secrets import KEY_ENV, generate_key

    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.setenv("EAP_IAM_IDENTITY", "person,apikey,devloopback")
    monkeypatch.setenv("EAP_IAM_MEMBERSHIP", "memory")
    tenant = f"t{uuid.uuid4().hex[:10]}"
    members = Members()
    register_local(GROUP_IDENTITY, "person", lambda ctx: PersonIdentity())
    register_local(GROUP_MEMBERSHIP, "memory", lambda ctx: members)
    app = create_app(database_url=PG_URL)
    router = APIRouter()

    @router.post("/api/v1/_probe/act")
    async def act(request: Request) -> dict:
        p = await authenticate_request(request, frozenset({"api_key"}), None)
        return {"scopes": sorted(p.scopes), "roles": sorted(p.roles), "on_behalf_of": p.on_behalf_of}

    app.include_router(router)
    with TestClient(app, client=("10.42.0.9", 40000)) as client:  # not loopback: the dev bearer is refused here
        yield SimpleNamespace(client=client, tenant=tenant, members=members, app=app)
    engine = app.state.database_engine
    with engine.begin() as conn:
        for table in ("api_keys", "audit_log"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
    engine.dispose()


def person(pk, sub="ada"):
    return {"Authorization": f"Bearer person:{pk.tenant}:{sub}"}


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def make(pk, sub="ada", **body):
    r = pk.client.post(ME, json={"name": "laptop", **body}, headers=person(pk, sub))
    assert r.status_code == 201, r.text
    return r.json()


def test_create_list_revoke_own_key_and_the_secret_is_shown_once(pk, caplog):
    caplog.set_level(logging.DEBUG)
    created = make(pk, scopes=["mcp", "manage"])
    assert created["key"].startswith(created["prefix"]) and created["status"] == "active"
    assert created["scopes"] == ["mcp", "manage"]
    exp = datetime.fromisoformat(created["expires_at"])
    assert timedelta(days=89) < exp - datetime.now(timezone.utc) <= timedelta(days=90, minutes=1)
    r = pk.client.get(ME, headers=person(pk))
    assert r.status_code == 200 and [k["id"] for k in r.json()["items"]] == [created["id"]]
    assert created["key"] not in r.text and "key" not in r.json()["items"][0]
    assert pk.client.delete(f"{ME}/{created['id']}", headers=person(pk)).status_code == 204
    assert pk.client.delete(f"{ME}/{created['id']}", headers=person(pk)).status_code == 204  # idempotent
    assert pk.client.get(ME, headers=person(pk)).json()["items"][0]["status"] == "revoked"
    assert pk.client.post("/api/v1/_probe/act", headers=bearer(created["key"])).status_code == 401
    assert created["key"] not in caplog.text


def test_nobody_sees_or_revokes_someone_elses_key(pk):
    mine = make(pk, "ada")
    assert pk.client.get(ME, headers=person(pk, "bo")).json()["items"] == []
    assert pk.client.delete(f"{ME}/{mine['id']}", headers=person(pk, "bo")).status_code == 404
    assert pk.client.get(ME, headers=person(pk, "ada")).json()["items"][0]["status"] == "active"
    # a tenant key is not "mine" either, and an unknown id is the same 404
    tenant_key = pk.client.post(ADMIN_KEYS, json={"name": "ci"}, headers=get_auth_header(pk.tenant, "ada"))
    assert pk.client.delete(f"{ME}/{'0' * 24}", headers=person(pk, "ada")).status_code == 404
    assert tenant_key.status_code in (201, 401)  # the dev bearer is refused from a non-loopback peer


def test_the_role_caps_the_scopes(pk):
    for sub, ok, refused in (
        ("vi", ["mcp"], ["manage"]),
        ("ed", ["agui:run", "mcp", "manage"], ["otlp"]),
        ("ada", ["agui:run", "mcp", "manage", "otlp"], []),
    ):
        assert make(pk, sub, scopes=ok)["scopes"] == ok
        for scope in refused:
            r = pk.client.post(ME, json={"name": "x", "scopes": [scope]}, headers=person(pk, sub))
            assert r.status_code == 403 and scope in r.text, r.text


def test_a_stranger_is_refused(pk):
    r = pk.client.post(ME, json={"name": "x"}, headers=person(pk, "nobody"))
    assert r.status_code == 403 and "not a member" in r.text


def test_expiry_default_maximum_and_past(pk):
    soon = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    assert make(pk, expires_at=soon)["expires_at"].startswith(soon[:10])
    far = (datetime.now(timezone.utc) + timedelta(days=366)).isoformat()
    assert pk.client.post(ME, json={"name": "x", "expires_at": far}, headers=person(pk)).status_code == 422
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert pk.client.post(ME, json={"name": "x", "expires_at": past}, headers=person(pk)).status_code == 422
    assert pk.client.post(ME, json={"name": "x", "expires_at": "2030-01-01T00:00:00"}, headers=person(pk)).status_code == 422


def test_an_api_key_is_refused_at_authentication_and_the_dev_bearer_with_a_clear_message(pk):
    key = make(pk)["key"]
    for call in (
        pk.client.get(ME, headers=bearer(key)),
        pk.client.post(ME, json={"name": "x"}, headers=bearer(key)),
        pk.client.delete(f"{ME}/{'0' * 24}", headers=bearer(key)),
    ):
        assert call.status_code == 401, (call.status_code, call.text)  # refused at authentication, like any people route
    with TestClient(pk.app, client=("127.0.0.1", 40001)) as local:
        r = local.post(ME, json={"name": "x"}, headers=get_auth_header(pk.tenant, "ada"))
        assert r.status_code == 403 and "signed-in person" in r.text
    assert pk.client.get(ME).status_code == 401


def test_a_personal_key_acts_as_its_owner_and_the_audit_log_names_the_person(pk):
    created = make(pk, "ed", scopes=["agui:run", "mcp", "manage"])
    r = pk.client.post("/api/v1/_probe/act", headers=bearer(created["key"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scopes"] == ["agui:run", "manage", "mcp"] and body["roles"] == ["editor"] and body["on_behalf_of"] == "zitadel:ed"
    engine = pk.app.state.database_engine
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT actor_kind, actor_subject, detail FROM audit_log WHERE tenant_id = :t AND action = 'act'"),
            {"t": pk.tenant},
        ).all()
    assert len(rows) == 1
    kind, subject, detail = rows[0]
    assert (kind, subject) == ("user", "zitadel:ed") and detail["via_api_key"] == created["id"]
    assert created["key"] not in str(detail)


def test_roles_are_the_owners_current_roles_and_a_removed_member_is_rejected(pk):
    created = make(pk, "ed", scopes=["agui:run", "mcp", "manage"])
    # a new provider process would have an empty cache: build the situation with a fresh app instead of waiting
    provider = next(p for p in pk.app.state.iam.providers if p.name == "apikey")
    pk.members.roles["zitadel:ed"] = ("viewer",)
    provider._owner_roles.clear()
    assert pk.client.post("/api/v1/_probe/act", headers=bearer(created["key"])).json()["scopes"] == ["mcp"]
    del pk.members.roles["zitadel:ed"]
    provider._owner_roles.clear()
    assert pk.client.post("/api/v1/_probe/act", headers=bearer(created["key"])).status_code == 401
    pk.members.roles["zitadel:ed"] = ("editor",)  # reinstated: the key works again
    provider._owner_roles.clear()
    assert pk.client.post("/api/v1/_probe/act", headers=bearer(created["key"])).status_code == 200


def test_a_membership_source_that_is_down_fails_closed(pk):
    created = make(pk, "ed", scopes=["mcp"])
    provider = next(p for p in pk.app.state.iam.providers if p.name == "apikey")
    provider._owner_roles.clear()
    pk.members.down = True
    assert pk.client.post("/api/v1/_probe/act", headers=bearer(created["key"])).status_code == 503
    assert pk.client.post(ME, json={"name": "x"}, headers=person(pk, "ed")).status_code == 503


def test_admin_listing_shows_the_owner_and_can_revoke_any_key(pk):
    created = make(pk, "ed")
    engine = pk.app.state.database_engine
    # the admin section (people route, loopback dev bearer) sees tenant keys and personal keys alike
    with TestClient(pk.app, client=("127.0.0.1", 40002)) as local:
        h = get_auth_header(pk.tenant, "admin")
        tenant_key = local.post(ADMIN_KEYS, json={"name": "ci"}, headers=h).json()
        items = {k["id"]: k for k in local.get(ADMIN_KEYS, headers=h).json()["items"]}
        assert items[created["id"]]["owner_subject"] == "zitadel:ed" and items[created["id"]]["owner_email"] == "ed@example.test"
        assert items[tenant_key["id"]]["owner_subject"] is None
        assert local.delete(f"{ADMIN_KEYS}/{created['id']}", headers=h).status_code == 204
    assert pk.client.get(ME, headers=person(pk, "ed")).json()["items"][0]["status"] == "revoked"
    with engine.connect() as conn:  # only a hash is stored
        assert conn.execute(text("SELECT secret_hash FROM api_keys WHERE id = :i"), {"i": created["id"]}).scalar() != created["key"]


def test_the_per_person_cap(pk, monkeypatch):
    from eap_run_store import api_keys

    monkeypatch.setattr(api_keys, "MAX_ACTIVE_API_KEYS_PER_OWNER", 2)
    make(pk, "ed")
    make(pk, "ed")
    r = pk.client.post(ME, json={"name": "x"}, headers=person(pk, "ed"))
    assert r.status_code == 409
    make(pk, "ada")  # another person is not affected
