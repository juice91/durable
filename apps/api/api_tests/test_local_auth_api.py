"""`/api/v1/auth/local/*`: the built-in admin signs in, changes the password, renews and signs out; every attempt is
audited; nothing echoes a password. Needs Postgres."""

from __future__ import annotations

import io
import json
import logging
import re

import pytest
from eap_run_store import AuditStore
from sqlalchemy import create_engine

from .conftest import get_auth_header
from .local_auth_helpers import (
    API,
    AUTH,
    CHOSEN,
    INITIAL,
    KEY,
    admin_app,
    bearer,
    clean,
    first_sign_in,
    plugin_tables,
)
from .run_helpers import PG_URL, hard_timeout, requires_pg
from .test_workflow_runs_api import AGENT_SPEC

pytestmark = requires_pg

ROUTES = {
    ("GET", AUTH), ("POST", f"{AUTH}/setup"),
    ("POST", f"{AUTH}/sign-in"), ("POST", f"{AUTH}/password"), ("POST", f"{AUTH}/refresh"), ("POST", f"{AUTH}/sign-out"),
}
WRONG = "wrong-password-000"


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(90):
        yield


@pytest.fixture
def tenants():
    created: list[str] = []
    with plugin_tables():
        yield created
        clean(created)


@pytest.fixture
def admin(monkeypatch, tenants):
    with admin_app(monkeypatch, tenants) as made:
        yield made


def sign_in(client, username="admin", password=CHOSEN, **kwargs):
    return client.post(f"{AUTH}/sign-in", json={"username": username, "password": password}, **kwargs)


def audit(tenant: str) -> list[dict]:
    engine = create_engine(PG_URL)
    try:
        rows, _ = AuditStore(engine).list(tenant, limit=200)
    finally:
        engine.dispose()
    return list(reversed(rows))


def trail(tenant: str) -> list[tuple[str, str, str | None]]:
    return [(r["action"], r["outcome"], (r["detail"] or {}).get("reason")) for r in audit(tenant)]


def test_the_routes_are_exactly_these(admin):
    app, _, _ = admin
    assert {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops if "/auth/local" in p} == ROUTES


def test_the_initial_password_gets_no_token_until_it_is_changed(admin):
    _, client, tenant = admin
    response = sign_in(client, password=INITIAL)
    assert response.status_code == 200
    assert response.json() == {"password_change_required": True}
    assert response.headers["cache-control"] == "no-store"
    session = first_sign_in(client)
    assert session["password_change_required"] is False and session["token_type"] == "Bearer"
    assert session["expires_in"] == 900 and session["access_token"] and session["refresh_token"]
    assert trail(tenant) == [
        ("local_sign_in", "success", "password_change_required"), ("local_password_change", "success", None),
    ]
    assert {r["actor_subject"] for r in audit(tenant)} == {"localadmin:admin"}


def test_the_session_is_the_tenant_admin(admin):
    _, client, tenant = admin
    session = first_sign_in(client)
    me = client.get(f"{API}/iam/capabilities", headers=bearer(session))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["me"] == {"subject": "localadmin:admin", "email": None, "tenant_id": tenant, "kind": "user",
                          "roles": ["admin"], "provider": "localadmin"}
    assert body["membership"] == {"name": "localadmin", "mutable": False}
    members = client.get(f"{API}/iam/members", headers=bearer(session)).json()
    assert [(m["id"], m["roles"]) for m in members["items"]] == [("admin", ["admin"])]
    assert members["mutable"] is False and members["can_create_users"] is False
    invite = client.post(f"{API}/iam/members", json={"email": "a@example.test", "role": "viewer"}, headers=bearer(session))
    assert invite.status_code == 409 and invite.json()["code"] == "read_only"


def test_the_admin_holds_personal_api_keys(admin):
    _, client, _ = admin
    session = first_sign_in(client)
    made = client.post(f"{API}/iam/me/api-keys", json={"name": "laptop", "scopes": ["mcp", "manage"]}, headers=bearer(session))
    assert made.status_code == 201, made.text
    assert client.get(f"{API}/iam/me/api-keys", headers=bearer(session)).status_code == 200


