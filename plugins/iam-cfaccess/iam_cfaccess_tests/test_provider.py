"""The Cloudflare Access JWT provider: every refusal of the design (1.3), the JWKS cache and its rate limit."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from eap_iam import AuthError, IamConfigError, PluginContext, RequestView
from eap_iam_cfaccess import JwksCache, create
from eap_iam_cfaccess.jwks import JwksUnavailable

TEAM, AUD, TENANT = "team.cloudflareaccess.com", "aud-1", "studio"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ENV = {"EAP_CF_ACCESS_TEAM_DOMAIN": TEAM, "EAP_CF_ACCESS_AUD": AUD, "EAP_CF_ACCESS_TENANT": TENANT}


def jwk(key, kid):
    return {**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": kid, "alg": "RS256", "use": "sig"}


class Fetcher:
    def __init__(self, *keys):
        self.keys = list(keys)
        self.calls = 0
        self.fail = False

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise JwksUnavailable("down")
        return {"keys": [jwk(k, kid) for kid, k in self.keys]}


def provider(fetcher=None, **env):
    fetcher = fetcher or Fetcher(("k1", KEY))
    ctx = PluginContext(env={**ENV, **env})
    return create(ctx, fetcher=fetcher)


def token(key=KEY, kid="k1", alg="RS256", **claims):
    body = {"iss": f"https://{TEAM}", "aud": [AUD], "exp": int(time.time()) + 300, "iat": int(time.time()), "email": "User@Example.com"}
    body.update(claims)
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm=alg, headers={"kid": kid} if kid else {})


def authenticate(p, **headers):
    view = RequestView(headers=headers, peer_host="10.42.0.9", accepts=frozenset({"user"}))
    return asyncio.run(p.authenticate(view))


def test_a_valid_token_authenticates_with_the_lowercased_email_as_subject():
    principal = authenticate(provider(), **{"cf-access-jwt-assertion": token()})
    assert (principal.kind, principal.subject, principal.tenant_id, principal.provider) == ("user", "user@example.com", TENANT, "cf-access")


def test_no_header_is_not_this_providers_credential():
    assert authenticate(provider()) is None


def test_a_plain_email_header_alone_is_never_trusted():
    assert authenticate(provider(), **{"cf-access-authenticated-user-email": "user@example.com"}) is None
    # and beside a bad token it changes nothing
    with pytest.raises(AuthError):
        authenticate(provider(), **{"cf-access-jwt-assertion": "x.y.z", "cf-access-authenticated-user-email": "user@example.com"})


def _forged(alg: str, claims: dict) -> str:
    """A token that claims `alg` in its header, signed with a random throwaway key (HS256) or not at all (none).

    Built by hand so the negative tests carry no literal `alg=none` encode call or hard-coded signing secret."""

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing_input = b64(json.dumps({"alg": alg, "typ": "JWT", "kid": "k1"}).encode()) + "." + b64(json.dumps(claims).encode())
    signature = hmac.new(os.urandom(32), signing_input.encode(), hashlib.sha256).digest() if alg == "HS256" else b""
    return signing_input + "." + b64(signature)


@pytest.mark.parametrize(
    "make",
    [
        lambda: token(key=OTHER),  # wrong key (right kid)
        lambda: token(aud=["other-app"]),
        lambda: token(iss="https://evil.example"),
        lambda: token(exp=int(time.time()) - 600),
        lambda: token(nbf=int(time.time()) + 600),
        lambda: token(email=None),
        lambda: token(email="not-an-email"),
        lambda: token(email=None, common_name="service-token-id"),  # a service token is not a person
        lambda: token(iat=int(time.time()) - 10 * 24 * 3600),
        lambda: token(kid="unknown"),
        lambda: token(kid=None),
        lambda: _forged("none", {"iss": f"https://{TEAM}", "aud": [AUD], "exp": int(time.time()) + 60, "email": "a@b.co"}),
        lambda: _forged("HS256", {"iss": f"https://{TEAM}", "aud": [AUD], "exp": int(time.time()) + 60, "email": "a@b.co"}),
        lambda: "not-a-jwt",
        lambda: "",
        lambda: "a" * 9000,
    ],
)
def test_every_invalid_token_is_a_401(make):
    with pytest.raises(AuthError) as info:
        authenticate(provider(), **{"cf-access-jwt-assertion": make()})
    assert info.value.status == 401


def test_a_list_of_audiences_overlaps_a_rotation():
    p = provider(EAP_CF_ACCESS_AUD="old-aud, aud-1")
    assert authenticate(p, **{"cf-access-jwt-assertion": token(aud=["old-aud"])}).subject == "user@example.com"
    assert authenticate(p, **{"cf-access-jwt-assertion": token(aud=["aud-1"])}).subject == "user@example.com"


@pytest.mark.parametrize("drop", ["EAP_CF_ACCESS_TEAM_DOMAIN", "EAP_CF_ACCESS_AUD", "EAP_CF_ACCESS_TENANT"])
def test_a_half_configured_plugin_refuses_to_start(drop):
    env = {k: v for k, v in ENV.items() if k != drop}
    with pytest.raises(IamConfigError, match=drop):
        create(PluginContext(env=env))


def test_the_token_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    good = token()
    authenticate(provider(), **{"cf-access-jwt-assertion": good})
    with pytest.raises(AuthError):
        authenticate(provider(), **{"cf-access-jwt-assertion": token(key=OTHER)})
    assert good not in caplog.text and "User@Example.com" not in caplog.text


# --- the JWKS cache ---------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def cache(fetcher, clock, **kw):
    return JwksCache(fetcher, clock=clock, **{"ttl": 100, "min_refresh": 30, "grace": 500, **kw})


def key_for(c, kid):
    return asyncio.run(c.key_for(kid))


def test_the_cache_serves_from_memory_and_refreshes_after_the_ttl():
    clock, fetcher = Clock(), Fetcher(("k1", KEY))
    c = cache(fetcher, clock)
    assert key_for(c, "k1") is not None and key_for(c, "k1") is not None
    assert fetcher.calls == 1
    clock.now += 101
    assert key_for(c, "k1") is not None
    assert fetcher.calls == 2


def test_an_unknown_kid_refreshes_at_most_once_per_interval():
    clock, fetcher = Clock(), Fetcher(("k1", KEY))
    c = cache(fetcher, clock)
    assert key_for(c, "k1") is not None
    for _ in range(50):  # an attacker spraying forged kids
        assert key_for(c, "forged") is None
    assert fetcher.calls == 1  # the load just happened: the forged kids cause no refresh at all
    clock.now += 31
    for _ in range(50):
        assert key_for(c, "forged") is None
    assert fetcher.calls == 2  # one refresh per interval, not 50


def test_a_rotated_key_is_picked_up_by_the_refresh_on_unknown_kid():
    clock, fetcher = Clock(), Fetcher(("k1", KEY))
    c = cache(fetcher, clock)
    key_for(c, "k1")
    fetcher.keys = [("k1", KEY), ("k2", OTHER)]
    clock.now += 31
    assert key_for(c, "k2") is not None


def test_stale_keys_serve_for_the_grace_period_then_fail_closed():
    clock, fetcher = Clock(), Fetcher(("k1", KEY))
    c = cache(fetcher, clock)
    key_for(c, "k1")
    fetcher.fail = True
    clock.now += 101  # stale, Cloudflare down: still served
    assert key_for(c, "k1") is not None
    clock.now += 600  # beyond ttl + grace
    with pytest.raises(JwksUnavailable):
        key_for(c, "k1")


def test_a_fetch_error_with_an_empty_cache_fails_closed():
    clock, fetcher = Clock(), Fetcher()
    fetcher.fail = True
    with pytest.raises(JwksUnavailable):
        key_for(cache(fetcher, clock), "k1")


# ---- F1: legacy, single-tenant only (tenant provisioning design, 0.1) -------------------------------------------------
# Access has no tenant concept: every Access-authenticated person becomes a user of one tenant. With a second tenant that
# is a way into the first one, so the plug-in refuses to start as soon as the configuration names another tenant.


@pytest.mark.parametrize(
    "extra",
    [
        {"EAP_ZITADEL_ORG_TENANT_MAP": "org-1=studio,org-2=acme"},
        {"EAP_ZITADEL_ORG_TENANT_MAP": "org-2=acme"},
        {"EAP_ZITADEL_ORG_TENANT_MAP": '{"org-1": "studio", "org-2": "acme"}'},
        {"EAP_ZITADEL_ORG_TENANT_MAP": "{not json"},
        {"EAP_ZITADEL_ORG_TENANT_MAP": "garbage"},
        {"EAP_TENANT_REGISTRY": "enforce"},
    ],
    ids=repr,
)
def test_cfaccess_refuses_to_start_when_there_is_more_than_one_tenant(extra):
    with pytest.raises(IamConfigError, match="single-tenant"):
        provider(**extra)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"EAP_ZITADEL_ORG_TENANT_MAP": ""},
        {"EAP_ZITADEL_ORG_TENANT_MAP": "org-1=studio"},
        {"EAP_ZITADEL_ORG_TENANT_MAP": ' {"org-1": "studio"} '},
        {"EAP_TENANT_REGISTRY": "off"},
    ],
    ids=repr,
)
def test_cfaccess_still_starts_for_its_one_tenant_and_warns_that_it_is_legacy(extra, caplog):
    assert provider(**extra).tenant_id == TENANT
    assert any(r.levelno == logging.WARNING and "legacy" in r.getMessage() for r in caplog.records)
