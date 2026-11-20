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
        self.info_calls: list[str] = []  # GET /model/info (capabilities) is counted apart from the model lists
        self.bodies = bodies or {}
        self.failures = failures or {}

    def __call__(self, url, headers, timeout_s):
        if "/model/info" in url:
            self.info_calls.append(url)
        else:
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
                     "source": "discovered", "description": None,
                     "capabilities": {"image": True, "pdf": True, "source": "assumed"}}
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


FALLBACK_IDS = ["anthropic:claude-haiku-5-5", "anthropic:claude-opus-5-5", "anthropic:claude-sonnet-5-5"]


def anthropic_page(names, *, has_more=False, last_id=None):
    return {"data": [{"id": n, "type": "model"} for n in names], "has_more": has_more, "first_id": names[0] if names else None,
            "last_id": last_id or (names[-1] if names else None)}


def test_anthropic_lists_what_the_key_can_use_and_nothing_else(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "anthropic:*")
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    stub = install(monkeypatch, Stub({"api.anthropic.com": anthropic_page(["claude-sonnet-5-5", "claude-x"])}))
    items = {i["id"]: i for i in get_models(client, tenant)}
    assert sorted(items) == ["anthropic:claude-sonnet-5-5", "anthropic:claude-x"]  # no invented extras
    assert {i["source"] for i in items.values()} == {"discovered"}
    url, headers = stub.calls[0]
    assert url.startswith("https://api.anthropic.com/v1/models?limit=") and "after_id" not in url
    assert headers == {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}


def test_anthropic_discovery_follows_after_id_pages(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "anthropic:*")
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    pages = {None: anthropic_page(["claude-a", "claude-b"], has_more=True), "claude-b": anthropic_page(["claude-c"])}
    calls = []

    def paged(url, headers, timeout_s):
        calls.append(url)
        return pages["claude-b" if "after_id=claude-b" in url else None]

    install(monkeypatch, paged)
    assert [i["id"] for i in get_models(client, tenant)] == [f"anthropic:claude-{x}" for x in "abc"]
    assert len(calls) == 2 and "after_id=claude-b" in calls[1]


@pytest.mark.parametrize("failure", [model_discovery.HttpStatusError(401), TimeoutError("slow")])
def test_anthropic_falls_back_to_the_small_catalogue_when_listing_fails(client, tenant, monkeypatch, failure):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "anthropic:*")
    install(monkeypatch, Stub())
    assert [i["id"] for i in get_models(client, tenant)] == ["anthropic:*"]  # no key: nothing is invented
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    model_discovery.clear_cache()
    install(monkeypatch, Stub(failures={"api.anthropic.com": failure}))
    items = get_models(client, tenant)
    assert [i["id"] for i in items] == FALLBACK_IDS and {i["source"] for i in items} == {"fallback"}
    status = model_discovery.provider_status("anthropic")
    assert status.status == "unreachable" and status.error  # the provider page still tells the truth


def test_the_listing_is_filtered_by_the_allowlist(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "anthropic:claude-sonnet-5-5,openai:*")
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    install(monkeypatch, Stub({"api.anthropic.com": anthropic_page(["claude-sonnet-5-5", "claude-fable-5"])}))
    assert [i["id"] for i in get_models(client, tenant)] == ["anthropic:claude-sonnet-5-5", "openai:*"]


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
    assert list(items) == ["litellm", "anthropic", "openai", "cloudflare"]
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


# --------------------------------------------------------------------------- capabilities


def info_body(**models):
    """LiteLLM GET /model/info: {model_name: model_info dict}."""
    return {"data": [{"model_name": n, "model_info": info} for n, info in models.items()]}


def test_litellm_model_info_fills_capabilities(client, tenant, litellm_env, monkeypatch):
    stub = install(monkeypatch, Stub({
        "/model/info": info_body(**{"vision-pdf": {"supports_vision": True, "supports_pdf_input": True},
                                    "vision-only": {"supports_vision": True, "supports_pdf_input": False},
                                    "text-only": {"supports_vision": False, "supports_pdf_input": False}}),
        "/models": models_body("vision-pdf", "vision-only", "text-only"),
    }))
    by_name = {i["name"]: i["capabilities"] for i in get_models(client, tenant) if i["provider"] == "litellm"}
    assert by_name["vision-pdf"] == {"image": True, "pdf": True, "source": "gateway"}
    assert by_name["vision-only"] == {"image": True, "pdf": False, "source": "gateway"}
    assert by_name["text-only"] == {"image": False, "pdf": False, "source": "gateway"}
    assert stub.info_calls == ["https://user:pw@gateway.example.test:4000/model/info"]  # once, root of the base
    assert model_discovery.capabilities_for("litellm", "vision-only").pdf is False
    assert len(stub.info_calls) == 1  # capabilities_for is served by the same cache


