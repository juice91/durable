"""The ZITADEL contract: what the platform relies on, run against the stateful fake AND against a real ZITADEL v4.20.5
(`EAP_TEST_ZITADEL_DOCKER=1`, `server.py`; CI Heavy). A skip is not a pass: without that variable only the `fake`
cases run.

The `real`-only tests are the facts no fake can prove: the shape of the project-roles claim (F2), what each split
machine user can and cannot do (F3), that public registration is really refused (F5) and that the invite link sets a
password without SMTP."""

from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from eap_iam import MembershipError, PluginContext, RequestView

from eap_iam_zitadel import create_identity, create_membership
from eap_iam_zitadel.bootstrap import amain
from eap_iam_zitadel.management import ManagementClient, ZitadelApiError
from eap_iam_zitadel.settings import parse_machine_key

from .fakes import FakeZitadel, machine_key_json
from .server import zitadel  # noqa: F401 - the session fixture

EQUALS = "TEXT_QUERY_METHOD_EQUALS"
FAR = "2099-01-01T00:00:00Z"
PASSWORD = "Aa1!contract-test-only"


@dataclass
class Idp:
    kind: str
    url: str
    root_key: str
    runner: asyncio.Runner
    fake: FakeZitadel | None = None

    def run(self, coro):
        return self.runner.run(coro)

    def client(self, key: str) -> ManagementClient:
        http = self.fake.client if self.fake else None
        return ManagementClient(api_url=self.url, issuer=self.url, host=self.url.split("//")[1], key=parse_machine_key(key), http=http)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None, *, org: str | None = None, key: str | None = None):
        async def go():
            client = self.client(key or self.root_key)
            try:
                return await client.request(method, path, org_id=org, json=body)
            finally:
                if not self.fake:
                    await client.aclose()

        return self.run(go())

    def status(self, method: str, path: str, body: dict[str, Any] | None = None, *, org: str | None = None, key: str) -> int:
        try:
            self.call(method, path, body, org=org, key=key)
        except ZitadelApiError as exc:
            return exc.status
        return 200

    def cli(self, *args: str) -> str:
        out = io.StringIO()
        code = self.run(amain(
            ["--issuer", self.url, *args], env={"EAP_ZITADEL_MACHINE_KEY": self.root_key}, out=out,
            http=self.fake.client if self.fake else None,
        ))
        assert code == 0, out.getvalue()
        return out.getvalue()

    def user_id(self, name: str, org: str) -> str:
        found = self.call(
            "POST", "/management/v1/users/_search", {"queries": [{"userNameQuery": {"userName": name, "method": EQUALS}}]}, org=org
        )
        (user,) = found["result"]
        return user["id"]


def printed_key(text: str, name: str) -> str:
    lines = text.splitlines()
    return next(lines[i + 1] for i, line in enumerate(lines) if line.startswith(f"{name} key (shown once"))


@dataclass
class Platform:
    idp: Idp
    org: str  # the operator organisation
    project: str
    dry_run: str
    users_after_dry_run: list[str]
    first: str
    second: str
    provisioner_key: str
    members_key: str


def _build(idp: Idp) -> Platform:
    admin = "zitadel-admin" if idp.kind == "real" else "admin"
    idp.cli("--admin-user", admin)  # the ordinary bootstrap: project, roles, applications
    org = idp.call("GET", "/management/v1/orgs/me")["org"]["id"]
    found = idp.call("POST", "/management/v1/projects/_search", {"queries": [{"nameQuery": {"name": "durable", "method": EQUALS}}]})
    project = found["result"][0]["id"]
    dry = idp.cli("--platform-users", "--dry-run")
    names = [
        u["userName"]
        for u in idp.call("POST", "/management/v1/users/_search", {"queries": []}, org=org).get("result", [])
        if u["userName"] in ("platform-provisioner", "platform-members")
    ]
    first = idp.cli("--platform-users")
    second = idp.cli("--platform-users")
    return Platform(
        idp, org, project, dry, names, first, second,
        printed_key(first, "platform-provisioner"), printed_key(first, "platform-members"),
    )


