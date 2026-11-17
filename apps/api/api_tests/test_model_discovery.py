"""Model discovery: GET /models expands wildcards, GET /providers and POST /providers/{id}/test.

A stub replaces the HTTP call (eap_api.model_discovery.http_get): no real network, no real keys.
"""

from __future__ import annotations

import json

import pytest

from api_tests.conftest import get_auth_header
from eap_api import model_discovery

LITELLM_KEY = "sk-litellm-SECRET-123"
ANTHROPIC_KEY = "sk-ant-SECRET-456"
OPENAI_KEY = "sk-openai-SECRET-789"


class Stub:
    def __init__(self, bodies=None, failures=None):
        self.calls: list[tuple[str, dict]] = []
        self.bodies = bodies or {}
        self.failures = failures or {}

    def __call__(self, url, headers, timeout_s):
        self.calls.append((url, dict(headers)))
        assert timeout_s <= 10
        for fragment, failure in self.failures.items():
            if fragment in url:
                raise failure
        for fragment, body in self.bodies.items():
            if fragment in url:
                return body
        raise AssertionError(f"unexpected url {url}")


def models_body(*names):
    return {"object": "list", "data": [{"id": n} for n in names]}


@pytest.fixture
def litellm_env(monkeypatch):
    monkeypatch.setenv("EAP_LITELLM_BASE_URL", "https://user:pw@gateway.example.test:4000/v1")
    monkeypatch.setenv("EAP_LITELLM_API_KEY", LITELLM_KEY)
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:echo,litellm:*")


def install(monkeypatch, stub):
    monkeypatch.setattr(model_discovery, "http_get", stub)
    return stub


def get_models(client, tenant):
    response = client.get("/api/v1/models", headers=get_auth_header(tenant, "alice"))
    assert response.status_code == 200
    return response.json()["items"]


def test_wildcard_is_expanded_with_the_discovered_models(client, tenant, litellm_env, monkeypatch):
    stub = install(monkeypatch, Stub({"/models": models_body("qwen3.8", "gpt-x", "qwen3.8")}))
    items = get_models(client, tenant)
    assert [i["id"] for i in items] == ["test:echo", "litellm:gpt-x", "litellm:qwen3.8"]
    found = items[1]
    assert found == {"id": "litellm:gpt-x", "provider": "litellm", "name": "gpt-x", "wildcard": False,
                     "source": "discovered", "description": None}
    assert items[0]["source"] == "allowlist"
    url, headers = stub.calls[0]
    assert url == "https://user:pw@gateway.example.test:4000/v1/models"  # a base ending in /v1 is not doubled
    assert headers["Authorization"] == f"Bearer {LITELLM_KEY}"


def test_base_without_v1_gets_v1_models(client, tenant, litellm_env, monkeypatch):
    monkeypatch.setenv("EAP_LITELLM_BASE_URL", "https://gateway.example.test")
    stub = install(monkeypatch, Stub({"/models": models_body("a")}))
    get_models(client, tenant)
    assert stub.calls[0][0] == "https://gateway.example.test/v1/models"


def test_discovery_is_cached_and_the_cache_is_shared_by_providers_endpoint(client, tenant, litellm_env, monkeypatch):
    stub = install(monkeypatch, Stub({"/models": models_body("a", "b")}))
    get_models(client, tenant)
    get_models(client, tenant)
    client.get("/api/v1/providers", headers=get_auth_header(tenant, "alice"))
    assert len(stub.calls) == 1  # litellm once; anthropic and openai are not configured
    monkeypatch.setattr(model_discovery, "CACHE_TTL_S", 0.0)  # expired
    get_models(client, tenant)
    assert len(stub.calls) == 2


def test_allowlist_filtering_applies_to_discovered_names(client, tenant, litellm_env, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "litellm:*,openai:gpt-4")
    install(monkeypatch, Stub({"/models": models_body("a", "b")}))
    items = get_models(client, tenant)
    assert [i["id"] for i in items] == ["litellm:a", "litellm:b", "openai:gpt-4"]  # a plain entry is untouched


def test_an_explicit_entry_is_not_duplicated_by_discovery(client, tenant, litellm_env, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "litellm:a,litellm:*")
    install(monkeypatch, Stub({"/models": models_body("a", "b")}))
    items = get_models(client, tenant)
    assert [(i["id"], i["source"]) for i in items] == [("litellm:a", "allowlist"), ("litellm:b", "discovered")]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (model_discovery.HttpStatusError(401), "rejected"),
        (model_discovery.HttpStatusError(500), "HTTP 500"),
        (TimeoutError("took too long https://user:pw@gateway.example.test"), "in time"),
        (ConnectionError("refused https://user:pw@gateway.example.test"), "could not connect"),
    ],
)
def test_discovery_failure_keeps_the_wildcard_and_never_fails_the_response(
    client, tenant, litellm_env, monkeypatch, failure, message
):
    install(monkeypatch, Stub(failures={"/models": failure}))
    items = get_models(client, tenant)
    wildcard = next(i for i in items if i["id"] == "litellm:*")
    assert wildcard["wildcard"] is True and wildcard["source"] == "allowlist"
    assert message in wildcard["discovery_error"]
    assert "pw" not in json.dumps(items) and LITELLM_KEY not in json.dumps(items)


def test_unexpected_body_is_a_discovery_error(client, tenant, litellm_env, monkeypatch):
    install(monkeypatch, Stub({"/models": {"nope": 1}}))
    assert "unexpected" in next(i for i in get_models(client, tenant) if i["wildcard"])["discovery_error"]


