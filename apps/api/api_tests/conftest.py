"""Shared test fixtures."""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from eap_api.app import create_app


def get_auth_header(tenant: str, user: str) -> dict[str, str]:
    """Generate an Authorization header for testing."""
    return {"Authorization": f"Bearer dev:{tenant}:{user}"}


@pytest.fixture
def client():
    """Create a test client with a temporary SQLite database or Postgres if EAP_TEST_DATABASE_URL is set."""
    db_url = os.environ.get("EAP_TEST_DATABASE_URL", f"sqlite:////tmp/eap-test-{uuid.uuid4().hex}.db")
    app = create_app(database_url=db_url)
    with TestClient(app) as test_client:
        yield test_client
    
    # Clean up SQLite files if using SQLite
    if db_url.startswith("sqlite:///") and "://" not in db_url.replace("sqlite://", ""):
        if os.path.exists(db_url.replace("sqlite://", "")):
            os.unlink(db_url.replace("sqlite://", ""))


@pytest.fixture
def auth_header():
    """Generate an Authorization header with unique tenant ID."""
    tenant = f"t{uuid.uuid4().hex[:10]}"
    user = "alice"
    return {"Authorization": f"Bearer dev:{tenant}:{user}"}

@pytest.fixture
def tenant():
    """Generate a unique tenant ID for each test."""
    return f"t{uuid.uuid4().hex[:10]}"

@pytest.fixture
def user():
    """Generate a unique user ID for each test."""
    return "alice"

__all__ = ["get_auth_header", "create_agent", "publish_version"]


def create_agent(client, tenant: str, user_id: str) -> str:
    """Helper to create an agent and return its ID."""
    from eap_schemas import AgentSpec, ModelRef
    spec = AgentSpec(
        name="Test Agent",
        model=ModelRef(provider="openai", name="gpt-4"),
    )
    response = client.post(
        "/api/v1/agents",
        json={"spec": spec.model_dump(mode="json")},
        headers=get_auth_header(tenant, user_id),
    )
    assert response.status_code == 201
    return response.json()["id"]


def publish_version(client, agent_id: str, tenant: str, user_id: str) -> dict:
    """Helper to publish a version."""
    response = client.post(
        f"/api/v1/agents/{agent_id}/versions",
        headers=get_auth_header(tenant, user_id),
    )
    assert response.status_code == 201
    return response.json()
