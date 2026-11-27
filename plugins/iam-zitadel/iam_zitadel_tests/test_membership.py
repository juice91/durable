"""The membership source against a fake Management API: list, invite, change role, remove, the last-admin guard."""

import asyncio

import pytest
from eap_iam import MembershipError, PluginContext

from eap_iam_zitadel import create_membership

from .fakes import ENV, ORG, TENANT, FakeZitadel, machine_key_json


def source(fake: FakeZitadel, **env):
    return create_membership(PluginContext(env={**ENV, "EAP_ZITADEL_MACHINE_KEY": machine_key_json(), **env}), http=fake.client)


def run(coro):
    return asyncio.run(coro)


def seeded():
    fake = FakeZitadel()
    fake.add_member("u-1", "ada@example.test", "admin", name="Ada")
    fake.add_member("u-2", "bob@example.test", "editor", name="Bob")
    fake.add_member("u-3", "cy@example.test", "viewer")
    return fake


def test_lists_members_with_roles_sorted_by_email():
    members = run(source(seeded()).list_members(TENANT))
    assert [(m.email, m.roles, m.display_name) for m in members] == [
        ("ada@example.test", ("admin",), "Ada"), ("bob@example.test", ("editor",), "Bob"), ("cy@example.test", ("viewer",), "cy@example.test"),
    ]


def test_the_search_is_scoped_to_the_tenants_org_and_project():
    fake = seeded()
    run(source(fake).list_members(TENANT))
    assert ("POST", "/management/v1/users/grants/_search") in fake.requests


def test_a_tenant_without_an_org_is_not_found():
    with pytest.raises(MembershipError) as err:
        run(source(seeded()).list_members("other"))
    assert err.value.code == "not_found"


def test_roles_of_by_email_or_id():
    s = source(seeded())
    assert run(s.roles_of(TENANT, "BOB@example.test")) == frozenset({"editor"})
    assert run(s.roles_of(TENANT, "u-1")) == frozenset({"admin"})
    assert run(s.roles_of(TENANT, "zitadel:u-2")) == frozenset({"editor"})  # a principal subject
    assert run(s.roles_of(TENANT, "stranger@example.test")) == frozenset()


def test_change_role():
    fake = seeded()
    member = run(source(fake).set_role(TENANT, "u-3", "editor"))
    assert member.roles == ("editor",)
    assert run(source(fake).roles_of(TENANT, "u-3")) == frozenset({"editor"})


def test_the_last_admin_cannot_be_demoted_or_removed():
    fake = seeded()
    s = source(fake)
    for call in (lambda: s.set_role(TENANT, "u-1", "editor"), lambda: s.remove(TENANT, "u-1")):
        with pytest.raises(MembershipError) as err:
            run(call())
        assert err.value.code == "last_admin"
    assert fake.mutations() == []  # refused before any write
    assert run(s.roles_of(TENANT, "u-1")) == frozenset({"admin"})


def test_with_a_second_admin_the_first_can_be_demoted_and_removed():
    fake = seeded()
    s = source(fake)
    run(s.set_role(TENANT, "u-2", "admin"))
    run(s.set_role(TENANT, "u-1", "viewer"))
    run(s.remove(TENANT, "u-1"))
    assert [m.email for m in run(s.list_members(TENANT))] == ["bob@example.test", "cy@example.test"]
    with pytest.raises(MembershipError) as err:  # bob is the only admin now
        run(s.remove(TENANT, "u-2"))
    assert err.value.code == "last_admin"


def test_unknown_member_and_bad_role():
    s = source(seeded())
    with pytest.raises(MembershipError) as err:
        run(s.set_role(TENANT, "nobody", "viewer"))
    assert err.value.code == "not_found"
    with pytest.raises(MembershipError) as err:
        run(s.set_role(TENANT, "u-2", "owner"))
    assert err.value.code == "invalid"
    with pytest.raises(MembershipError) as err:
        run(s.invite(TENANT, "not-an-email", "viewer"))
    assert err.value.code == "invalid"


def test_invite_a_new_user_creates_the_user_and_the_grant():
    fake = seeded()
    member = run(source(fake).invite(TENANT, "New@Example.test", "editor"))
    assert (member.email, member.roles, member.status) == ("new@example.test", ("editor",), "invited")
    assert fake.invited[0]["organization"] == {"orgId": ORG} and fake.invited[0]["email"]["email"] == "new@example.test"
    assert "new@example.test" in [m.email for m in run(source(fake).list_members(TENANT))]


