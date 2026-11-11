"""Model governance at publish: only models on the EAP_ALLOWED_MODELS allowlist can be published."""

import pytest

from api_tests.conftest import get_auth_header


def _agent_with_model(client, tenant: str, provider: str, name: str) -> str:
    response = client.post(
        "/api/v1/agents",
        json={"spec": {"name": "Policy agent", "model": {"provider": provider, "name": name}}},
        headers=get_auth_header(tenant, "alice"),
    )
    assert response.status_code == 201  # drafts may name any model; publishing is the gate
    return response.json()["id"]


def _publish(client, tenant: str, agent_id: str):
    return client.post(f"/api/v1/agents/{agent_id}/versions", headers=get_auth_header(tenant, "alice"))


def test_publish_disallowed_model_is_422_naming_the_model(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,litellm:qwen3.8-thinking")
    agent_id = _agent_with_model(client, tenant, "litellm", "gpt-5-expensive")
    response = _publish(client, tenant, agent_id)
    assert response.status_code == 422
    assert "litellm:gpt-5-expensive" in response.json()["detail"]
    assert "not allowed" in response.json()["detail"]
    versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=get_auth_header(tenant, "alice"))
    assert versions.json() == []


def test_publish_allowed_exact_model_is_201(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,litellm:qwen3.8-thinking")
    agent_id = _agent_with_model(client, tenant, "litellm", "qwen3.8-thinking")
    response = _publish(client, tenant, agent_id)
    assert response.status_code == 201
    assert response.json()["spec"]["model"] == {"provider": "litellm", "name": "qwen3.8-thinking"}


def test_publish_allowed_wildcard_model_is_201(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "litellm:*")
    agent_id = _agent_with_model(client, tenant, "litellm", "any-model-name")
    assert _publish(client, tenant, agent_id).status_code == 201


@pytest.mark.parametrize("unset_value", [None, ""])
def test_default_policy_allows_only_test_models(client, tenant, monkeypatch, unset_value):
    if unset_value is None:
        monkeypatch.delenv("EAP_ALLOWED_MODELS", raising=False)
    else:
        monkeypatch.setenv("EAP_ALLOWED_MODELS", unset_value)
    test_agent = _agent_with_model(client, tenant, "test", "ticket")
    assert _publish(client, tenant, test_agent).status_code == 201
    real_agent = _agent_with_model(client, tenant, "openai", "gpt-4")
    response = _publish(client, tenant, real_agent)
    assert response.status_code == 422
    assert "openai:gpt-4" in response.json()["detail"]
