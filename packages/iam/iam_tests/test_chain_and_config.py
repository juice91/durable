"""Chain and start-up rules of the IAM core, with fake providers: no plug-in, no web framework, no database."""

import asyncio

import pytest

from eap_iam import (
    AuthError,
    IamConfigError,
    IdentityChain,
    Principal,
    RequestView,
    require_iam_configured,
)

INSTALLED_IDENTITY = {"apikey": object(), "devloopback": object(), "cfaccess": object()}
INSTALLED_POLICY = {"openaccess": object(), "other": object()}


def run(coro):
    return asyncio.run(coro)


class Provider:
    def __init__(self, name, kinds, outcome):
        self.name, self.kinds, self.outcome, self.calls = name, frozenset(kinds), outcome, 0

    async def authenticate(self, request):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def principal(kind="user", provider="p"):
    return Principal(tenant_id="t", kind=kind, subject="s", provider=provider)


def view(accepts=("user", "api_key"), **headers):
    return RequestView(headers=headers, accepts=frozenset(accepts))


def test_first_match_wins_and_later_providers_are_not_asked():
    first, second = Provider("a", {"user"}, principal(provider="a")), Provider("b", {"user"}, principal(provider="b"))
    got = run(IdentityChain([first, second]).authenticate(view(authorization="Bearer x")))
    assert got.provider == "a" and second.calls == 0


def test_none_falls_through_to_the_next_provider():
    first, second = Provider("a", {"user"}, None), Provider("b", {"user"}, principal(provider="b"))
    assert run(IdentityChain([first, second]).authenticate(view(authorization="Bearer x"))).provider == "b"
    assert first.calls == 1


def test_an_invalid_credential_answers_401_and_is_not_retried_by_a_later_provider():
    first = Provider("a", {"user"}, AuthError("bad token"))
    second = Provider("b", {"user"}, principal(provider="b"))
    with pytest.raises(AuthError) as info:
        run(IdentityChain([first, second]).authenticate(view(authorization="Bearer x")))
    assert info.value.status == 401 and info.value.detail == "bad token" and second.calls == 0


def test_a_route_that_accepts_only_api_keys_skips_user_providers():
    user, key = Provider("u", {"user"}, principal(provider="u")), Provider("k", {"api_key"}, principal("api_key", "k"))
    got = run(IdentityChain([user, key]).authenticate(view(accepts=("api_key",), authorization="Bearer x")))
    assert got.provider == "k" and user.calls == 0


def test_no_applicable_provider_and_no_match_have_fixed_answers():
    chain = IdentityChain([Provider("k", {"api_key"}, None)])
    with pytest.raises(AuthError) as no_user:
        run(chain.authenticate(view(accepts=("user",), authorization="Bearer x")))
    assert no_user.value.detail == "dev bearer authentication is not enabled"
    with pytest.raises(AuthError) as missing:
        run(chain.authenticate(view(accepts=("api_key",))))
    assert missing.value.detail == "Missing Authorization header"
    with pytest.raises(AuthError) as unknown:
        run(IdentityChain([Provider("u", {"user"}, None)]).authenticate(view(accepts=("user",), authorization="x")))
    assert unknown.value.detail == "Invalid Authorization header format"


def test_a_provider_cannot_hand_a_route_a_kind_it_refuses():
    chain = IdentityChain([Provider("k", {"user", "api_key"}, principal("api_key"))])
    with pytest.raises(AuthError):
        run(chain.authenticate(view(accepts=("user",), authorization="Bearer x")))


def configure(**env):
    return require_iam_configured(env, identity=INSTALLED_IDENTITY, policy=INSTALLED_POLICY, membership={})


def test_unset_or_unknown_mode_refuses():
    for mode in (None, "DEV", "apikey"):
        env = {} if mode is None else {"EAP_AUTH_MODE": mode}
        with pytest.raises(IamConfigError, match="EAP_AUTH_MODE=dev"):
            configure(**env)


def test_dev_defaults_to_apikey_devloopback_and_openaccess():
    settings = configure(EAP_AUTH_MODE="dev")
    assert settings.identity == ("apikey", "devloopback") and settings.policy == "openaccess"


def test_dev_without_the_default_plugins_installed_refuses():
    with pytest.raises(IamConfigError, match="not installed"):
        require_iam_configured({"EAP_AUTH_MODE": "dev"}, identity={}, policy={}, membership={})


def test_production_needs_an_identity_provider_and_exactly_one_policy_engine():
    with pytest.raises(IamConfigError, match="EAP_IAM_IDENTITY"):
        configure(EAP_AUTH_MODE="production", EAP_IAM_POLICY="openaccess")
    with pytest.raises(IamConfigError, match="exactly one"):
        configure(EAP_AUTH_MODE="production", EAP_IAM_IDENTITY="apikey")
    with pytest.raises(IamConfigError, match="exactly one"):
        configure(EAP_AUTH_MODE="production", EAP_IAM_IDENTITY="apikey", EAP_IAM_POLICY="openaccess,other")
    ok = configure(EAP_AUTH_MODE="production", EAP_IAM_IDENTITY="apikey,cfaccess", EAP_IAM_POLICY="other")
    assert ok.identity == ("apikey", "cfaccess") and not ok.allow_transitional


def test_unknown_and_duplicate_names_refuse():
    with pytest.raises(IamConfigError, match="not installed: nope"):
        configure(EAP_AUTH_MODE="dev", EAP_IAM_IDENTITY="apikey,nope")
    with pytest.raises(IamConfigError, match="twice"):
        configure(EAP_AUTH_MODE="dev", EAP_IAM_IDENTITY="apikey,apikey")
    with pytest.raises(IamConfigError, match="not installed"):
        configure(EAP_AUTH_MODE="dev", EAP_IAM_POLICY="nope")


def test_api_key_mode_is_a_deprecated_alias_for_apikey_with_the_transitional_engine():
    settings = configure(EAP_AUTH_MODE="api_key")
    assert settings.identity == ("apikey",) and settings.policy == "openaccess" and settings.allow_transitional


def test_dev_bearer_refuses_wildcard_forwarded_allow_ips():
    with pytest.raises(IamConfigError, match="FORWARDED_ALLOW_IPS"):
        configure(EAP_AUTH_MODE="dev", FORWARDED_ALLOW_IPS="*")
