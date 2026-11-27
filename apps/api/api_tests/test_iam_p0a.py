"""IAM P0a through the API: start-up rules, the chain behind the dependencies, the loopback rule over the app, and
`GET /api/v1/iam/capabilities`."""

import re

import pytest
from fastapi.testclient import TestClient

from eap_api.app import create_app

from .conftest import get_auth_header
from .run_helpers import PG_URL

UNUSED_DB = "postgresql+psycopg://unused@localhost:1/unused"
CAPABILITIES = "/api/v1/iam/capabilities"


def refuses(monkeypatch, match, **env):
    for name in ("EAP_IAM_IDENTITY", "EAP_IAM_POLICY", "EAP_IAM_MEMBERSHIP", "EAP_IAM_ALLOW_TRANSITIONAL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=re.escape(match)):
        create_app(database_url=UNUSED_DB)


def test_unset_auth_mode_refuses_naming_the_variables(monkeypatch):
    monkeypatch.delenv("EAP_AUTH_MODE")
    refuses(monkeypatch, "EAP_IAM_IDENTITY")


def test_production_without_an_identity_provider_refuses(monkeypatch):
    refuses(monkeypatch, "EAP_IAM_IDENTITY", EAP_AUTH_MODE="production", EAP_IAM_POLICY="openaccess")


def test_production_without_a_policy_engine_refuses(monkeypatch):
    refuses(monkeypatch, "exactly one policy engine", EAP_AUTH_MODE="production", EAP_IAM_IDENTITY="apikey")


def test_an_unknown_or_duplicate_plugin_name_refuses(monkeypatch):
    refuses(monkeypatch, "not installed: nope", EAP_AUTH_MODE="dev", EAP_IAM_IDENTITY="apikey,nope")
    refuses(monkeypatch, "twice", EAP_AUTH_MODE="dev", EAP_IAM_IDENTITY="apikey,apikey")


def test_the_transitional_engine_is_refused_in_production_unless_allowed(monkeypatch):
    if not (PG_URL or "").startswith("postgresql"):
        pytest.skip("the API needs Postgres")
    env = {"EAP_AUTH_MODE": "production", "EAP_IAM_IDENTITY": "apikey", "EAP_IAM_POLICY": "openaccess"}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("EAP_IAM_ALLOW_TRANSITIONAL", raising=False)
    with pytest.raises(RuntimeError, match="transitional"):
        create_app(database_url=PG_URL)
    monkeypatch.setenv("EAP_IAM_ALLOW_TRANSITIONAL", "1")
    app = create_app(database_url=PG_URL)
    app.state.database_engine.dispose()


def test_dev_with_the_plugins_starts_as_before(client, tenant):
    assert client.get("/api/v1/agents", headers=get_auth_header(tenant, "alice")).status_code == 200


def test_capabilities_list_the_active_plugins_and_the_caller(client, tenant):
    r = client.get(CAPABILITIES, headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 200
    body = r.json()
    names = [p["name"] for p in body["identity"]]
    assert body["mode"] == "dev" and names == ["apikey", "devloopback", "webhook"] or names == ["fake"]
    assert body["policy"]["name"] in ("openaccess", "fake")
    assert body["membership"] is None
    assert body["me"]["subject"] == "alice" and body["me"]["tenant_id"] == tenant and body["me"]["kind"] == "user"
    assert "agent.publish" in body["actions"]
    # Names and flags only: nothing resembling configuration or a credential.
    assert "dev:" not in r.text and "pos_" not in r.text


def test_capabilities_need_authentication_and_refuse_keys(client):
    assert client.get(CAPABILITIES).status_code == 401
    assert client.get(CAPABILITIES, headers={"Authorization": "Bearer pos_" + "a" * 24 + "_" + "b" * 43}).status_code == 401


@pytest.fixture
def remote_client(client):
    """The same app, reached from a non-loopback TCP peer."""
    with TestClient(client.app, client=("10.42.0.9", 40000)) as remote:
        yield remote


def test_the_dev_bearer_from_a_non_loopback_peer_is_401(remote_client, tenant):
    for extra in ({}, {"X-Forwarded-For": "127.0.0.1"}):
        r = remote_client.get("/api/v1/agents", headers={**get_auth_header(tenant, "alice"), **extra})
        assert r.status_code == 401, extra


def test_the_dev_bearer_from_the_loopback_peer_works_and_a_proxy_header_breaks_it(client, tenant):
    with TestClient(client.app, client=("127.0.0.1", 40000)) as local:
        headers = get_auth_header(tenant, "alice")
        assert local.get("/api/v1/agents", headers=headers).status_code == 200
        assert local.get("/api/v1/agents", headers={**headers, "X-Forwarded-For": "1.2.3.4"}).status_code == 401


def test_capability_registry_routes_go_through_the_identity_chain(client, remote_client, tenant):
    """The registry routes authenticate with current_principal: the loopback rule and the chain apply to them."""
    headers = get_auth_header(tenant, "alice")
    assert client.get("/api/v1/capabilities").status_code == 401
    assert client.get("/api/v1/capabilities", headers=headers).status_code == 200
    assert remote_client.get("/api/v1/capabilities", headers=headers).status_code == 401
    assert client.get("/api/v1/capabilities", headers={"Authorization": "Bearer pos_" + "a" * 24 + "_" + "b" * 43}).status_code == 401