def test_data_stays_inside_the_configured_tenant(monkeypatch, tenants):
    with admin_app(monkeypatch, tenants) as (_, client, first):
        a = bearer(first_sign_in(client))
        created = client.post(f"{API}/agents", json={"spec": AGENT_SPEC}, headers=a)
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]
        with admin_app(monkeypatch, tenants) as (_, other, second):
            b = bearer(first_sign_in(other))
            assert first != second
            assert other.get(f"{API}/agents", headers=b).json() == []
            assert other.get(f"{API}/agents/{agent_id}", headers=b).status_code == 404
            assert other.get(f"{API}/agents", headers=a).status_code == 401  # the first tenant's token is nobody here
            assert {r["actor_subject"] for r in audit(second)} == {"localadmin:admin"}
        assert agent_id in [x["id"] for x in client.get(f"{API}/agents", headers=a).json()]


def test_wrong_username_and_wrong_password_answer_the_same_and_are_audited(admin):
    _, client, tenant = admin
    first_sign_in(client)
    typed_secret = "hunter2-typed-as-username"
    answers = [sign_in(client, password=WRONG), sign_in(client, username=typed_secret, password=WRONG)]
    assert [a.status_code for a in answers] == [401, 401]
    assert answers[0].json() == answers[1].json() == {"detail": "Invalid username or password", "code": "invalid_credentials"}
    assert trail(tenant)[-2:] == [("local_sign_in", "denied", "invalid_credentials")] * 2
    assert [r["actor_subject"] for r in audit(tenant)][-2:] == ["localadmin:admin", "localadmin:unknown"]
    assert typed_secret not in json.dumps(audit(tenant), default=str)


def test_repeated_failures_lock_and_write_no_row_while_locked(admin):
    _, client, tenant = admin
    first_sign_in(client)
    for _ in range(10):
        assert sign_in(client, password=WRONG).status_code == 401
    for _ in range(5):
        locked = sign_in(client)  # the right password
        assert locked.status_code == 429
        assert locked.json() == {"detail": "Too many sign-in attempts. Wait 15 minutes, then try again.", "code": "locked"}
        assert 0 < int(locked.headers["retry-after"]) <= 900
    rows = trail(tenant)[1:]
    assert rows.count(("local_sign_in", "denied", "invalid_credentials")) == 10
    assert rows.count(("local_sign_in_lockout", "denied", "locked")) == 1
    assert len(rows) == 11


def test_forwarded_addresses_from_an_untrusted_peer_do_not_split_the_address_limit(admin):
    _, client, _ = admin
    first_sign_in(client)
    codes = [
        sign_in(client, username=f"u{n}", password=WRONG, headers={"CF-Connecting-IP": f"198.51.100.{n}"}).status_code
        for n in range(21)
    ]
    assert codes == [401] * 20 + [429]


def test_a_trusted_proxys_sender_address_is_the_one_throttled(monkeypatch, tenants):
    with admin_app(monkeypatch, tenants, EAP_TRUSTED_PROXY_CIDRS="192.0.2.0/24") as (_, client, _tenant):
        first_sign_in(client)
        attacker = {"CF-Connecting-IP": "198.51.100.7"}
        for n in range(20):
            sign_in(client, username=f"u{n}", password=WRONG, headers=attacker)
        assert sign_in(client, headers=attacker).status_code == 429
        assert sign_in(client, headers={"CF-Connecting-IP": "198.51.100.8"}).status_code == 200


def test_refresh_rotates_and_a_spent_token_ends_the_session(admin):
    _, client, tenant = admin
    session = first_sign_in(client)
    renewed = client.post(f"{AUTH}/refresh", json={"refresh_token": session["refresh_token"]})
    assert renewed.status_code == 200 and renewed.headers["cache-control"] == "no-store"
    renewed = renewed.json()
    assert renewed["refresh_token"] != session["refresh_token"]
    assert client.get(f"{API}/agents", headers=bearer(renewed)).status_code == 200
    reused = client.post(f"{AUTH}/refresh", json={"refresh_token": session["refresh_token"]})
    assert reused.status_code == 401 and reused.json()["code"] == "invalid_token"
    assert client.get(f"{API}/agents", headers=bearer(renewed)).status_code == 401
    assert client.post(f"{AUTH}/refresh", json={"refresh_token": renewed["refresh_token"]}).status_code == 401
    assert client.post(f"{AUTH}/refresh", json={"refresh_token": "made-up"}).status_code == 401
    assert trail(tenant)[1:] == [("local_session_reuse", "denied", "reused")]