@pytest.fixture(scope="module")
def runner():
    with asyncio.Runner() as r:
        yield r


@pytest.fixture(scope="module")
def real(request, runner) -> Platform:
    server = request.getfixturevalue("zitadel")
    return _build(Idp("real", server.url, server.key_json, runner))


@pytest.fixture(scope="module", params=["fake", "real"])
def platform(request, runner) -> Platform:
    if request.param == "real":
        return request.getfixturevalue("real")
    fake = FakeZitadel()
    return _build(Idp("fake", "https://auth.example.test", machine_key_json(), runner, fake))


# ---- shared: the same behaviour on the fake and on the real server ---------------------------------------------------


def test_the_access_app_is_made_only_on_request_and_a_later_run_keeps_it(platform):
    idp = platform.idp

    def names() -> set[str]:
        found = idp.call("POST", f"/management/v1/projects/{platform.project}/apps/_search", {"queries": []}, org=platform.org)
        return {a["name"] for a in found.get("result", [])}

    def until(output: str, *args: str) -> str:
        for _ in range(100):  # ZITADEL's read models are eventually consistent
            if output in (text := idp.cli(*args)):
                return text
            time.sleep(0.2)
        raise AssertionError(f"never printed {output!r}")

    admin = ("--admin-user", "zitadel-admin" if idp.kind == "real" else "admin")
    access = (*admin, "--cloudflare-access", "acme")
    # The module's first bootstrap ran without the flag.
    assert "cloudflare" not in until("nothing to change", *admin).lower() and names() == {"Durable Studio"}
    dry = idp.cli(*access, "--dry-run")
    assert "create application Cloudflare Access" in dry and names() == {"Durable Studio"}
    made = idp.cli(*access)
    assert "created application Cloudflare Access" in made and "https://acme.cloudflareaccess.com/cdn-cgi/access/callback" in made
    assert "would change nothing" in until("would change nothing", *access, "--dry-run")
    assert "nothing to change" in idp.cli(*access)
    assert "cloudflare" not in idp.cli(*admin).lower() and names() == {"Durable Studio", "Cloudflare Access"}


def test_the_platform_users_dry_run_creates_nothing(platform):
    assert "DRY RUN" in platform.dry_run and platform.users_after_dry_run == []
    assert "create the machine user platform-members" in platform.dry_run


def test_a_second_platform_users_run_changes_nothing_and_shows_no_key(platform):
    assert "nothing to change" in platform.second and "shown once" not in platform.second


def test_the_memberships_are_exactly_the_least_ones(platform):
    idp = platform.idp
    for name, instance, organisation in (
        ("platform-provisioner", ["IAM_ORG_MANAGER"], None),
        ("platform-members", None, ["ORG_USER_PERMISSION_EDITOR"]),
    ):
        uid = idp.user_id(name, platform.org)
        query = {"queries": [{"userIdQuery": {"userId": uid}}]}
        mine = lambda path, org=None: [  # noqa: E731
            m["roles"] for m in idp.call("POST", path, query, org=org).get("result", []) if m["userId"] == uid
        ]
        assert mine("/admin/v1/members/_search") == ([instance] if instance else [])
        assert mine("/management/v1/orgs/me/members/_search", platform.org) == ([organisation] if organisation else [])


def test_the_instance_policy_dry_run_then_apply_then_nothing(platform):
    idp = platform.idp
    before = idp.call("GET", "/admin/v1/policies/login")["policy"]
    dry = idp.cli("--instance-policy", "--dry-run")
    assert "DRY RUN" in dry
    assert idp.call("GET", "/admin/v1/policies/login")["policy"].get("allowRegister") == before.get("allowRegister")
    idp.cli("--instance-policy")
    after = idp.call("GET", "/admin/v1/policies/login")["policy"]
    for name in ("allowRegister", "allowExternalIdp", "allowDomainDiscovery"):
        assert not after.get(name, False), name
    assert after["forceMfa"] is True and after["secondFactors"]
    assert after["passwordCheckLifetime"] == before["passwordCheckLifetime"]
    assert after.get("allowUsernamePassword") is True
    assert idp.call("GET", "/admin/v1/restrictions")["disallowPublicOrgRegistration"] is True
    assert "nothing to change" in idp.cli("--instance-policy")


