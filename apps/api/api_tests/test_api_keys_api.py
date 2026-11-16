"""AGUI.3: the API key management routes and `agui_principal`. Implementer's focused tests; the
acceptance suite adds more."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import APIRouter

from eap_api.auth import AgUiPrincipalDep, require_agui_agent

from .conftest import create_agent, get_auth_header


def _probe_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/ag-ui/_probe/agents/{agent_id}")
    async def probe(agent_id: str, principal: AgUiPrincipalDep) -> dict:
        require_agui_agent(principal, agent_id)
        return principal.model_dump() | {"default_resource_id": principal.default_resource_id}

    return router


@pytest.fixture
def probe_client(client):
    client.app.include_router(_probe_router())
    return client


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _create(client, tenant, **body):
    response = client.post("/api/v1/api-keys", json={"name": "ci", **body}, headers=get_auth_header(tenant, "alice"))
    assert response.status_code == 201, response.text
    return response


def test_create_list_revoke(client, tenant, caplog):
    caplog.set_level(logging.DEBUG)
    response = _create(client, tenant)
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    key = body["key"]
    assert key.startswith(body["prefix"]) and body["prefix"] == f"pos_{body['id']}_"
    assert body["status"] == "active" and body["scopes"] == ["agui:run"] and body["created_by"] == "alice"
    listed = client.get("/api/v1/api-keys", headers=get_auth_header(tenant, "alice"))
    assert listed.status_code == 200
    assert [k["id"] for k in listed.json()["items"]] == [body["id"]]
    assert key not in listed.text and "secret" not in listed.text and "key" not in listed.json()["items"][0]
    assert client.delete(f"/api/v1/api-keys/{body['id']}", headers=get_auth_header(tenant, "alice")).status_code == 204
    assert client.delete(f"/api/v1/api-keys/{body['id']}", headers=get_auth_header(tenant, "alice")).status_code == 204
    assert client.get("/api/v1/api-keys", headers=get_auth_header(tenant, "alice")).json()["items"][0]["status"] == "revoked"
    assert key not in caplog.text


def test_revoke_other_tenant_or_unknown_is_404(client, tenant):
    body = _create(client, tenant).json()
    other = f"t{uuid.uuid4().hex[:10]}"
    for key_id in [body["id"], "0" * 24, "nope"]:
        response = client.delete(f"/api/v1/api-keys/{key_id}", headers=get_auth_header(other, "bob"))
        assert response.status_code == 404 and response.json()["detail"] == "API key not found"


def test_management_routes_refuse_a_key(client, tenant):
    key = _create(client, tenant).json()["key"]
    assert client.get("/api/v1/api-keys", headers=_bearer(key)).status_code == 401
    assert client.post("/api/v1/api-keys", json={"name": "x"}, headers=_bearer(key)).status_code == 401
    assert client.get("/api/v1/agents", headers=_bearer(key)).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"scopes": ["admin"]},
        {"allowed_origins": ["https://a.example/x"]},
        {"expires_at": "2000-01-01T00:00:00Z"},
        {"agent_ids": ["no-such-agent"]},
        {"workflow_ids": ["no-such-workflow"]},
        {"extra": 1},
    ],
)
def test_create_validation(client, tenant, body):
    response = client.post("/api/v1/api-keys", json={"name": "ci", **body}, headers=get_auth_header(tenant, "alice"))
    assert response.status_code == 422


def test_agent_allowlist_must_name_own_agents(client, tenant):
    agent_id = create_agent(client, tenant, "alice")
    other = f"t{uuid.uuid4().hex[:10]}"
    assert client.post(
        "/api/v1/api-keys", json={"name": "x", "agent_ids": [agent_id]}, headers=get_auth_header(other, "bob")
    ).status_code == 422
    assert _create(client, tenant, agent_ids=[agent_id]).json()["agent_ids"] == [agent_id]


def test_agui_principal_key_and_errors(probe_client, tenant):
    client = probe_client
    agent_id = create_agent(client, tenant, "alice")
    other_agent = create_agent(client, tenant, "alice")
    created = _create(client, tenant, agent_ids=[agent_id]).json()
    key = created["key"]
    ok = client.get(f"/api/v1/ag-ui/_probe/agents/{agent_id}", headers=_bearer(key))
    assert ok.status_code == 200
    assert ok.json()["kind"] == "api_key" and ok.json()["tenant_id"] == tenant
    assert ok.json()["default_resource_id"] == f"apikey:{created['id']}"
    # Outside the allowlist: the same 404 as a missing agent.
    outside = client.get(f"/api/v1/ag-ui/_probe/agents/{other_agent}", headers=_bearer(key))
    assert outside.status_code == 404 and outside.json()["detail"] == "Agent not found"

    _, _, secret = key.split("_", 2)
    bad_keys = [
        key[:-1] + ("A" if key[-1] != "A" else "B"),  # wrong secret
        f"pos_{'f' * 24}_{secret}",  # unknown id
        "pos_malformed",
    ]
    expired = _create(client, tenant, expires_at=(datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()).json()
    revoked = _create(client, tenant).json()
    client.delete(f"/api/v1/api-keys/{revoked['id']}", headers=get_auth_header(tenant, "alice"))
    engine = client.app.state.database_engine
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("UPDATE api_keys SET expires_at = now() - interval '1 hour' WHERE id = :i"),
                     {"i": expired["id"]})
    bad_keys += [expired["key"], revoked["key"]]
    details = set()
    for bad in bad_keys:
        response = client.get(f"/api/v1/ag-ui/_probe/agents/{agent_id}", headers=_bearer(bad))
        assert response.status_code == 401, bad
        details.add(response.json()["detail"])
    assert details == {"Invalid API key"}


def test_key_works_without_dev_mode_but_dev_bearer_does_not(probe_client, tenant, monkeypatch):
    client = probe_client
    agent_id = create_agent(client, tenant, "alice")
    key = _create(client, tenant).json()["key"]
    monkeypatch.delenv("EAP_AUTH_MODE")
    assert client.get(f"/api/v1/ag-ui/_probe/agents/{agent_id}", headers=_bearer(key)).status_code == 200
    dev = client.get(f"/api/v1/ag-ui/_probe/agents/{agent_id}", headers=get_auth_header(tenant, "alice"))
    assert dev.status_code == 401
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    dev = client.get(f"/api/v1/ag-ui/_probe/agents/{agent_id}", headers=get_auth_header(tenant, "alice"))
    assert dev.status_code == 200 and dev.json()["kind"] == "dev" and dev.json()["default_resource_id"] == "alice"


@pytest.mark.parametrize(
    ("agent_ids", "workflow_ids", "agent_ok", "workflow_ok"),
    [
        (None, None, True, True),  # tenant-wide
        (("ag",), None, True, False),  # a hand-edited row with a NULL other list fails closed
        (None, ("wf",), False, True),
        (("ag",), ("wf",), True, True),
        ((), (), False, False),
    ],
)
def test_principal_allowlists_fail_closed(agent_ids, workflow_ids, agent_ok, workflow_ok):
    from eap_api.auth import AgUiPrincipal

    p = AgUiPrincipal(tenant_id="t", kind="api_key", key_id="k", agent_ids=agent_ids, workflow_ids=workflow_ids)
    assert p.allows_agent("ag") is agent_ok and p.allows_workflow("wf") is workflow_ok
    assert not p.allows_agent("other") or p.is_unrestricted
    assert not p.allows_workflow("other") or p.is_unrestricted
