"""The `cloudflare` provider: client construction (no network), settings and the Architect allowlist."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from eap_schemas.architect_seed import pick_seed_model, rank_model_ids
from eap_schemas.models_policy import ModelPolicy
from eap_worker.architect_allowlist import models_from_settings
from eap_worker.config import WorkerSettings
from eap_worker.models import cloudflare_model, make_resolver

MODEL = "@cf/deepseek-ai/deepseek-v4.1-flash-1017"


def settings(**kw) -> WorkerSettings:
    base = WorkerSettings.from_env()
    return dataclasses.replace(base, **kw)


def client_of(model):
    return model._provider.client  # type: ignore[attr-defined]


def test_client_without_gateway_header():
    model = cloudflare_model(MODEL, settings(cloudflare_api_token="tok", cloudflare_account_id="acct"))
    client = client_of(model)
    assert model.model_name == MODEL
    assert str(client.base_url) == "https://api.cloudflare.com/client/v4/accounts/acct/ai/v1/"
    assert client.api_key == "tok" and "cf-aig-gateway-id" not in client.default_headers


def test_client_with_gateway_header_and_base_override():
    model = cloudflare_model(MODEL, settings(cloudflare_api_token="tok", cloudflare_account_id="acct",
                                             cloudflare_gateway_id="gw1", cloudflare_base_url="https://example.test/v1"))
    client = client_of(model)
    assert client.default_headers["cf-aig-gateway-id"] == "gw1"
    assert str(client.base_url) == "https://example.test/v1/"


def test_missing_token_or_account_raises():
    with pytest.raises(RuntimeError, match="CLOUDFLARE_API_TOKEN"):
        cloudflare_model(MODEL, settings(cloudflare_api_token=None, cloudflare_account_id="a"))
    with pytest.raises(RuntimeError, match="EAP_CLOUDFLARE_ACCOUNT_ID"):
        cloudflare_model(MODEL, settings(cloudflare_api_token="t", cloudflare_account_id=None, cloudflare_base_url=None))


def test_settings_read_env_with_token_fallback(monkeypatch):
    for k in ("CLOUDFLARE_API_TOKEN", "EAP_CLOUDFLARE_API_TOKEN", "EAP_CLOUDFLARE_GATEWAY_ID", "EAP_CLOUDFLARE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EAP_CLOUDFLARE_API_TOKEN", "fallback")
    monkeypatch.setenv("EAP_CLOUDFLARE_ACCOUNT_ID", " acct ")
    monkeypatch.setenv("EAP_CLOUDFLARE_GATEWAY_ID", "")
    s = WorkerSettings.from_env()
    assert (s.cloudflare_api_token, s.cloudflare_account_id, s.cloudflare_gateway_id) == ("fallback", "acct", None)
    assert "fallback" not in repr(s)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "primary")
    assert WorkerSettings.from_env().cloudflare_api_token == "primary"


def test_resolver_builds_cloudflare_models():
    s = settings(cloudflare_api_token="tok", cloudflare_account_id="acct", allowed_models=ModelPolicy.parse("cloudflare:*"))
    resolve = make_resolver(lambda: s)
    ctx = SimpleNamespace(deps=None)
    assert resolve(ctx, f"cloudflare:{MODEL}").model_name == MODEL


def test_allowlist_offers_cloudflare_only_when_configured():
    calls = []

    def lister(token, account, gateway):
        calls.append((token, account, gateway))
        return [MODEL, "@cf/meta/other"]

    policy = ModelPolicy.parse("cloudflare:*")
    on = settings(allowed_models=policy, cloudflare_api_token="t", cloudflare_account_id="a", cloudflare_gateway_id="g")
    assert [m.id for m in models_from_settings(on, environ={}, cloudflare=lister)] == [
        f"cloudflare:{MODEL}", "cloudflare:@cf/meta/other"]
    assert calls == [("t", "a", "g")]
    off = settings(allowed_models=policy, cloudflare_api_token="t", cloudflare_account_id=None)
    assert models_from_settings(off, environ={}, cloudflare=lister) == ()
    assert len(calls) == 1


def test_seed_ranking_cloudflare_behind_direct_providers_never_default_over_them():
    ranked = rank_model_ids([f"cloudflare:{MODEL}", "mystery:thing", "openai:gpt-5", "anthropic:claude-sonnet-5-5"])
    assert ranked == ["anthropic:claude-sonnet-5-5", "openai:gpt-5", f"cloudflare:{MODEL}", "mystery:thing"]
    assert pick_seed_model([("cloudflare", [MODEL]), ("anthropic", ["claude-sonnet-5-5"])], []) == "anthropic:claude-sonnet-5-5"