def test_the_members_key_runs_the_members_page_on_the_operator_organisation(platform):
    idp = platform.idp
    if idp.fake:  # the fake does not derive permissions from the caller's roles
        idp.fake.permissions[platform.org] = ["org.read", "user.read", "user.grant.read", "user.grant.write"]
        idp.fake.users["u-nia"] = {"id": "u-nia", "userName": "nia@example.test", "human": {"email": {"email": "nia@example.test"}}}
    else:  # the operator creates the person in the console (here: with the instance owner's key)
        idp.call("POST", "/v2/users/human", {
            "organization": {"orgId": platform.org}, "username": "nia@example.test",
            "profile": {"givenName": "Nia", "familyName": "Nia"}, "email": {"email": "nia@example.test", "returnCode": {}},
        })
    env = {
        "EAP_ZITADEL_ISSUER": idp.url, "EAP_ZITADEL_PROJECT_ID": platform.project,
        "EAP_ZITADEL_ORG_TENANT_MAP": f"{platform.org}=studio",
        # The fake only verifies assertions signed with the test machine key.
        "EAP_ZITADEL_MACHINE_KEY": idp.root_key if idp.fake else platform.members_key,
    }
    source = create_membership(PluginContext(env=env), http=idp.fake.client if idp.fake else None)

    async def until(check):
        for _ in range(100):  # ZITADEL's read models are eventually consistent
            if check(members := await source.list_members("studio")):
                return members
            await asyncio.sleep(0.2)
        raise AssertionError("the member list never caught up")

    async def go():
        assert await source.can_create_users("studio") is False
        with pytest.raises(MembershipError) as err:
            await source.invite("studio", "stranger@example.test", "viewer")
        assert err.value.code == "no_user_creation"
        added = await source.invite("studio", "nia@example.test", "viewer")
        await until(lambda ms: any(m.id == added.id and m.roles == ("viewer",) for m in ms))
        await source.set_role("studio", added.id, "editor")
        await until(lambda ms: any(m.id == added.id and m.roles == ("editor",) for m in ms))
        await source.remove("studio", added.id)
        await until(lambda ms: all(m.id != added.id for m in ms))

    idp.run(go())


# ---- real only -------------------------------------------------------------------------------------------------------


@dataclass
class Tenant:
    org: str
    grant: str


@pytest.fixture(scope="module")
def tenant(real) -> Tenant:
    """A second organisation, made as provisioning will make it: by the provisioner, with a chosen id."""
    idp = real.idp
    org = idp.call("POST", "/v2/organizations", {"name": "Acme", "organizationId": "acme-org-1"}, key=real.provisioner_key)
    assert org["organizationId"] == "acme-org-1"
    grant = idp.call(
        "POST", f"/management/v1/projects/{real.project}/grants",
        {"grantedOrgId": "acme-org-1", "roleKeys": ["admin", "editor", "viewer"]}, org=real.org, key=real.provisioner_key,
    )["grantId"]
    members = idp.user_id("platform-members", real.org)
    idp.call(
        "POST", "/management/v1/orgs/me/members", {"userId": members, "roles": ["ORG_USER_MANAGER"]}, org="acme-org-1",
        key=real.provisioner_key,
    )
    return Tenant("acme-org-1", grant)