def test_a_gateway_model_without_the_flags_uses_the_assumed_values_for_them(client, tenant, litellm_env, monkeypatch):
    install(monkeypatch, Stub({"/model/info": info_body(odd={"supports_vision": False, "supports_pdf_input": None}),
                               "/models": models_body("odd")}))
    caps = model_discovery.capabilities_for("litellm", "odd")
    assert (caps.image, caps.pdf, caps.source) == (False, True, "gateway")  # pdf unknown: the old provider rule


def test_static_table_covers_claude_and_gpt4o():
    for name in ("claude-sonnet-4-5", "claude-3-5-haiku", "claude-opus-5-5", "claude-haiku-5-5"):
        assert model_discovery.static_capabilities("anthropic", name) == model_discovery.Capabilities(True, True, "static")
    for name in ("gpt-4o-mini", "gpt-4.1", "o3", "o4-mini"):  # OpenAI: images yes, PDFs only if the gateway says so
        assert model_discovery.static_capabilities("openai", name) == model_discovery.Capabilities(True, False, "static")
    for provider, name in (("anthropic", "claude-2.1"), ("anthropic", "claude-instant-1.2"),
                           ("openai", "o1-mini"), ("openai", "o3-mini")):
        assert model_discovery.static_capabilities(provider, name) is None  # take no files
    assert model_discovery.static_capabilities("openai", "gpt-3.5-turbo") is None
    assert model_discovery.static_capabilities("litellm", "claude-x") is None


def test_duplicate_gateway_rows_are_anded_and_wildcards_fall_to_static(client, tenant, litellm_env, monkeypatch):
    body = {"data": [
        {"model_name": "dup", "model_info": {"supports_vision": True, "supports_pdf_input": True}},
        {"model_name": "dup", "model_info": {"supports_vision": True, "supports_pdf_input": False}},
        {"model_name": "claude-*", "model_info": {"supports_vision": True, "supports_pdf_input": True}},
    ]}
    install(monkeypatch, Stub({"/model/info": body, "/models": models_body("dup", "claude-x")}))
    dup = model_discovery.capabilities_for("litellm", "dup")
    assert (dup.image, dup.pdf, dup.source) == (True, False, "gateway")
    wild = model_discovery.capabilities_for("litellm", "claude-x")  # a wildcard row is not matched by name
    assert wild.source == "assumed"


def test_unknown_model_is_assumed_with_the_old_provider_rules(monkeypatch):
    install(monkeypatch, Stub())
    caps = model_discovery.capabilities_for("litellm", "mystery")
    assert caps == model_discovery.Capabilities(True, True, "assumed")  # not configured: nothing discovered
    assert model_discovery.capabilities_for("test", "ticket") == model_discovery.Capabilities(False, False, "assumed")
    assert model_discovery.capabilities_for("test", "echo") == model_discovery.Capabilities(True, True, "assumed")
    assert model_discovery.capabilities_for("mistral", "x") == model_discovery.Capabilities(True, False, "assumed")


def test_model_info_outage_falls_back_to_assumed(client, tenant, litellm_env, monkeypatch):
    install(monkeypatch, Stub({"/model/info": None, "/models": models_body("a", "b")},
                              failures={"/model/info": ConnectionError("gateway down")}))
    items = get_models(client, tenant)  # still 200, the models are still listed
    caps = [i["capabilities"] for i in items if i["provider"] == "litellm"]
    assert caps == [{"image": True, "pdf": True, "source": "assumed"}] * 2


