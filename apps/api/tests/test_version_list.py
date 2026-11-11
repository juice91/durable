"""Tests for version listing and getting."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from pydantic import UUID4

from eap_api.app import create_app
from eap_schemas import AgentSpec, ModelRef
from .conftest import get_auth_header
from .conftest import create_agent, publish_version




class TestListVersions:
    """Tests for GET /api/v1/agents/{id}/versions."""

    def test_list_versions_empty(self, client, tenant):
        """List versions for agent with none."""
        agent_id = create_agent(client, tenant, "user1")
        response = client.get(
            f"/api/v1/agents/{agent_id}/versions",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_list_versions(self, client, tenant):
        """List versions shows published versions newest first."""
        agent_id = create_agent(client, tenant, "user1")
        v1 = publish_version(client, agent_id, tenant, "user1")
        
        # Patch and publish v2
        response = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"spec": {"name": "Updated", "model": {"provider": "openai", "name": "gpt-4"}}},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        
        v2 = publish_version(client, agent_id, tenant, "user1")
        
        response = client.get(
            f"/api/v1/agents/{agent_id}/versions",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        items = response.json()
        assert len(items) == 2
        assert items[0]["version"] == 2  # Newest first
        assert items[1]["version"] == 1
        assert items[0]["spec_hash"] == v2["spec_hash"]
        assert items[1]["spec_hash"] == v1["spec_hash"]


class TestGetVersion:
    """Tests for GET /api/v1/agents/{id}/versions/{n}."""

    def test_get_version_not_found(self, client, tenant):
        """Get non-existent version returns 404."""
        agent_id = create_agent(client, tenant, "user1")
        response = client.get(
            f"/api/v1/agents/{agent_id}/versions/999",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 404

    def test_get_version(self, client, tenant):
        """Get specific version."""
        agent_id = create_agent(client, tenant, "user1")
        v1 = publish_version(client, agent_id, tenant, "user1")
        
        response = client.get(
            f"/api/v1/agents/{agent_id}/versions/1",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        assert response.json()["version"] == 1
        assert response.json()["spec_hash"] == v1["spec_hash"]


class TestTenantVersionIsolation:
    """Tests for version tenant isolation."""

    def test_tenant_b_cannot_read_tenant_a_version(self, client, tenant):
        """Tenant B gets 404 on Tenant A's version."""
        agent_id = create_agent(client, "tenantA", "userA")
        v1 = publish_version(client, agent_id, "tenantA", "userA")
        
        response = client.get(
            f"/api/v1/agents/{agent_id}/versions/1",
            headers=get_auth_header("tenantB", "userB"),
        )
        assert response.status_code == 404

    def test_tenant_b_cannot_list_tenant_a_versions(self, client, tenant):
        """Tenant B gets 404 listing Tenant A's versions."""
        agent_id = create_agent(client, "tenantA", "userA")
        publish_version(client, agent_id, "tenantA", "userA")
        
        response = client.get(
            f"/api/v1/agents/{agent_id}/versions",
            headers=get_auth_header("tenantB", "userB"),
        )
        assert response.status_code == 404

    def test_tenant_b_cannot_publish_tenant_a_version(self, client):
        """Tenant B gets 404 publishing to Tenant A's agent."""
        agent_id = create_agent(client, "tenantA", "userA")
        
        response = client.post(
            f"/api/v1/agents/{agent_id}/versions",
            headers=get_auth_header("tenantB", "userB"),
        )
        assert response.status_code == 404

    def test_tenant_b_cannot_patch_tenant_a_agent_for_version(self, client):
        """Tenant B gets 404 patching Tenant A's agent."""
        agent_id = create_agent(client, "tenantA", "userA")
        
        response = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"spec": {"name": "Hacked", "model": {"provider": "openai", "name": "gpt-4"}}},
            headers=get_auth_header("tenantB", "userB"),
        )
        assert response.status_code == 404