def _machine(idp: Idp, name: str, org: str) -> tuple[str, str]:
    uid = idp.call(
        "POST", "/management/v1/users/machine",
        {"userName": name, "name": name, "accessTokenType": "ACCESS_TOKEN_TYPE_JWT"}, org=org,
    )["userId"]
    key = idp.call("POST", f"/management/v1/users/{uid}/keys", {"type": "KEY_TYPE_JSON", "expirationDate": FAR}, org=org)
    return uid, base64.b64decode(key["keyDetails"]).decode()


def _token(idp: Idp, key: str, project: str) -> str:
    import jwt

    data = json.loads(key)
    now = int(time.time())
    assertion = jwt.encode(
        {"iss": data["userId"], "sub": data["userId"], "aud": idp.url, "iat": now, "exp": now + 300},
        data["key"], algorithm="RS256", headers={"kid": data["keyId"]},
    )
    scope = (
        f"openid urn:zitadel:iam:org:project:id:{project}:aud urn:zitadel:iam:org:projects:roles "
        "urn:zitadel:iam:user:resourceowner"
    )
    response = httpx2.post(
        f"{idp.url}/oauth/v2/token",
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "scope": scope, "assertion": assertion},
    )
    assert response.status_code == 200, response.status_code
    return response.json()["access_token"]


def _claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_f2_the_role_claim_names_the_granting_organisation_and_the_provider_keeps_only_the_own_one(real, tenant):
    idp = real.idp
    insider, insider_key = _machine(idp, "svc-acme", tenant.org)
    outsider, outsider_key = _machine(idp, "svc-durable", real.org)
    grants = [
        (insider, tenant.org, {"projectId": real.project, "projectGrantId": tenant.grant, "roleKeys": ["viewer"]}),
        (outsider, real.org, {"projectId": real.project, "roleKeys": ["editor"]}),
        # The crossing grants F2 is about: ZITADEL accepts both.
        (insider, real.org, {"projectId": real.project, "roleKeys": ["admin"]}),
        (outsider, tenant.org, {"projectId": real.project, "projectGrantId": tenant.grant, "roleKeys": ["admin"]}),
    ]
    for user, org, body in grants:
        idp.call("POST", f"/management/v1/users/{user}/grants", body, org=org)
    claim_name = f"urn:zitadel:iam:org:project:{real.project}:roles"
    env = {
        "EAP_ZITADEL_ISSUER": idp.url, "EAP_ZITADEL_PROJECT_ID": real.project,
        "EAP_ZITADEL_ORG_TENANT_MAP": f"{real.org}=studio,{tenant.org}=acme",
    }
    provider = create_identity(PluginContext(env=env))
    for key, own, other, tenant_id, role in (
        (insider_key, tenant.org, real.org, "acme", "viewer"),
        (outsider_key, real.org, tenant.org, "studio", "editor"),
    ):
        for _ in range(100):  # the grants reach the read model a moment later
            token = _token(idp, key, real.project)
            claim = _claims(token).get(claim_name, {})
            if set(claim) == {"admin", role}:
                break
            time.sleep(0.2)
        assert set(claim[role]) == {own} and set(claim["admin"]) == {other}, claim
        assert all(isinstance(domain, str) and domain for orgs in claim.values() for domain in orgs.values())
        assert _claims(token)["urn:zitadel:iam:user:resourceowner:id"] == own
        view = RequestView(headers={"authorization": f"Bearer {token}"}, peer_host="10.0.0.1", accepts=frozenset({"user", "service"}))
        principal = idp.run(provider.authenticate(view))
        assert (principal.tenant_id, principal.roles) == (tenant_id, frozenset({role}))


