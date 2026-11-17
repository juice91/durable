"""Shared test fixtures."""

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from eap_api.app import create_app

# An in-process worker must never schedule the hourly registry mirror (it would reach the real registry).
os.environ.setdefault("EAP_MCP_CATALOG_SCHEDULE", "off")


@pytest.fixture(autouse=True)
def dev_auth_mode(monkeypatch: pytest.MonkeyPatch):
    """API tests explicitly opt in to the local-only authentication stub."""
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")


@pytest.fixture(autouse=True)
def allowed_models(monkeypatch: pytest.MonkeyPatch):
    """Model allowlist for API tests: the scripted test models plus the `openai:gpt-4` fixtures use."""
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,openai:gpt-4")


@pytest.fixture(autouse=True)
def no_provider_discovery(monkeypatch: pytest.MonkeyPatch):
    """Provider keys and URLs never leak into API tests, and no test reaches a real provider: the
    model discovery cache starts empty and its HTTP call fails unless a test installs a stub."""
    from eap_api import model_discovery

    for name in ("EAP_LITELLM_BASE_URL", "OPENAI_BASE_URL", "EAP_LITELLM_API_KEY", "LITELLM_API_KEY",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def refuse(url, headers, timeout_s):
        raise AssertionError("a test reached the network through model discovery")

    monkeypatch.setattr(model_discovery, "http_get", refuse)
    model_discovery.clear_cache()
    yield
    model_discovery.clear_cache()


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


# RAG P1: the API never imports a document parser (uploads are parsed only in the worker's sandbox
# child). Checked once the session ends, so any import an API test triggered counts. Only for a
# session of API tests alone: a mixed run also imports the worker and its parsers in this process.
PARSER_MODULES = (
    "docutils", "pypdf", "pypdfium2", "PIL", "eap_knowledge.sandbox", "eap_knowledge.formats.rst",
    "eap_knowledge.formats.structured", "eap_knowledge.formats.doxygen", "eap_knowledge.formats.pdf",
)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    items = getattr(session, "items", None) or []
    if not items or any("api_tests" not in str(item.path) for item in items):
        return
    loaded = [name for name in PARSER_MODULES if name in sys.modules]
    if loaded:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"API test session imported document parsers: {', '.join(loaded)}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
