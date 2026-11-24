"""The dev bearer over loopback only, with no database."""

import asyncio

import pytest

from eap_iam import AuthError, RequestView
from eap_iam_devloopback import DevLoopbackProvider

BEARER = "Bearer dev:studio:operator"


@pytest.fixture(autouse=True)
def dev_mode(monkeypatch):
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")


def authenticate(provider, peer, **headers):
    headers.setdefault("authorization", BEARER)
    view = RequestView(headers=headers, peer_host=peer, accepts=frozenset({"user"}))
    return asyncio.run(provider.authenticate(view))


def test_loopback_peer_gets_a_principal():
    principal = authenticate(DevLoopbackProvider({}), "127.0.0.1")
    assert (principal.tenant_id, principal.subject, principal.kind, principal.provider) == (
        "studio", "operator", "user", "loopback-dev",
    )
    assert authenticate(DevLoopbackProvider({}), "::1").subject == "operator"


@pytest.mark.parametrize("extra", [{}, {"x-forwarded-for": "127.0.0.1"}, {"cf-connecting-ip": "127.0.0.1"}])
def test_a_non_loopback_peer_is_refused_with_or_without_forwarded_headers(extra):
    with pytest.raises(AuthError) as info:
        authenticate(DevLoopbackProvider({}), "10.42.0.9", **extra)
    assert info.value.status == 401


def test_a_forwarding_header_on_a_loopback_peer_is_not_loopback():
    with pytest.raises(AuthError):
        authenticate(DevLoopbackProvider({}), "127.0.0.1", **{"x-forwarded-for": "203.0.113.5"})


def test_extra_peers_name_a_test_peer_as_loopback():
    provider = DevLoopbackProvider({"EAP_IAM_DEVLOOPBACK_EXTRA_PEERS": "testclient"})
    assert authenticate(provider, "testclient").subject == "operator"
    with pytest.raises(AuthError):
        authenticate(provider, "testclient", **{"x-forwarded-for": "1.2.3.4"})


def test_other_credentials_are_not_this_providers():
    assert authenticate(DevLoopbackProvider({}), "127.0.0.1", authorization="Bearer pos_abc") is None
    assert asyncio.run(
        DevLoopbackProvider({}).authenticate(RequestView(headers={}, peer_host="127.0.0.1", accepts=frozenset({"user"})))
    ) is None


@pytest.mark.parametrize(
    "bearer, detail",
    [
        ("Bearer dev:a", "Invalid Authorization header format"),
        ("Bearer dev:a:b:c", "Invalid Authorization header format"),
        ("Bearer dev: :b", "Tenant ID cannot be empty"),
        ("Bearer dev:a: ", "User ID cannot be empty"),
        ("Bearer dev:a b:u", "Invalid tenant ID format"),
        ("Bearer dev:a:u!", "Invalid user ID format"),
    ],
)
def test_malformed_bearers_keep_their_messages(bearer, detail):
    with pytest.raises(AuthError) as info:
        authenticate(DevLoopbackProvider({}), "127.0.0.1", authorization=bearer)
    assert info.value.detail == detail and info.value.www_authenticate is False


def test_the_bearer_needs_dev_mode_at_request_time(monkeypatch):
    monkeypatch.setenv("EAP_AUTH_MODE", "production")
    with pytest.raises(AuthError) as info:
        authenticate(DevLoopbackProvider({}), "127.0.0.1")
    assert info.value.detail == "dev bearer authentication is not enabled"


def test_a_cloudflare_access_jwt_never_makes_the_bearer_loopback():
    with pytest.raises(AuthError):
        authenticate(DevLoopbackProvider({}), "127.0.0.1", **{"cf-access-jwt-assertion": "x.y.z"})
