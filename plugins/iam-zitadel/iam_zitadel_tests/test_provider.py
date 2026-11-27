"""The ZITADEL identity provider: a token from a fake ZITADEL (generated RSA keys behind a JWKS fake) authenticates, and
each refusal of design 1.7 answers 401 or 403. See README: the CI uses a JWKS fake, not a ZITADEL container."""

import asyncio
import base64
import json
import logging

import jwt
import pytest
from eap_iam import AuthError, IamConfigError, IdentityChain, PluginContext, Principal, RequestView

from eap_iam_zitadel import create_identity

from .fakes import ENV, ISSUER, KEY, ORG, OTHER, PROJECT, TENANT, Fetcher, mint


def provider(fetcher=None, env=None, userinfo=None):
    fetcher = fetcher or Fetcher(("k1", KEY))
    p = create_identity(PluginContext(env={**ENV, **(env or {})}), fetcher=fetcher)
    if userinfo is not None:
        p._userinfo = userinfo
    else:
        async def no_userinfo(token):
            return {}
        p._userinfo = no_userinfo
    return p


def bearer(token):
    return {"authorization": f"Bearer {token}"}


def authenticate(p, headers):
    return asyncio.run(p.authenticate(RequestView(headers=headers, peer_host="10.42.0.9", accepts=frozenset({"user"}))))


def refused(p, headers, status=401):
    with pytest.raises(AuthError) as err:
        authenticate(p, headers)
    assert err.value.status == status
    return err.value


def test_a_valid_token_carries_sub_email_org_tenant_and_roles():
    principal = authenticate(provider(), bearer(mint(roles={"admin": {ORG: "x"}, "viewer": {ORG: "x"}, "bogus": {ORG: "x"}})))
    assert (principal.kind, principal.subject, principal.tenant_id, principal.provider) == ("user", "zitadel:user-1", TENANT, "zitadel")  # the stable sub, never the e-mail
    assert principal.extra["email"] == "user@example.com"
    assert principal.roles == frozenset({"admin", "viewer"})  # a role the platform does not define grants nothing
    assert principal.extra["sub"] == "user-1" and principal.extra["org_id"] == ORG


def test_project_specific_roles_claim_is_read_too():
    token = mint(roles={}, **{f"urn:zitadel:iam:org:project:{PROJECT}:roles": {"editor": {ORG: "x"}}})
    assert authenticate(provider(), bearer(token)).roles == frozenset({"editor"})


def test_a_machine_user_without_an_email_is_a_service():
    principal = authenticate(provider(), bearer(mint(email=None, sub="machine-9")))
    assert (principal.kind, principal.subject, principal.tenant_id) == ("service", "zitadel:machine-9", TENANT)


def test_the_email_comes_from_userinfo_when_the_token_has_none_and_is_cached():
    calls = []

    async def userinfo(token):
        calls.append(1)
        return {"email": "Late@Example.com"}

    p = provider(userinfo=userinfo)
    token = mint(email=None)
    assert authenticate(p, bearer(token)).extra["email"] == "late@example.com"
    assert authenticate(p, bearer(token)).subject == "zitadel:user-1"
    assert len(calls) == 1


def test_a_userinfo_failure_is_a_401_not_a_service():
    async def userinfo(token):
        raise RuntimeError("down")

    refused(provider(userinfo=userinfo), bearer(mint(email=None)))


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: mint(aud=["client-1", "other-project"]), id="wrong-aud"),
        pytest.param(lambda: mint(aud="other-project"), id="wrong-aud-string"),
        pytest.param(lambda: mint(iss="https://evil.example.test"), id="wrong-iss"),
        pytest.param(lambda: mint(exp=1), id="expired"),
        pytest.param(lambda: mint(nbf=4102444800), id="not-yet-valid"),
        pytest.param(lambda: mint(key=OTHER), id="wrong-key"),
        pytest.param(lambda: mint(sub=None), id="no-sub"),
        pytest.param(lambda: mint(exp=None), id="no-exp"),
    ],
)
def test_claim_and_signature_failures_are_401(token):
    refused(provider(), bearer(token()))


def test_unknown_kid_is_401_and_the_refresh_is_bounded():
    fetcher = Fetcher(("k1", KEY))
    p = provider(fetcher)
    for i in range(20):  # a stream of forged kids
        refused(p, bearer(mint(kid=f"forged-{i}")))
    assert fetcher.calls == 1  # one load; the forged kids did not make the API hammer ZITADEL (min refresh 30 s)
    assert authenticate(p, bearer(mint())).subject == "zitadel:user-1"


