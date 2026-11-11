"""Tests for version endpoints."""

import os
import tempfile
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from pydantic import UUID4

from eap_api.app import create_app
from eap_schemas import AgentSpec, ModelRef, canonical_spec_hash
from .conftest import get_auth_header
from .conftest import create_agent, publish_version




class TestPublishVersion:
    """Tests for POST /api/v1/agents/{id}/versions."""

    def test_publish_v1(self, client, tenant):
        """Publish first version."""
        agent_id = create_agent(client, tenant, "user1")
        response = publish_version(client, agent_id, tenant, "user1")
        assert response["version"] == 1
        assert response["spec_hash"] == canonical_spec_hash(
            AgentSpec.model_validate(response["spec"])
        )

    def test_publish_unchanged_returns_409(self, client, tenant):
        """Publish unchanged draft returns 409."""
        agent_id = create_agent(client, tenant, "user1")
        publish_version(client, agent_id, tenant, "user1")
        
        response = client.post(
            f"/api/v1/agents/{agent_id}/versions",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 409
        assert "no changes since version 1" in response.json()["detail"]

    def test_publish_after_patch_creates_v2(self, client, tenant):
        """Patch then publish creates v2 with new hash."""
        agent_id = create_agent(client, tenant, "user1")
        v1 = publish_version(client, agent_id, tenant, "user1")
        
        # Patch the draft
        spec = AgentSpec(
            name="Updated Name",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        response = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"spec": spec.model_dump(mode="json")},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        
        # Publish again
        v2 = publish_version(client, agent_id, tenant, "user1")
        assert v2["version"] == 2
        assert v2["spec_hash"] != v1["spec_hash"]
        
        # v1's spec should be unchanged
        response = client.get(
            f"/api/v1/agents/{agent_id}/versions/1",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        assert response.json()["spec"]["name"] == "Test Agent"