def test_get_models_items_carry_capabilities(client, tenant, litellm_env, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:ticket,test:echo,litellm:*,anthropic:claude-sonnet-4-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    install(monkeypatch, Stub({"/model/info": info_body(), "/models": models_body("a")}))
    items = {i["id"]: i for i in get_models(client, tenant)}
    assert items["test:ticket"]["capabilities"] == {"image": False, "pdf": False, "source": "assumed"}
    assert items["test:echo"]["capabilities"]["image"] is True  # a scripted item
    assert items["litellm:a"]["capabilities"]["source"] == "assumed"  # the gateway lists no info for it
    assert items["anthropic:claude-sonnet-4-5"]["capabilities"] == {"image": True, "pdf": True, "source": "static"}


# ---- cloudflare (Workers AI) -----------------------------------------------------------------

CF_TOKEN = "cf-token-SECRET-321"
DEEPSEEK = "@cf/deepseek-ai/deepseek-v4.1-flash-1017"


def cf_page(names, *, total=None, props=None):
    return {"result": [{"name": n, "properties": (props or {}).get(n, [])} for n in names],
            "result_info": {"total_count": total if total is not None else len(names)}, "success": True}


@pytest.fixture
def cf_env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", CF_TOKEN)
    monkeypatch.setenv("EAP_CLOUDFLARE_ACCOUNT_ID", "acct123")
    monkeypatch.delenv("EAP_CLOUDFLARE_GATEWAY_ID", raising=False)
    monkeypatch.delenv("EAP_CLOUDFLARE_BASE_URL", raising=False)
    monkeypatch.delenv("EAP_CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:echo,cloudflare:*")


def cf_status(monkeypatch, stub, environ=None):
    install(monkeypatch, stub)
    return model_discovery.provider_status("cloudflare", environ=environ)


def test_cloudflare_unconfigured_without_token_or_account(monkeypatch):
    stub = install(monkeypatch, Stub())
    for env in ({}, {"CLOUDFLARE_API_TOKEN": CF_TOKEN}, {"EAP_CLOUDFLARE_ACCOUNT_ID": "a"}):
        status = model_discovery.provider_status("cloudflare", environ=env, refresh=False)
        model_discovery.clear_cache()
        assert (status.configured, status.status) == (False, "not_configured")
    assert stub.calls == []


def test_cloudflare_lists_catalogue_with_properties_and_static_entry(monkeypatch):
    props = {"@cf/meta/llama-x": [{"property_id": "function_calling", "value": "true"},
                                  {"property_id": "context_window", "value": "131072"}]}
    stub = Stub({"/ai/models/search": cf_page(["@cf/meta/llama-x", DEEPSEEK], props=props)})
    env = {"CLOUDFLARE_API_TOKEN": CF_TOKEN, "EAP_CLOUDFLARE_ACCOUNT_ID": "acct123"}
    model_discovery.clear_cache()
    status = cf_status(monkeypatch, stub, env)
    assert status.status == "ok" and status.env_var == "CLOUDFLARE_API_TOKEN"
    assert status.base_url_host == "api.cloudflare.com"
    by_name = {m.name: m for m in status.models}
    assert by_name["@cf/meta/llama-x"].function_calling is True and by_name["@cf/meta/llama-x"].context_window == 131072
    ds = by_name[DEEPSEEK]
    assert (ds.function_calling, ds.structured_output, ds.context_window) == (True, True, 1_048_576)
    assert ds.capabilities.image is False
    url, headers = stub.calls[0]
    assert url.startswith("https://api.cloudflare.com/client/v4/accounts/acct123/ai/models/search?task=Text%20Generation")
    assert headers == {"Authorization": f"Bearer {CF_TOKEN}"}
    model_discovery.clear_cache()


def test_cloudflare_follows_pages_up_to_five(monkeypatch):
    model_discovery.clear_cache()
    pages = []

    def stub(url, headers, timeout_s):
        pages.append(url)
        n = int(url.rsplit("page=", 1)[1])
        return cf_page([f"@cf/m/p{n}-{i}" for i in range(100)], total=10_000)

    status = cf_status(monkeypatch, stub, {"CLOUDFLARE_API_TOKEN": CF_TOKEN, "EAP_CLOUDFLARE_ACCOUNT_ID": "a"})
    assert len(pages) == 5 and len(status.models) == 500
    model_discovery.clear_cache()


def test_cloudflare_gateway_header_and_token_fallback(monkeypatch):
    model_discovery.clear_cache()
    stub = Stub({"/ai/models/search": cf_page([DEEPSEEK])})
    env = {"EAP_CLOUDFLARE_API_TOKEN": CF_TOKEN, "EAP_CLOUDFLARE_ACCOUNT_ID": "a", "EAP_CLOUDFLARE_GATEWAY_ID": "gw1"}
    status = cf_status(monkeypatch, stub, env)
    assert status.status == "ok" and status.env_var == "EAP_CLOUDFLARE_API_TOKEN"
    assert stub.calls[0][1] == {"Authorization": f"Bearer {CF_TOKEN}", "cf-aig-gateway-id": "gw1"}
    model_discovery.clear_cache()


@pytest.mark.parametrize("failure,fragment", [
    (model_discovery.HttpStatusError(401), "rejected"), (model_discovery.HttpStatusError(500), "HTTP 500"),
    (TimeoutError("x"), "in time"), (OSError("x"), "could not connect")])
def test_cloudflare_failures_are_unreachable_without_leaks(monkeypatch, failure, fragment):
    model_discovery.clear_cache()
    status = cf_status(monkeypatch, Stub(failures={"/ai/models/search": failure}),
                       {"CLOUDFLARE_API_TOKEN": CF_TOKEN, "EAP_CLOUDFLARE_ACCOUNT_ID": "a"})
    assert status.status == "unreachable" and fragment in status.error and CF_TOKEN not in str(status.public())
    model_discovery.clear_cache()


def test_cloudflare_in_providers_endpoint_and_models_expansion(client, tenant, cf_env, monkeypatch):
    model_discovery.clear_cache()
    install(monkeypatch, Stub({"/ai/models/search": cf_page([DEEPSEEK])}))
    ids = [i["id"] for i in get_models(client, tenant)]
    assert f"cloudflare:{DEEPSEEK}" in ids
    rows = client.get("/api/v1/providers", headers=get_auth_header(tenant, "alice")).json()["items"]
    row = next(r for r in rows if r["id"] == "cloudflare")
    assert row["label"] == "Cloudflare Workers AI" and row["status"] == "ok" and CF_TOKEN not in json.dumps(rows)
    model_discovery.clear_cache()