def test_a_rotated_key_is_picked_up_by_one_refresh():
    fetcher = Fetcher(("k1", KEY))
    p = provider(fetcher, env={"EAP_ZITADEL_JWKS_MIN_REFRESH_SECONDS": "0"})
    assert authenticate(p, bearer(mint())).subject
    fetcher.keys.append(("k2", OTHER))
    assert authenticate(p, bearer(mint(key=OTHER, kid="k2"))).subject
    assert fetcher.calls == 2


@pytest.mark.parametrize("alg", ["HS256", "ES256"])
def test_wrong_alg_is_401(alg):
    forged = jwt.encode({"iss": ISSUER, "aud": PROJECT, "sub": "x", "exp": 4102444800}, "secret" * 8 if alg == "HS256" else _ec(), algorithm=alg, headers={"kid": "k1"})
    refused(provider(), bearer(forged))


def _ec():
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def test_alg_none_is_401():
    def seg(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    token = f"{seg({'alg': 'none', 'kid': 'k1'})}.{seg({'iss': ISSUER, 'aud': PROJECT, 'sub': 'x', 'exp': 4102444800})}."
    refused(provider(), bearer(token))


def test_a_missing_kid_is_401():
    refused(provider(), bearer(mint(kid=None)))


def test_an_unmapped_org_is_403_and_a_missing_org_too():
    err = refused(provider(), bearer(mint(org="org-stranger")), status=403)
    assert err.www_authenticate is False
    refused(provider(), bearer(mint(org=None)), status=403)


def test_jwks_outage_with_nothing_cached_fails_closed():
    def down():
        from eap_iam_zitadel.jwks import JwksUnavailable

        raise JwksUnavailable("down")

    refused(create_identity(PluginContext(env=dict(ENV)), fetcher=down), bearer(mint()))


@pytest.mark.parametrize("header", [None, "Basic dXNlcjpwYXNz", "Bearer pos_abc_def", "Bearer dev:studio:operator", "Bearer a.b", "Bearer ", "Bearer x.y.z"])
def test_a_credential_that_is_not_a_jwt_is_not_ours(header):
    headers = {} if header is None else {"authorization": header}
    assert authenticate(provider(), headers) is None


def test_a_plain_email_header_is_never_trusted():
    assert authenticate(provider(), {"x-forwarded-email": "a@b.co", "cf-access-authenticated-user-email": "a@b.co"}) is None


def test_the_token_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    token = mint()
    authenticate(provider(), bearer(token))
    refused(provider(), bearer(mint(iss="https://evil.example.test")))
    assert token not in caplog.text and "Bearer" not in caplog.text


def test_an_invalid_token_stops_the_chain_and_never_falls_through():
    class Later:
        name, kinds = "later", frozenset({"user"})

        async def authenticate(self, request):
            return Principal(tenant_id="t", kind="user", subject="weak", provider="later")

    chain = IdentityChain([provider(), Later()])
    view = RequestView(headers=bearer(mint(iss="https://evil.example.test")), peer_host="10.0.0.1", accepts=frozenset({"user"}))
    with pytest.raises(AuthError) as err:
        asyncio.run(chain.authenticate(view))
    assert err.value.status == 401
    # a credential that is not a JWT falls to the next provider, as the order intends
    view = RequestView(headers={"authorization": "Bearer pos_x_y"}, peer_host="10.0.0.1", accepts=frozenset({"user"}))
    assert asyncio.run(chain.authenticate(view)).subject == "weak"


def test_dormant_without_project_and_org_map_recognises_nothing():
    p = create_identity(PluginContext(env={}))
    assert p.dormant
    assert authenticate(p, bearer(mint())) is None


@pytest.mark.parametrize(
    "env",
    [
        {"EAP_ZITADEL_PROJECT_ID": "p"},
        {"EAP_ZITADEL_ORG_TENANT_MAP": "o=t"},
        {"EAP_ZITADEL_PROJECT_ID": "p", "EAP_ZITADEL_ORG_TENANT_MAP": "o=t"},  # no issuer
        {**ENV, "EAP_ZITADEL_ORG_TENANT_MAP": "garbage"},
        {**ENV, "EAP_ZITADEL_ORG_TENANT_MAP": "o=t,o=u"},
        {**ENV, "EAP_ZITADEL_ORG_TENANT_MAP": "o=t,p=t"},
        {**ENV, "EAP_ZITADEL_ISSUER": "not a url"},
        {**ENV, "EAP_ZITADEL_MACHINE_KEY": "{not json"},
    ],
)
def test_a_half_configured_plugin_refuses_to_start(env):
    with pytest.raises(IamConfigError):
        create_identity(PluginContext(env=env))


def test_the_org_map_accepts_json_and_several_orgs():
    p = create_identity(PluginContext(env={**ENV, "EAP_ZITADEL_ORG_TENANT_MAP": json.dumps({ORG: TENANT, "o2": "other"})}))
    assert p._settings.org_tenants == {ORG: TENANT, "o2": "other"}


def test_the_service_key_is_not_in_a_repr():
    from eap_iam_zitadel.settings import parse_machine_key

    from .fakes import machine_key_json

    assert "BEGIN" not in repr(parse_machine_key(machine_key_json()))


def test_the_subject_survives_an_email_change():
    a = authenticate(provider(), bearer(mint(email="old@example.com")))
    b = authenticate(provider(), bearer(mint(email="new@example.com")))
    assert a.subject == b.subject == "zitadel:user-1"
    assert (a.extra["email"], b.extra["email"]) == ("old@example.com", "new@example.com")


# ---- F2: only roles granted in the principal's own organisation count (tenant provisioning design, 0.1) --------------
# The real claim is {role: {granting organisation id: its primary domain}}: ZITADEL v4.20.5,
# internal/api/oidc/client.go `projectRoles.Add(roleKey, grant.ResourceOwner, grant.OrgPrimaryDomain)`.


def test_a_role_granted_by_another_organisation_is_ignored():
    roles = {"admin": {"org-other": "other.test"}, "viewer": {ORG: "x"}}
    assert authenticate(provider(), bearer(mint(roles=roles))).roles == frozenset({"viewer"})


def test_a_role_granted_by_both_organisations_counts():
    roles = {"admin": {"org-other": "other.test", ORG: "x"}}
    assert authenticate(provider(), bearer(mint(roles=roles))).roles == frozenset({"admin"})


def test_the_project_specific_claim_is_filtered_by_organisation_too():
    token = mint(roles={}, **{f"urn:zitadel:iam:org:project:{PROJECT}:roles": {"admin": {"org-other": "x"}}})
    assert authenticate(provider(), bearer(token)).roles == frozenset()


def test_the_organisation_decides_not_the_tenant_the_claim_names():
    env = {"EAP_ZITADEL_ORG_TENANT_MAP": f"{ORG}={TENANT},org-b=other"}
    token = mint(org="org-b", roles={"admin": {ORG: "x"}})  # a user of org B holding a grant made in org A
    principal = authenticate(provider(env=env), bearer(token))
    assert (principal.tenant_id, principal.roles) == ("other", frozenset())


@pytest.mark.parametrize("value", [{}, [], "org-1", None, True, ["org-1"]], ids=repr)
def test_a_role_without_the_organisation_map_grants_nothing(value):
    assert authenticate(provider(), bearer(mint(roles={"admin": value}))).roles == frozenset()


def test_a_foreign_grant_is_logged_at_warning_once_per_subject(caplog):
    caplog.set_level(logging.DEBUG)
    p = provider()
    foreign = {"admin": {"org-other": "other.test"}}
    for _ in range(3):
        authenticate(p, bearer(mint(roles=foreign)))
    authenticate(p, bearer(mint(roles=foreign, sub="user-2")))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2 and "zitadel:user-1" in warnings[0] and "zitadel:user-2" in warnings[1]
    assert "admin" in warnings[0] and "other.test" not in caplog.text and "Bearer" not in caplog.text


def test_own_organisation_grants_log_no_warning(caplog):
    authenticate(provider(), bearer(mint()))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_role_the_platform_does_not_define_is_not_worth_a_warning(caplog):
    authenticate(provider(), bearer(mint(roles={"bogus": {"org-other": "x"}})))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_fake_builds_the_role_claim_in_the_real_shape_and_only_the_own_grant_counts():
    from .fakes import FakeZitadel

    fake = FakeZitadel()
    fake.add_member("u-1", "ada@example.test", "viewer")
    fake.add_org("org-b", "Acme")
    fake.grant_in("org-b", "u-1", "admin")  # another organisation grants this user admin
    claim = fake.role_claim("u-1")
    assert claim == {"viewer": {ORG: "durable.auth.example.test"}, "admin": {"org-b": "acme.auth.example.test"}}
    assert authenticate(provider(), bearer(mint(sub="u-1", roles=claim))).roles == frozenset({"viewer"})