def test_sign_out_ends_the_session_and_answers_the_same_twice(admin):
    _, client, tenant = admin
    session = first_sign_in(client)
    for _ in range(2):
        assert client.post(f"{AUTH}/sign-out", json={"refresh_token": session["refresh_token"]}).status_code == 204
    assert client.get(f"{API}/agents", headers=bearer(session)).status_code == 401
    assert client.post(f"{AUTH}/refresh", json={"refresh_token": session["refresh_token"]}).status_code == 401
    assert trail(tenant)[1:] == [("local_sign_out", "success", None)]


def test_a_weak_new_password_is_refused_and_audited(admin):
    _, client, tenant = admin
    response = client.post(f"{AUTH}/password", json={"username": "admin", "current_password": INITIAL, "new_password": "short"})
    assert response.status_code == 422
    assert response.json() == {"detail": "The password must have at least 12 characters.", "code": "weak_password"}
    assert trail(tenant) == [("local_password_change", "denied", "weak_password")]


@pytest.mark.parametrize(
    "body",
    [
        b'{"username": "admin", "password": 123456789012345}',
        b'{"username": "admin", "passwd": "leaky-secret-value"}',
        b'{"username": "admin", "password": "leaky-secret-value"',
        b'["leaky-secret-value"]',
        b'{"username": "admin", "password": ["leaky-secret-value"]}',
        b'{"username": "admin", "password": "' + b"x" * 20_000 + b'"}',
    ],
)
def test_a_malformed_body_is_refused_without_echoing_it(admin, body):
    _, client, _ = admin
    response = client.post(f"{AUTH}/sign-in", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code in (413, 422), response.text
    for echoed in ("leaky-secret-value", "123456789012345", "xxxxxxxxxx"):
        assert echoed not in response.text


def test_no_password_token_or_key_reaches_a_log_or_an_audit_row(admin, caplog):
    _, client, tenant = admin
    with caplog.at_level(logging.DEBUG):
        session = first_sign_in(client)
        sign_in(client, password=WRONG)
        sign_in(client)
        client.get(f"{API}/agents", headers=bearer(session))
        client.post(f"{AUTH}/sign-out", json={"refresh_token": session["refresh_token"]})
    everything = caplog.text + json.dumps(audit(tenant), default=str)
    for secret in (INITIAL, CHOSEN, WRONG, KEY, session["access_token"], session["refresh_token"]):
        assert secret not in everything


def test_the_reset_command_writes_its_audit_row(admin):
    from eap_iam_localadmin.__main__ import main

    _, client, tenant = admin
    session = first_sign_in(client)

    class Pipe(io.StringIO):
        def isatty(self) -> bool:
            return False

    import os

    out = io.StringIO()
    env = {**os.environ, "EAP_DATABASE_URL": PG_URL}
    assert main(["reset-password"], env=env, stdin=Pipe("reset-password-0004\n"), out=out, err=out) == 0, out.getvalue()
    assert client.get(f"{API}/agents", headers=bearer(session)).status_code == 401
    assert ("local_password_reset", "success", None) in trail(tenant)


def test_without_the_provider_the_routes_are_not_there(client):
    """The default dev chain (apikey, devloopback): sign-in answers like an unknown route."""
    for path, body in [
        ("sign-in", {"username": "admin", "password": WRONG}),
        ("password", {"username": "admin", "current_password": WRONG, "new_password": CHOSEN}),
        ("refresh", {"refresh_token": "x"}),
        ("sign-out", {"refresh_token": "x"}),
        ("setup", {"setup_code": "x", "username": "admin", "password": CHOSEN}),
    ]:
        response = client.post(f"{AUTH}/{path}", json=body, headers=get_auth_header("studio", "operator"))
        assert response.status_code == 404, (path, response.text)
    assert client.get(AUTH).status_code == 404


def test_the_api_refuses_to_start_without_the_signing_key(monkeypatch, tenants):
    with pytest.raises(RuntimeError, match="EAP_LOCAL_AUTH_SIGNING_KEY"):
        with admin_app(monkeypatch, tenants, EAP_LOCAL_AUTH_SIGNING_KEY=""):
            pass


# --- first-run setup ---------------------------------------------------------------------------------------------------

NO_ACCOUNT = {"EAP_LOCAL_ADMIN_USERNAME": "", "EAP_LOCAL_ADMIN_INITIAL_PASSWORD": ""}
CODE = re.compile(r"setup code: ((?:[A-Z2-7]{4}-){7}[A-Z2-7]{4})\b")


@pytest.fixture
def fresh(monkeypatch, tenants, capsys):
    """(client, tenant, the code the API printed at start) with no admin yet."""
    with admin_app(monkeypatch, tenants, **NO_ACCOUNT) as (_, client, tenant):
        (code,) = CODE.findall(capsys.readouterr().err)
        yield client, tenant, code


def set_up(client, code, username="owner", password=CHOSEN, **kwargs):
    return client.post(f"{AUTH}/setup", json={"setup_code": code, "username": username, "password": password}, **kwargs)


def test_the_setup_code_creates_the_admin_who_then_signs_in(fresh, caplog):
    client, tenant, code = fresh
    status = client.get(AUTH)
    assert status.json() == {"setup_required": True} and status.headers["cache-control"] == "no-store"
    with caplog.at_level(logging.DEBUG):
        created = set_up(client, code)
    assert created.status_code == 201, created.text
    assert created.json() == {"username": "owner"} and created.headers["cache-control"] == "no-store"
    assert client.get(AUTH).json() == {"setup_required": False}
    signed = sign_in(client, "owner")
    assert signed.status_code == 200
    me = client.get(f"{API}/iam/capabilities", headers=bearer(signed.json())).json()["me"]
    assert (me["subject"], me["tenant_id"], me["roles"]) == ("localadmin:owner", tenant, ["admin"])
    assert trail(tenant)[0] == ("local_setup", "success", None)
    assert audit(tenant)[0]["actor_subject"] == "localadmin:owner"
    everything = caplog.text + json.dumps(audit(tenant), default=str) + created.text
    assert code not in everything and CHOSEN not in everything


def test_setup_is_closed_once_the_admin_exists(fresh):
    client, tenant, code = fresh
    assert set_up(client, code).status_code == 201
    before = trail(tenant)
    for again in (code, "AAAA"):
        closed = set_up(client, again, username="intruder")
        assert closed.status_code == 404, closed.text
    assert trail(tenant) == before  # an anonymous caller adds nothing to the log once setup is over


def test_an_env_made_admin_keeps_setup_closed(admin, capsys):
    _, client, _ = admin
    assert client.get(AUTH).json() == {"setup_required": False}
    assert set_up(client, "AAAA").status_code == 404


def test_a_wrong_code_is_refused_audited_and_locks_after_twenty(fresh):
    client, tenant, code = fresh
    first = set_up(client, "AAAA-BBBB")
    assert first.status_code == 401 and first.json()["code"] == "invalid_setup_code"
    for _ in range(19):
        set_up(client, "AAAA-BBBB")
    locked = set_up(client, code)
    assert locked.status_code == 429 and locked.headers["retry-after"]
    rows = trail(tenant)
    assert rows.count(("local_setup", "denied", "invalid_setup_code")) == 20
    assert rows.count(("local_sign_in_lockout", "denied", "locked")) == 1 and len(rows) == 21
    assert {r["actor_subject"] for r in audit(tenant)} == {"localadmin:unknown"}


@pytest.mark.parametrize(
    ("username", "password", "code_"), [("has space", CHOSEN, "invalid_username"), ("owner", "short", "weak_password")]
)
def test_setup_refuses_a_bad_username_or_password(fresh, username, password, code_):
    client, tenant, code = fresh
    refused = set_up(client, code, username, password)
    assert refused.status_code == 422 and refused.json()["code"] == code_
    assert set_up(client, code).status_code == 201
