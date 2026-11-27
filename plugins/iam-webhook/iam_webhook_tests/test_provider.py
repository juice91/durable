"""The webhook identity provider with a fake store and fake secrets: no database."""

import asyncio
import time

import pytest
from eap_inbound.signing import github_headers, standard_webhooks_headers
from eap_iam import ACTION_INGEST_WEBHOOK, AuthError, IdentityChain, RequestView

import eap_iam_webhook.provider as provider_module
from eap_iam_webhook import WebhookSignatureProvider

TRIGGER = "0f3c2a5e-1111-4222-8333-444455556666"
SECRET = "plug-in-sentinel-secret"
BODY = b'{"a":1}'


class FakeStore:
    def __init__(self, **row):
        self.row = {
            "id": TRIGGER, "tenant_id": "t1", "status": "enabled", "config_hash": "h", "approved_config_hash": "h",
            "preset": "github", "preset_config": {}, "tolerance_seconds": 300, "enabled_by": "user:alice",
            "secrets": [{"ref": "secret:hook"}], **row,
        }
        self.recorded, self.touched = [], []

    def get_trigger_by_id(self, trigger_id):
        return self.row if trigger_id == TRIGGER else None

    def record_delivery(self, *args, **kwargs):
        self.recorded.append((args, kwargs))

    def touch_secret_matched(self, *args):
        self.touched.append(args)


class FakeSecrets:
    values = {("t1", "hook"): SECRET}

    def __init__(self, engine, ring):
        pass

    def resolve(self, tenant, name):
        return self.values.get((tenant, name))


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(provider_module, "SecretStore", FakeSecrets)
    monkeypatch.setattr(provider_module, "load_keyring", lambda env: object())


def view(headers, *, trigger=TRIGGER, body=BODY, accepts=frozenset({"webhook"}), action=ACTION_INGEST_WEBHOOK):
    return RequestView(headers=headers, accepts=accepts, action=action, route_params={"trigger_id": trigger}, body=body)


def authenticate(store, request, env=None):
    return asyncio.run(WebhookSignatureProvider(store, None, {"EAP_WEBHOOKS_ENABLED": "true"} if env is None else env).authenticate(request))


def test_a_valid_signature_is_a_webhook_principal_carrying_what_was_proven():
    store = FakeStore()
    principal = authenticate(store, view(github_headers(SECRET, BODY)))
    assert (principal.kind, principal.subject, principal.tenant_id, principal.on_behalf_of) == ("webhook", TRIGGER, "t1", None)
    assert principal.extra["dedupe_key"].startswith("body:") and principal.extra["config_hash"] == "h"
    assert store.touched == [("t1", TRIGGER, 0)]


@pytest.mark.parametrize("row,status", [({"status": "disabled"}, 404), ({"status": "pending_enable"}, 404), ({"approved_config_hash": "old"}, 404),
                                       ({"status": "archived"}, 404)])
def test_an_unusable_trigger_is_a_404_with_a_reason_for_counters_only(row, status):
    with pytest.raises(AuthError) as caught:
        authenticate(FakeStore(**row), view(github_headers(SECRET, BODY)))
    assert caught.value.status == status and caught.value.detail == "not found" and caught.value.reason == "not_enabled"


def test_unknown_and_malformed_ids_are_the_same_404():
    for trigger in (str(__import__("uuid").uuid4()), "not-a-uuid", "../agents"):
        with pytest.raises(AuthError) as caught:
            authenticate(FakeStore(), view(github_headers(SECRET, BODY), trigger=trigger))
        assert (caught.value.status, caught.value.detail) == (404, "not found")


def test_every_signature_failure_is_one_401_with_distinct_counter_reasons():
    reasons = {}
    for name, headers in {"mismatch": github_headers("wrong", BODY), "missing": {}, "malformed": {"x-hub-signature-256": "sha256=zz"}}.items():
        with pytest.raises(AuthError) as caught:
            authenticate(FakeStore(), view(headers))
        assert (caught.value.status, caught.value.detail) == (401, "invalid signature")
        reasons[name] = caught.value.reason
    assert reasons == {"mismatch": "signature_mismatch", "missing": "missing_header", "malformed": "malformed_signature"}


def test_a_missing_secret_is_a_401_secret_missing():
    FakeSecrets.values = {}
    try:
        with pytest.raises(AuthError) as caught:
            authenticate(FakeStore(), view(github_headers(SECRET, BODY)))
        assert (caught.value.status, caught.value.reason) == (401, "secret_missing")
    finally:
        FakeSecrets.values = {("t1", "hook"): SECRET}


def test_a_stale_delivery_with_a_valid_signature_is_recorded_and_refused():
    import base64

    whsec = "whsec_" + base64.b64encode(b"k" * 32).decode()
    FakeSecrets.values = {("t1", "hook"): whsec}
    store = FakeStore(preset="standard_webhooks")
    try:
        headers = standard_webhooks_headers(whsec, BODY, msg_id="m1", timestamp=int(time.time()) - 5000)
        with pytest.raises(AuthError) as caught:
            authenticate(store, view(headers))
        assert (caught.value.status, caught.value.reason) == (401, "timestamp_stale")
        (args, kwargs), = store.recorded
        assert args[2] == "stale" and kwargs["timestamp_skew_seconds"] >= 5000 and "body" not in kwargs
    finally:
        FakeSecrets.values = {("t1", "hook"): SECRET}


def test_the_feature_is_off_unless_set_to_true():
    with pytest.raises(AuthError) as caught:
        authenticate(FakeStore(), view(github_headers(SECRET, BODY)), {})
    assert (caught.value.status, caught.value.reason) == (404, "feature_off")


def test_the_provider_answers_only_the_hook_action_and_the_webhook_kind():
    provider = WebhookSignatureProvider(FakeStore(), None, {"EAP_WEBHOOKS_ENABLED": "true"})
    assert provider.kinds == frozenset({"webhook"})
    assert asyncio.run(provider.authenticate(view(github_headers(SECRET, BODY), action="run.agui"))) is None
    assert asyncio.run(provider.authenticate(RequestView(headers=github_headers(SECRET, BODY), accepts=frozenset({"webhook"}), action=ACTION_INGEST_WEBHOOK))) is None
    # in a chain, a route that does not accept `webhook` never consults it
    chain = IdentityChain([provider])
    with pytest.raises(AuthError):
        asyncio.run(chain.authenticate(view(github_headers(SECRET, BODY), accepts=frozenset({"user", "api_key"}))))