def test_unconfigured_provider_keeps_the_wildcard_with_an_error(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "litellm:*")  # no litellm env at all
    items = get_models(client, tenant)
    assert [(i["id"], i["wildcard"]) for i in items] == [("litellm:*", True)]
    assert items[0]["discovery_error"]


def test_anthropic_and_openai_need_their_key(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "anthropic:*,openai:*")
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    stub = install(monkeypatch, Stub({"api.anthropic.com": models_body("claude-x")}))
    items = get_models(client, tenant)
    assert [i["id"] for i in items] == ["anthropic:claude-x", "openai:*"]
    assert [c[0].split("?")[0] for c in stub.calls] == ["https://api.anthropic.com/v1/models"]  # openai never called
    assert stub.calls[0][1]["x-api-key"] == ANTHROPIC_KEY


def test_models_requires_auth(client):
    assert client.get("/api/v1/models").status_code == 401
    assert client.get("/api/v1/providers").status_code == 401
    assert client.post("/api/v1/providers/litellm/test").status_code == 401


def test_providers_shape_and_no_key_leak(client, tenant, litellm_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    install(monkeypatch, Stub(
        {"gateway.example.test": models_body("a", "b"), "api.openai.com": models_body("gpt-x")},
        {"api.anthropic.com": model_discovery.HttpStatusError(401)},
    ))
    h = get_auth_header(tenant, "alice")
    listed = client.get("/api/v1/providers", headers=h)
    tested = client.post("/api/v1/providers/litellm/test", headers=h)
    items = {i["id"]: i for i in listed.json()["items"]}
    assert list(items) == ["litellm", "anthropic", "openai"]
    assert items["litellm"] == {
        "id": "litellm", "label": "LiteLLM gateway", "configured": True, "env_var": "EAP_LITELLM_API_KEY",
        "base_url_host": "gateway.example.test:4000", "status": "ok", "model_count": 2,
        "checked_at": items["litellm"]["checked_at"], "error": None,
    }
    assert items["litellm"]["checked_at"]
    assert items["anthropic"]["status"] == "unreachable" and "rejected" in items["anthropic"]["error"]
    assert items["openai"]["model_count"] == 1 and items["openai"]["base_url_host"] == "api.openai.com"
    assert tested.json()["id"] == "litellm" and tested.json()["status"] == "ok"
    everything = listed.text + tested.text + client.get("/api/v1/models", headers=h).text
    for secret in (LITELLM_KEY, ANTHROPIC_KEY, OPENAI_KEY, "SECRET", "user:pw", "pw@"):
        assert secret not in everything


def test_unconfigured_provider_status(client, tenant):
    items = {i["id"]: i for i in client.get("/api/v1/providers", headers=get_auth_header(tenant, "alice")).json()["items"]}
    assert items["anthropic"]["status"] == "not_configured" and items["anthropic"]["configured"] is False
    assert items["anthropic"]["env_var"] == "ANTHROPIC_API_KEY" and items["anthropic"]["model_count"] == 0


def test_test_connection_waits_out_the_cooldown_before_refreshing(client, tenant, litellm_env, monkeypatch):
    stub = install(monkeypatch, Stub({"/models": models_body("a")}))
    h = get_auth_header(tenant, "alice")
    client.get("/api/v1/providers", headers=h)
    # An immediate test reuses the fresh result instead of calling the provider again.
    assert client.post("/api/v1/providers/litellm/test", headers=h).json()["model_count"] == 1
    assert len(stub.calls) == 1
    stub.bodies = {"/models": models_body("a", "b", "c")}
    assert client.get("/api/v1/providers", headers=h).json()["items"][0]["model_count"] == 1  # still cached
    monkeypatch.setattr(model_discovery, "REFRESH_COOLDOWN_S", 0.0)
    assert client.post("/api/v1/providers/litellm/test", headers=h).json()["model_count"] == 3
    assert len(stub.calls) == 2
    assert client.get("/api/v1/providers", headers=h).json()["items"][0]["model_count"] == 3  # the test refreshed it


def test_a_failed_check_expires_sooner_than_a_successful_one(client, tenant, litellm_env, monkeypatch):
    stub = install(monkeypatch, Stub(failures={"/models": ConnectionError("refused")}))
    h = get_auth_header(tenant, "alice")
    first = client.get("/api/v1/providers", headers=h).json()["items"][0]
    assert first["status"] == "unreachable"
    client.get("/api/v1/providers", headers=h)
    assert len(stub.calls) == 1  # the failure is cached
    monkeypatch.setattr(model_discovery, "FAILURE_TTL_S", 0.0)
    stub.failures = {}
    stub.bodies = {"/models": models_body("a")}
    again = client.get("/api/v1/providers", headers=h).json()["items"][0]
    assert again["status"] == "ok" and again["model_count"] == 1
    assert len(stub.calls) == 2


def test_a_missing_key_is_not_cached_for_the_full_success_interval(client, tenant, monkeypatch):
    h = get_auth_header(tenant, "alice")
    assert client.get("/api/v1/providers", headers=h).json()["items"][1]["status"] == "not_configured"
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    stub = install(monkeypatch, Stub({"api.anthropic.com": models_body("claude-x")}))
    assert client.get("/api/v1/providers", headers=h).json()["items"][1]["status"] == "not_configured"
    assert stub.calls == []
    monkeypatch.setattr(model_discovery, "FAILURE_TTL_S", 0.0)
    item = client.get("/api/v1/providers", headers=h).json()["items"][1]
    assert item["status"] == "ok" and item["model_count"] == 1
    assert len(stub.calls) == 1


def test_test_unknown_provider_is_404(client, tenant):
    assert client.post("/api/v1/providers/nope/test", headers=get_auth_header(tenant, "alice")).status_code == 404
