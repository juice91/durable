"""Publish MCP tools validation (milestone 3, task 3.5)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from eap_api.app import create_app
from eap_schemas import AgentSpec, McpToolRef, canonical_spec_hash

from sqlalchemy import text


@pytest.fixture(autouse=True)
def dev_auth_mode(monkeypatch: pytest.MonkeyPatch):
    """API tests explicitly opt in to the local-only authentication stub."""
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")


@pytest.fixture(autouse=True)
def allowed_models(monkeypatch: pytest.MonkeyPatch):
    """Model allowlist for API tests."""
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,openai:gpt-4")


@pytest.fixture
def client():
    """A test client on Postgres."""
    from .run_helpers import PG_URL

    if not (PG_URL or "").startswith("postgresql"):
        pytest.skip("the API needs Postgres")
    app = create_app(database_url=PG_URL)
    with TestClient(app) as test_client:
        yield test_client
    app.state.database_engine.dispose()


def get_auth_header(tenant: str, user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer dev:{tenant}:{user}"}


def create_agent(client, tenant: str, user_id: str, spec: dict | None = None) -> str:
    """Helper to create an agent."""
    if spec is None:
        spec = {
            "spec_version": 1,
            "name": "Test Agent",
            "instructions": "",
            "model": {"provider": "test", "name": "mcp"},
            "tools": [],
            "model_settings": {},
        }
    response = client.post(
        "/api/v1/agents",
        json={"spec": spec},
        headers=get_auth_header(tenant, user_id),
    )
    assert response.status_code == 201
    return response.json()["id"]


def patch_agent(client, agent_id: str, tenant: str, user_id: str, spec: dict) -> None:
    """Patch an agent's draft spec."""
    response = client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"spec": spec},
        headers=get_auth_header(tenant, user_id),
    )
    assert response.status_code == 200, response.json()


def publish_version(client, agent_id: str, tenant: str, user_id: str) -> dict:
    """Helper to publish a version."""
    response = client.post(
        f"/api/v1/agents/{agent_id}/versions",
        headers=get_auth_header(tenant, user_id),
    )
    return response


def test_publish_with_enabled_server_succeeds(client, dev_auth_mode):
    """201 with an enabled server of this tenant."""
    tenant = f"t{uuid.uuid4().hex[:10]}"
    user = "alice"
    agent_id = create_agent(client, tenant, user)

    # Insert a server directly into the database
    with client.app.state.database_engine.connect() as conn:
        server_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO mcp_servers (id, tenant_id, name, url, transport, enabled, created_at, updated_at) "
                "VALUES (:id, :t, :n, :u, 'streamable_http', true, now(), now())"
            ),
            {"id": server_id, "t": tenant, "n": "orders", "u": "http://127.0.0.1:12345/mcp"},
        )
        conn.commit()

    # Patch the agent with an MCP spec
    spec = {
        "spec_version": 1,
        "name": "Test Agent",
        "instructions": "",
        "model": {"provider": "test", "name": "mcp"},
        "tools": [],
        "model_settings": {},
        "mcp_tools": [{"server": "orders", "tool": "lookup_order"}],
    }
    patch_agent(client, agent_id, tenant, user, spec)

    response = publish_version(client, agent_id, tenant, user)
    assert response.status_code == 201, response.json()


def test_publish_with_unknown_server_returns_422(client, dev_auth_mode):
    """422 for an unknown server name."""
    tenant = f"t{uuid.uuid4().hex[:10]}"
    user = "alice"
    agent_id = create_agent(client, tenant, user)

    # Patch the agent with an MCP spec referencing unknown server
    spec = {
        "spec_version": 1,
        "name": "Test Agent",
        "instructions": "",
        "model": {"provider": "test", "name": "mcp"},
        "tools": [],
        "model_settings": {},
        "mcp_tools": [{"server": "unknown_server", "tool": "lookup_order"}],
    }
    patch_agent(client, agent_id, tenant, user, spec)

    response = publish_version(client, agent_id, tenant, user)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "unknown_server" in detail


def test_publish_with_other_tenant_server_returns_422(client, dev_auth_mode):
    """422 for another tenant's server with the same name."""
    tenant1 = f"t{uuid.uuid4().hex[:10]}"
    tenant2 = f"t{uuid.uuid4().hex[:10]}"
    user = "alice"

    # Create a server in tenant1
    with client.app.state.database_engine.connect() as conn:
        server_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO mcp_servers (id, tenant_id, name, url, transport, enabled, created_at, updated_at) "
                "VALUES (:id, :t, :n, :u, 'streamable_http', true, now(), now())"
            ),
            {"id": server_id, "t": tenant1, "n": "orders", "u": "http://127.0.0.1:12345/mcp"},
        )
        conn.commit()

    agent_id = create_agent(client, tenant2, user)  # Agent in tenant2

    # Patch the agent with an MCP spec referencing server from tenant1
    spec = {
        "spec_version": 1,
        "name": "Test Agent",
        "instructions": "",
        "model": {"provider": "test", "name": "mcp"},
        "tools": [],
        "model_settings": {},
        "mcp_tools": [{"server": "orders", "tool": "lookup_order"}],
    }
    patch_agent(client, agent_id, tenant2, user, spec)

    response = publish_version(client, agent_id, tenant2, user)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "orders" in detail


def test_publish_with_disabled_server_returns_422(client, dev_auth_mode):
    """422 for a disabled server."""
    tenant = f"t{uuid.uuid4().hex[:10]}"
    user = "alice"
    agent_id = create_agent(client, tenant, user)

    # Insert a disabled server
    with client.app.state.database_engine.connect() as conn:
        server_id = str(uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO mcp_servers (id, tenant_id, name, url, transport, enabled, created_at, updated_at) "
                "VALUES (:id, :t, :n, :u, 'streamable_http', false, now(), now())"
            ),
            {"id": server_id, "t": tenant, "n": "orders", "u": "http://127.0.0.1:12345/mcp"},
        )
        conn.commit()

    # Patch the agent with an MCP spec referencing the disabled server
    spec = {
        "spec_version": 1,
        "name": "Test Agent",
        "instructions": "",
        "model": {"provider": "test", "name": "mcp"},
        "tools": [],
        "model_settings": {},
        "mcp_tools": [{"server": "orders", "tool": "lookup_order"}],
    }
    patch_agent(client, agent_id, tenant, user, spec)

    response = publish_version(client, agent_id, tenant, user)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "orders" in detail