def _machine_calls(user: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Every way to take a machine user over through the APIs: keys, secrets, tokens, state."""
    return [
        ("POST", f"/management/v1/users/{user}/keys", {"type": "KEY_TYPE_JSON", "expirationDate": FAR}),
        ("POST", f"/v2/users/{user}/keys", {"expirationDate": FAR}),
        ("PUT", f"/management/v1/users/{user}/secret", {}),
        ("POST", f"/v2/users/{user}/secret", {}),
        ("POST", f"/management/v1/users/{user}/pats", {"expirationDate": FAR}),
        ("POST", f"/v2/users/{user}/pats", {"expirationDate": FAR}),
        ("POST", f"/v2/users/{user}/deactivate", {}),
        ("DELETE", f"/v2/users/{user}", None),
    ]


def _human_calls(user: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Every way to take a person over: password, reset and invite codes, e-mail, factors, state."""
    return [
        ("POST", f"/v2/users/{user}/password", {"newPassword": {"password": PASSWORD, "changeRequired": False}}),
        ("POST", f"/management/v1/users/{user}/password", {"password": PASSWORD}),
        ("POST", f"/v2/users/{user}/password_reset", {"returnCode": {}}),
        ("POST", f"/v2/users/{user}/invite_code", {"returnCode": {}}),
        ("PUT", f"/v2/users/human/{user}", {"email": {"email": "taken@example.test", "isVerified": True}}),
        ("PATCH", f"/v2/users/{user}", {"human": {"email": {"email": "taken@example.test", "isVerified": True}}}),
        ("PUT", f"/management/v1/users/{user}/email", {"email": "taken@example.test", "isEmailVerified": True}),
        ("POST", f"/v2/users/{user}/passkeys/registration_link", {"returnCode": {}}),
        ("POST", f"/v2/users/{user}/totp", {}),
        ("POST", f"/v2/users/{user}/deactivate", {}),
        ("DELETE", f"/v2/users/{user}", None),
    ]


def _attacks(targets: dict[str, str]):
    """(target, method, path, body): each call only against the user type it applies to. ZITADEL answers a call for the
    wrong type "not found" before it looks at permissions, which would prove nothing."""
    for name, user in targets.items():
        for method, path, body in (_human_calls(user) if name == "zitadel-admin" else _machine_calls(user)):
            yield name, method, path, body


def _targets(real) -> dict[str, str]:
    idp = real.idp
    users = idp.call("POST", "/management/v1/users/_search", {"queries": []}, org=real.org)["result"]
    wanted = ("platform-automation", "platform-provisioner", "zitadel-admin")
    found = {name: u["id"] for u in users for name in wanted if u["userName"].split("@")[0] == name}
    assert set(found) == set(wanted)
    return found


def test_f3_every_credential_call_of_the_members_key_on_the_operator_organisation_is_403(real, tenant):
    idp = real.idp
    targets = _targets(real)
    refused = {
        (name, method, path): idp.status(method, path, body, org=real.org, key=real.members_key)
        for name, method, path, body in _attacks(targets)
    }
    assert len(refused) == 27 and {k: v for k, v in refused.items() if v != 403} == {}
    # Naming the tenant organisation, where the key does manage users, opens nothing either.
    through_tenant = {
        (name, method, path): idp.status(method, path, body, org=tenant.org, key=real.members_key)
        for name, method, path, body in _attacks(targets)
    }
    assert {k: v for k, v in through_tenant.items() if v < 400} == {}
    # Nor can it create users or widen its own rights.
    me = idp.user_id("platform-members", real.org)
    for method, path, body in (
        ("POST", "/v2/users/human", {
            "organization": {"orgId": real.org}, "username": "mallory@example.test",
            "profile": {"givenName": "m", "familyName": "m"}, "email": {"email": "mallory@example.test"},
        }),
        ("POST", "/management/v1/users/machine", {"userName": "mallory", "name": "mallory"}),
        ("PUT", f"/management/v1/orgs/me/members/{me}", {"roles": ["ORG_OWNER"]}),
        ("POST", "/admin/v1/members", {"userId": me, "roles": ["IAM_OWNER"]}),
        ("PUT", "/admin/v1/policies/login", {"allowRegister": True}),
        ("PUT", "/admin/v1/restrictions", {"disallowPublicOrgRegistration": False}),
    ):
        assert idp.status(method, path, body, org=real.org, key=real.members_key) == 403, (method, path)
    assert idp.status("POST", "/v2/organizations", {"name": "Mallory"}, key=real.members_key) in (403, 404)
    owner_keys = idp.call("POST", f"/management/v1/users/{targets['platform-automation']}/keys/_search", {}, org=real.org)
    assert len(owner_keys["result"]) == 1  # only the first instance's own


def test_f3_why_not_org_user_manager_it_can_mint_a_key_for_the_instance_owner(real):
    """The role the design first named. If ZITADEL ever refuses this, `platform-members` could be widened again."""
    idp = real.idp
    uid, key = _machine(idp, "svc-user-manager", real.org)
    idp.call("POST", "/management/v1/orgs/me/members", {"userId": uid, "roles": ["ORG_USER_MANAGER"]}, org=real.org)
    owner = _targets(real)["platform-automation"]
    path = f"/management/v1/users/{owner}/keys"
    for _ in range(100):
        status = idp.status("POST", path, {"type": "KEY_TYPE_JSON", "expirationDate": FAR}, org=real.org, key=key)
        if status == 200:
            break
        time.sleep(0.2)
    assert status == 200
    for found in idp.call("POST", f"{path}/_search", {}, org=real.org)["result"][1:]:
        idp.call("DELETE", f"{path}/{found['id']}", org=real.org)
    idp.call("DELETE", f"/management/v1/orgs/me/members/{uid}", org=real.org)


def test_f3_the_provisioner_cannot_change_the_instance(real):
    idp = real.idp
    me = idp.user_id("platform-provisioner", real.org)
    for method, path, body in (
        ("PUT", "/admin/v1/policies/login", {"allowRegister": True}),
        ("PUT", "/admin/v1/restrictions", {"disallowPublicOrgRegistration": False}),
        ("POST", "/admin/v1/members", {"userId": me, "roles": ["IAM_OWNER"]}),
    ):
        assert idp.status(method, path, body, key=real.provisioner_key) == 403, (method, path)


def test_the_members_key_creates_users_and_grants_in_a_tenant_organisation(real, tenant):
    idp = real.idp
    user = idp.call("POST", "/v2/users/human", {
        "organization": {"orgId": tenant.org}, "username": "member@acme.test",
        "profile": {"givenName": "m", "familyName": "m"}, "email": {"email": "member@acme.test", "returnCode": {}},
    }, key=real.members_key)["userId"]
    idp.call(
        "POST", f"/management/v1/users/{user}/grants",
        {"projectId": real.project, "projectGrantId": tenant.grant, "roleKeys": ["viewer"]}, org=tenant.org, key=real.members_key,
    )
    assert idp.call("POST", f"/v2/users/{user}/invite_code", {"returnCode": {}}, key=real.members_key)["inviteCode"]


def _page(url: str, path: str, **params: str) -> httpx2.Response:
    return httpx2.get(url + path, params=params, follow_redirects=True, timeout=30)


def test_f5_public_registration_is_refused_after_the_instance_policy(real, tenant):
    idp = real.idp
    idp.cli("--instance-policy")
    for _ in range(100):  # the restriction reaches the login UI through a read model
        if _page(idp.url, "/ui/login/register/org").status_code == 404:
            break
        time.sleep(0.2)
    assert _page(idp.url, "/ui/login/register/org").status_code == 404
    for params in ({}, {"orgID": tenant.org}):
        page = _page(idp.url, "/ui/login/register", **params)
        assert 'type="password"' not in page.text, params
    # A new organisation inherits the default; the machine users still get tokens although a second factor is required.
    inherited = idp.call("GET", "/management/v1/policies/login", org=tenant.org, key=real.provisioner_key)
    assert inherited["isDefault"] is True
    assert not inherited["policy"].get("allowRegister", False) and inherited["policy"]["forceMfa"] is True
    assert idp.call("GET", "/management/v1/orgs/me", org=tenant.org, key=real.members_key)["org"]["id"] == tenant.org


def _eventually(read, check, *, seconds: float = 30):
    """What `read` returns once `check` accepts it: ZITADEL answers a write before its read model shows it."""
    deadline = time.monotonic() + seconds
    while not check(found := read()):
        assert time.monotonic() < deadline, f"the read model never showed the expected state: {found}"
        time.sleep(0.2)
    return found


def test_the_operator_organisations_custom_policy_gets_the_second_factor_and_the_rollback_takes_it_back(real):
    idp = real.idp
    custom = {
        "allowUsernamePassword": True, "allowRegister": False, "allowDomainDiscovery": True, "hidePasswordReset": True,
        "passwordCheckLifetime": "864000s", "externalLoginCheckLifetime": "864000s", "mfaInitSkipLifetime": "2592000s",
        "secondFactorCheckLifetime": "64800s", "multiFactorCheckLifetime": "43200s",
    }
    idp.call("POST", "/management/v1/policies/login", custom, org=real.org)
    read = lambda: idp.call("GET", "/management/v1/policies/login", org=real.org)  # noqa: E731
    _eventually(read, lambda r: not r.get("isDefault"))
    idp.cli("--instance-policy")
    policy = _eventually(read, lambda r: r["policy"].get("forceMfa") and r["policy"].get("secondFactors"))["policy"]
    assert policy["forceMfa"] is True and policy["hidePasswordReset"] is True
    assert policy["allowDomainDiscovery"] is True and not policy.get("allowRegister", False)
    # A dry run only reads: wait until everything the CLI reads shows the first run, then run it for real once.
    _eventually(lambda: idp.cli("--instance-policy", "--dry-run"), lambda text: "would change nothing" in text)
    assert "nothing to change" in idp.cli("--instance-policy")
    idp.cli("--instance-policy", "--no-force-mfa")
    policy = _eventually(read, lambda r: not r["policy"].get("forceMfa", False))["policy"]
    assert not policy.get("allowRegister", False)


def _accept(url: str, user: str, org: str, login: str, code: str) -> None:
    """What a person does with the link: open it, type a password, submit the form."""
    with httpx2.Client(base_url=url, follow_redirects=True, timeout=30) as browser:
        page = browser.get("/ui/login/user/invite", params={"userID": user, "code": code, "orgID": org, "loginname": login})
        assert page.status_code == 200 and 'name="passwordconfirm"' in page.text
        hidden = {
            name: html.unescape(value)
            for name, value in re.findall(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page.text)
        }
        browser.post(
            "/ui/login/user/invite", data={**hidden, "code": code, "password": PASSWORD, "passwordconfirm": PASSWORD},
            headers={"Origin": url, "Referer": str(page.url)},
        )


def test_the_invite_link_sets_a_password_without_smtp_and_only_the_newest_code_works(real, tenant):
    idp = real.idp
    user = idp.call("POST", "/v2/users/human", {
        "organization": {"orgId": tenant.org}, "username": "boss@acme.test",
        "profile": {"givenName": "b", "familyName": "b"}, "email": {"email": "boss@acme.test", "returnCode": {}},
    }, key=real.provisioner_key)["userId"]
    ask = lambda: idp.call("POST", f"/v2/users/{user}/invite_code", {"returnCode": {}}, key=real.members_key)["inviteCode"]  # noqa: E731
    old, new = ask(), ask()
    read = lambda: idp.call("GET", f"/v2/users/{user}")["user"]["human"]  # noqa: E731
    time.sleep(1)
    _accept(idp.url, user, tenant.org, "boss@acme.test", old)
    time.sleep(1)
    assert "passwordChanged" not in read()
    _accept(idp.url, user, tenant.org, "boss@acme.test", new)
    for _ in range(100):
        if "passwordChanged" in read():
            break
        time.sleep(0.2)
    assert "passwordChanged" in read() and read()["email"]["isVerified"] is True
    with pytest.raises(ZitadelApiError) as err:  # a user with a credential gets no further code
        ask()
    assert err.value.status == 400
