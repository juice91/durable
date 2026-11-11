"""Tests for Task 4 bug fixes."""

import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_schemas import AgentSpec, ModelRef
from .conftest import get_auth_header
from .conftest import create_agent, publish_version


class TestPatchWithInvalidSpec:
    """Tests for PATCH with invalid spec returning 422."""

    def test_patch_agent_invalid_spec_returns_422(self, client, tenant):
        """PATCH with invalid spec returns 422, not 500."""
        # First create a valid agent
        spec = AgentSpec(
            name="Test Agent",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        response = client.post(
            "/api/v1/agents",
            json={"spec": spec.model_dump(mode="json")},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 201
        agent_id = response.json()["id"]

        # Now try to PATCH with invalid spec (uppercase tool id)
        response = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"spec": {"name": "Updated", "model": {"provider": "openai", "name": "gpt-4"}, "tools": ["Bad"]}},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 422
        # Response should have a list detail
        data = response.json()
        assert isinstance(data["detail"], list)
        assert len(data["detail"]) > 0


class TestPostWithInvalidSpec:
    """Tests for POST with invalid spec returning 422."""

    def test_post_agent_invalid_spec_returns_422(self, client, tenant):
        """POST with invalid spec returns 422."""
        response = client.post(
            "/api/v1/agents",
            json={"spec": {"name": "Test", "model": {"provider": "openai", "name": "gpt-4"}, "tools": ["Bad"]}},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 422
        data = response.json()
        assert isinstance(data["detail"], list)
        assert len(data["detail"]) > 0


class TestConcurrentPublish:
    """Tests for concurrent publish returning 409."""

    def test_concurrent_publish_returns_409(self, client, tenant):
        """Concurrent publishes don't cause 500 - return 409."""
        # Create an agent
        spec = AgentSpec(
            name="Test Agent",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        response = client.post(
            "/api/v1/agents",
            json={"spec": spec.model_dump(mode="json")},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 201
        agent_id = response.json()["id"]

        # Patch the agent to create a new draft
        response = client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"spec": {"name": "Updated", "model": {"provider": "openai", "name": "gpt-4"}}},
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200

        # Use a barrier to synchronize two concurrent publishes
        barrier = threading.Barrier(2)
        results = []

        def publish_version():
            barrier.wait()  # Wait for both threads to be ready
            response = client.post(
                f"/api/v1/agents/{agent_id}/versions",
                headers=get_auth_header(tenant, "user1"),
            )
            results.append(response.status_code)

        # Start two threads that will try to publish simultaneously
        t1 = threading.Thread(target=publish_version)
        t2 = threading.Thread(target=publish_version)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # One should succeed (201), the other should get 409 (conflict)
        assert 201 in results
        assert 409 in results

        # Verify only one version was created
        response = client.get(
            f"/api/v1/agents/{agent_id}/versions",
            headers=get_auth_header(tenant, "user1"),
        )
        assert response.status_code == 200
        versions = response.json()
        assert len(versions) == 1
