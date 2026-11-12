"""Shared test fixtures."""

import uuid

import pytest
from fastapi.testclient import TestClient

from eap_api.app import create_app


@pytest.fixture(autouse=True)
def dev_auth_mode(monkeypatch: pytest.MonkeyPatch):
    """API tests explicitly opt in to the local-only authentication stub."""
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")


@pytest.fixture(autouse=True)
def allowed_models(monkeypatch: pytest.MonkeyPatch):
    """Model allowlist for API tests: the scripted test models plus the `openai:gpt-4` fixtures use."""
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,openai:gpt-4")


def get_auth_header(tenant: str, user: str) -> dict[str, str]:
    """Generate an Authorization header for testing."""
    return {"Authorization": f"Bearer dev:{tenant}:{user}"}


@pytest.fixture
def client():
    """A test client on Postgres: the eap_db plugin's freshly migrated session schema."""
    from .run_helpers import PG_URL

    if not (PG_URL or "").startswith("postgresql"):
        pytest.skip("the API needs Postgres: start the dev database (bash scripts/dev-db.sh up)")
    app = create_app(database_url=PG_URL)
    with TestClient(app) as test_client:
        yield test_client
    app.state.database_engine.dispose()


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