def test_invite_an_existing_org_user_only_adds_the_grant_and_a_member_conflicts():
    fake = seeded()
    fake.users["u-9"] = {"id": "u-9", "userName": "dee@example.test", "human": {"email": {"email": "dee@example.test"}}}
    member = run(source(fake).invite(TENANT, "dee@example.test", "viewer"))
    assert member.status == "active" and not fake.invited
    with pytest.raises(MembershipError) as err:
        run(source(fake).invite(TENANT, "dee@example.test", "viewer"))
    assert err.value.code == "conflict"


def test_zitadel_down_is_unavailable_and_leaks_no_key():
    fake = seeded()
    s = source(fake)
    fake.down = True
    with pytest.raises(MembershipError) as err:
        run(s.list_members(TENANT))
    assert err.value.code == "unavailable" and "BEGIN" not in err.value.detail


def test_without_the_machine_key_the_source_is_not_configured():
    s = create_membership(PluginContext(env=dict(ENV)))
    assert (s.configured, s.mutable) == (False, False)
    with pytest.raises(MembershipError) as err:
        run(s.list_members(TENANT))
    assert err.value.code == "unavailable"


def test_dormant_when_nothing_is_set():
    s = create_membership(PluginContext(env={}))
    assert s.configured is False


def test_the_management_token_is_fetched_once_and_cached():
    fake = seeded()
    s = source(fake)
    run(s.list_members(TENANT))
    run(s.list_members(TENANT))
    assert sum(1 for _, p in fake.requests if p == "/oauth/v2/token") == 1


def test_an_unconfigured_membership_is_left_out_of_the_capabilities():
    from eap_iam import IamRuntime, IamSettings, IdentityChain
    from eap_iam.testing import FakePolicyEngine

    settings = IamSettings(mode="dev", identity=(), policy="fake", membership="zitadel", allow_transitional=True)
    for membership, expected in ((create_membership(PluginContext(env=dict(ENV))), None), (source(seeded()), {"name": "zitadel", "mutable": True})):
        runtime = IamRuntime(settings=settings, providers=(), chain=IdentityChain(()), policy=FakePolicyEngine(), membership=membership)
        assert runtime.describe()["membership"] == expected


# ---- F3: on the operator organisation the platform may grant roles but not create users -----------------------------

NO_CREATE = ["org.read", "user.read", "user.grant.read", "user.grant.write"]


def test_the_source_says_whether_it_can_create_users_and_asks_once():
    fake = seeded()
    s = source(fake)
    assert run(s.can_create_users(TENANT)) is True
    fake.permissions[ORG] = NO_CREATE
    assert run(source(fake).can_create_users(TENANT)) is False
    asked = len([p for _, p in fake.requests if p.endswith("/permissions/zitadel/me/_search")])
    assert run(s.can_create_users(TENANT)) is True  # cached
    assert len([p for _, p in fake.requests if p.endswith("/permissions/zitadel/me/_search")]) == asked


def test_inviting_a_new_address_without_the_right_to_create_says_what_to_do_and_writes_nothing():
    fake = seeded()
    fake.permissions[ORG] = NO_CREATE
    with pytest.raises(MembershipError) as err:
        run(source(fake).invite(TENANT, "new@example.test", "viewer"))
    assert err.value.code == "no_user_creation"
    assert "ZITADEL console" in err.value.detail and "add" in err.value.detail
    assert not [p for _, p in fake.mutations() if "permissions" not in p]


def test_a_user_created_in_the_console_can_still_be_added():
    fake = seeded()
    fake.permissions[ORG] = NO_CREATE
    fake.users["u-9"] = {"id": "u-9", "userName": "nia", "human": {"email": {"email": "nia@example.test"}}}
    member = run(source(fake).invite(TENANT, "nia@example.test", "editor"))
    assert (member.id, member.roles, member.status) == ("u-9", ("editor",), "active")


def test_a_refused_creation_is_the_same_clear_answer_even_when_the_permissions_cannot_be_read():
    fake = seeded()
    original = fake.route

    def route(method, path, body, org):
        import httpx2

        if path.endswith("/permissions/zitadel/me/_search"):
            return httpx2.Response(500, json={"message": "down"})
        if path == "/v2/users/human":
            return httpx2.Response(403, json={"message": "No matching permissions found"})
        return original(method, path, body, org)

    fake.route = route
    s = source(fake)
    assert run(s.can_create_users(TENANT)) is True  # unknown: the invite itself will say
    with pytest.raises(MembershipError) as err:
        run(s.invite(TENANT, "new@example.test", "viewer"))
    assert err.value.code == "no_user_creation"
