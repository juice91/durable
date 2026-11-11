"""Additional tests for the control-plane API."""

import os
import tempfile
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from pydantic import UUID4

from eap_api.app import create_app
from eap_schemas import AgentSpec, ModelRef
from .conftest import get_auth_header
from .conftest import create_agent, publish_version




class TestAgentGet:
    """Tests for GET /api/v1/agents/{id}."""

    def test_get_agent_not_found(self, client, tenant):
        """Get non-existent agent returns 404."""
        fake_id = "00000000-0000-4000-8000-000000000000"
        response = client.get(
            f"/api/v1/agents/{fake_id}",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 404

    def test_get_agent(self, client, tenant):
        """Get existing agent."""
        agent_id = create_agent(client, tenant, "user1")
        response = client.get(
            f"/api/v1/agents/{agent_id}",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        assert response.json()["id"] == str(agent_id)


class TestAgentPatch:
    """Tests for PATCH /api/v1/agents/{id}."""

    def test_patch_agent_not_found(self, client, tenant):
        """Patch non-existent agent returns 404."""
        fake_id = "00000000-0000-4000-8000-000000000000"
        response = client.patch(
            f"/api/v1/agents/{fake_id}",
            json={"spec": {"name": "New Name", "model": {"provider": "openai", "name": "gpt-4"}}},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 404

    def test_patch_agent(self, client, tenant):
        """Patch existing agent updates draft."""
        agent_id = create_agent(client, tenant, "user1")
        response = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"spec": {"name": "Updated Name", "model": {"provider": "openai", "name": "gpt-4"}}},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Updated Name"


class TestTenantIsolation:
    """Tests for tenant isolation."""

    def test_tenant_b_cannot_read_tenant_a_agent(self, client, tenant):
        """Tenant B gets 404 on Tenant A's agent."""
        agent_id = create_agent(client, "tenantA", "userA")
        response = client.get(
            f"/api/v1/agents/{agent_id}",
            headers=get_auth_header("tenantB", "userB"),
        )
        assert response.status_code == 404

    def test_tenant_b_cannot_list_tenant_a_agents(self, client, tenant):
        """Tenant B's list is empty when Tenant A has agents."""
        create_agent(client, "tenantA", "userA")
        response = client.get(
            "/api/v1/agents",
            headers=get_auth_header("tenantB", "userB"),
        )
        assert response.status_code == 200
        # Response should be a bare JSON array
        assert isinstance(response.json(), list)
        assert response.json() == []

    def test_tenant_b_cannot_patch_tenant_a_agent(self, client, tenant):
        """Tenant B gets 404 when patching Tenant A's agent."""
        agent_id = create_agent(client, "tenantA", "userA")
        response = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"spec": {"name": "Hacked", "model": {"provider": "openai", "name": "gpt-4"}}},
            headers=get_auth_header("tenantB", "userB"),
        )
        assert response.status_code == 404
