"""Tests for the control-plane API."""

import os
import tempfile
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from pydantic import UUID4

from eap_api.app import create_app
from eap_api.auth import Principal
from eap_schemas import AgentSpec, ModelRef, ModelSettings
from .conftest import get_auth_header




class TestHealthz:
    """Tests for the healthz endpoint."""

    def test_healthz_ok(self, client):
        """Healthz endpoint returns ok without auth."""
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAuth:
    """Tests for authentication."""

    @pytest.mark.parametrize("auth_mode", [None, "production", "DEV"])
    def test_app_refuses_to_start_without_explicit_dev_auth(self, monkeypatch, auth_mode):
        if auth_mode is None:
            monkeypatch.delenv("EAP_AUTH_MODE", raising=False)
        else:
            monkeypatch.setenv("EAP_AUTH_MODE", auth_mode)

        with pytest.raises(
            RuntimeError,
            match="no production authentication is configured; set EAP_AUTH_MODE=dev for local development only",
        ):
            create_app(database_url="postgresql+psycopg://unused@localhost:1/unused")

    def test_missing_authorization_header(self, client, tenant):
        """Missing Authorization header returns 401."""
        response = client.get("/api/v1/agents", headers={})
        assert response.status_code == 401
        assert "Missing Authorization header" in response.json()["detail"]

    def test_malformed_authorization_header(self, client, tenant):
        """Malformed Authorization header returns 401."""
        response = client.get(
            "/api/v1/agents",
            headers={"Authorization": "Bearer invalid"},
        )
        assert response.status_code == 401

    def test_invalid_tenant_format(self, client, tenant):
        """Invalid tenant format returns 401."""
        response = client.get(
            "/api/v1/agents",
            headers={"Authorization": "Bearer dev:invalid tenant:user"},
        )
        assert response.status_code == 401

    def test_invalid_user_format(self, client, tenant):
        """Invalid user format returns 401."""
        response = client.get(
            "/api/v1/agents",
            headers={"Authorization": "Bearer dev:tenant:invalid user"},
        )
        assert response.status_code == 401

    def test_empty_tenant(self, client, tenant):
        """Empty tenant returns 401."""
        response = client.get(
            "/api/v1/agents",
            headers={"Authorization": "Bearer dev::user"},
        )
        assert response.status_code == 401

    def test_empty_user(self, client, tenant):
        """Empty user returns 401."""
        response = client.get(
            "/api/v1/agents",
            headers={"Authorization": "Bearer dev:tenant:"},
        )
        assert response.status_code == 401

    def test_valid_authorization(self, client, tenant):
        """Valid Authorization header works."""
        response = client.get(
            "/api/v1/agents",
            headers=get_auth_header(tenant, "user1"),
        )
        # Should get 200 with empty list (no agents yet)
        assert response.status_code == 200
        # Response should be a bare JSON array
        assert isinstance(response.json(), list)
        assert response.json() == []


class TestAgents:
    """Tests for agent CRUD operations."""

    def test_create_agent(self, client, tenant):
        """Create a new agent."""
        spec = AgentSpec(
            name="Test Agent",
            instructions="Test instructions",
            model=ModelRef(provider="openai", name="gpt-4"),
            tools=("tool_one", "tool_two"),
        )
        response = client.post(
            "/api/v1/agents",
            json={"spec": spec.model_dump(mode="json")},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Test Agent"
        # Response follows contract: {id, name, draft_spec, latest_version}
        assert set(data.keys()) == {"id", "name", "draft_spec", "latest_version", "latest_spec_summary", "draft_dirty", "archived", "builtin", "builtin_role", "revision"}
        assert data["draft_spec"] is not None
        assert data["latest_version"] is None
        assert data["latest_spec_summary"] is None
        assert data["draft_dirty"] is False
        assert UUID4(data["id"])

    def test_list_and_get_report_the_published_model_and_dirty_draft(self, client, tenant):
        """The list and GET carry the published version's summary; draft_dirty flags unpublished edits."""
        headers = get_auth_header(tenant, "user1")
        spec = AgentSpec(name="Pub", model=ModelRef(provider="litellm", name="gemma"), tools=("tool_one",))
        agent = client.post("/api/v1/agents", json={"spec": spec.model_dump(mode="json")}, headers=headers).json()
        aid = agent["id"]
        client.post(f"/api/v1/agents/{aid}/versions", headers=headers).raise_for_status()
        edited = AgentSpec(name="Pub", model=ModelRef(provider="anthropic", name="claude"), tools=("tool_one", "tool_two"))

        def both():
            listed = next(a for a in client.get("/api/v1/agents", headers=headers).json() if a["id"] == aid)
            return listed, client.get(f"/api/v1/agents/{aid}", headers=headers).json()

        for body in both():
            assert body["latest_spec_summary"]["version"] == 1
            assert body["latest_spec_summary"]["model"] == {"provider": "litellm", "name": "gemma"}
            assert body["latest_spec_summary"]["tool_count"] == 1
            assert body["draft_dirty"] is False
        patched = client.patch(f"/api/v1/agents/{aid}", json={"spec": edited.model_dump(mode="json")}, headers=headers)
        assert patched.status_code == 200, patched.text
        for body in both():
            assert body["draft_dirty"] is True
            assert body["latest_spec_summary"]["model"]["name"] == "gemma"  # still what runs
            assert body["draft_spec"]["model"]["name"] == "claude"
    def test_create_agent_invalid_spec(self, client, tenant):
        """Create with invalid spec returns 422."""
        response = client.post(
            "/api/v1/agents",
            json={"spec": {"name": "", "model": {"provider": "", "name": ""}}},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 422

    def test_create_agent_invalid_tool_id(self, client, tenant):
        """Create with invalid tool id returns 422."""
        response = client.post(
            "/api/v1/agents",
            json={"spec": {"name": "Test", "model": {"provider": "openai", "name": "gpt-4"}, "tools": ("INVALID_TOOL",)}},  # uppercase not allowed
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 422

    def test_list_agents_empty(self, client, tenant):
        """List agents returns empty array when none exist."""
        response = client.get(
            "/api/v1/agents",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        # Response should be a bare JSON array
        assert isinstance(response.json(), list)
        assert response.json() == []

    def test_list_agents(self, client, tenant):
        """List agents returns created agents."""
        spec = AgentSpec(
            name="Agent 1",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        client.post(
            "/api/v1/agents",
            json={"spec": spec.model_dump(mode="json")},
            headers=get_auth_header(tenant, "user1"),
        )
        
        response = client.get(
            "/api/v1/agents",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        # Response should be a bare JSON array
        assert isinstance(response.json(), list)
        assert len(response.json()) == 1
        agent = response.json()[0]
        assert agent["name"] == "Agent 1"
        # Response follows contract: {id, name, draft_spec, latest_version}
        assert set(agent.keys()) == {"id", "name", "draft_spec", "latest_version", "latest_spec_summary", "draft_dirty", "archived", "builtin", "builtin_role", "revision"}
