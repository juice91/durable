"""`bootstrap --platform-users` and `--instance-policy` (tenant provisioning design 4.2, 4.3; F3, F5) against the fake:
dry run first, idempotent, keys printed once, the least role that works, and no credential in a log."""

import asyncio
import io
import json
import logging

import pytest
from eap_iam_zitadel.bootstrap import amain

from .fakes import ORG, FakeZitadel, machine_key_json

ARGS = ["--issuer", "https://auth.example.test"]


def ready() -> FakeZitadel:
    """An instance the first bootstrap has set up: the project `durable` is in the operator organisation."""
    fake = FakeZitadel()
    fake.projects["proj-1"] = {"id": "proj-1", "name": "durable"}
    return fake


def cli(fake, *args):
    out = io.StringIO()
    code = asyncio.run(amain([*ARGS, *args], env={"EAP_ZITADEL_MACHINE_KEY": machine_key_json()}, out=out, http=fake.client))
    return code, out.getvalue()


def user(fake, name):
    (found,) = [u for u in fake.users.values() if u["userName"] == name]
    return found["id"]


def printed_keys(text):
    """The key JSON lines of the output, by user name."""
    lines = text.splitlines()
    return {line.split()[0]: json.loads(lines[i + 1]) for i, line in enumerate(lines) if "key (shown once" in line}


# ---- --platform-users ----------------------------------------------------------------------------------------------


def test_platform_users_creates_both_with_the_least_membership_that_works_and_prints_each_key_once():
    fake = ready()
    code, text = cli(fake, "--platform-users")
    assert code == 0
    provisioner, members = user(fake, "platform-provisioner"), user(fake, "platform-members")
    assert fake.iam_members[provisioner] == ["IAM_ORG_MANAGER"]
    # F3: on the operator organisation the API's key may write grants only. ORG_USER_MANAGER there could mint a key for
    # the instance owner (test_contract.py shows both on a real ZITADEL).
    assert fake.org_members[ORG] == {members: ["ORG_USER_PERMISSION_EDITOR"]}
    assert members not in fake.iam_members
    keys = printed_keys(text)
    assert set(keys) == {"platform-provisioner", "platform-members"}
    assert keys["platform-members"]["userId"] == members and len(fake.keys[members]) == 1
    assert "EAP_ZITADEL_MACHINE_KEY" in text and text.count("FAKE-PRIVATE") == 2


def test_a_second_run_writes_nothing_and_shows_no_key():
    fake = ready()
    cli(fake, "--platform-users")
    before = len(fake.mutations())
    code, text = cli(fake, "--platform-users")
    assert code == 0 and fake.mutations()[before:] == []
    assert "FAKE-PRIVATE" not in text and "not shown" in text and "nothing to change" in text


def test_the_platform_users_dry_run_reads_only():
    fake = ready()
    code, text = cli(fake, "--platform-users", "--dry-run")
    assert code == 0 and fake.mutations() == [] and not fake.keys and set(fake.iam_members) == {"machine-1"}
    for would in ("create the machine user platform-provisioner", "create the machine user platform-members",
                  "IAM_ORG_MANAGER", "ORG_USER_PERMISSION_EDITOR", "create a key for platform-members"):
        assert would in text, would
    assert "DRY RUN" in text and "FAKE-PRIVATE" not in text


def test_too_wide_a_membership_is_narrowed():
    fake = ready()
    cli(fake, "--platform-users")
    members, provisioner = user(fake, "platform-members"), user(fake, "platform-provisioner")
    fake.org_members[ORG][members] = ["ORG_USER_MANAGER"]
    fake.iam_members[members] = ["IAM_OWNER"]
    fake.iam_members[provisioner] = ["IAM_OWNER"]
    code, text = cli(fake, "--platform-users", "--dry-run")
    assert "remove the instance membership of platform-members" in text and fake.iam_members[members] == ["IAM_OWNER"]
    code, text = cli(fake, "--platform-users")
    assert code == 0 and members not in fake.iam_members
    assert fake.org_members[ORG][members] == ["ORG_USER_PERMISSION_EDITOR"]
    assert fake.iam_members[provisioner] == ["IAM_ORG_MANAGER"]


def test_rotating_a_key_prints_a_new_one_and_names_the_old_one_to_delete():
    fake = ready()
    cli(fake, "--platform-users")
    members = user(fake, "platform-members")
    old = fake.keys[members][0]["id"]
    code, text = cli(fake, "--platform-users", "--rotate-key", "platform-members")
    assert code == 0 and len(fake.keys[members]) == 2 and set(printed_keys(text)) == {"platform-members"}
    assert old in text and "delete" in text


def test_rotate_key_accepts_only_the_two_platform_users(capsys):
    with pytest.raises(SystemExit):
        cli(ready(), "--platform-users", "--rotate-key", "platform-automation")


def test_platform_users_refuses_a_key_of_another_organisation(capsys):
    fake = FakeZitadel()  # the caller's organisation has no project `durable`
    code, _ = cli(fake, "--platform-users")
    assert code == 1 and "project" in capsys.readouterr().err and fake.mutations() == []


def test_a_human_with_a_platform_user_name_is_refused(capsys):
    fake = ready()
    fake.users["u-x"] = {"id": "u-x", "userName": "platform-members", "human": {"email": {"email": "x@example.test"}}}
    code, _ = cli(fake, "--platform-users")
    assert code == 1 and "not a machine user" in capsys.readouterr().err and not fake.keys


def test_a_key_already_issued_is_printed_even_when_a_later_step_fails(capsys):
    fake = ready()
    original = fake.route
    calls = []

    def route(method, path, body, org):
        if path.endswith("/keys") and method == "POST":
            calls.append(path)
            if len(calls) == 2:
                import httpx2

                return httpx2.Response(503, json={"message": "down"})
        return original(method, path, body, org)

    fake.route = route
    code, text = cli(fake, "--platform-users")
    assert code == 1 and text.count("FAKE-PRIVATE") == 1 and "HTTP 503" in capsys.readouterr().err


def test_no_key_reaches_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    _, text = cli(ready(), "--platform-users")
    assert "FAKE-PRIVATE" in text and "FAKE-PRIVATE" not in caplog.text and "mgmt-token" not in caplog.text


# ---- --instance-policy ---------------------------------------------------------------------------------------------


def test_instance_policy_turns_registration_off_and_requires_a_second_factor():
    fake = ready()
    before = dict(fake.default_policy)
    code, text = cli(fake, "--instance-policy")
    assert code == 0
    after = fake.default_policy
    assert (after["allowRegister"], after["allowExternalIdp"], after["allowDomainDiscovery"], after["forceMfa"]) == (False, False, False, True)
    # everything else is as it was
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {"allowRegister", "allowExternalIdp", "allowDomainDiscovery", "forceMfa"}
    assert fake.restrictions == {"disallowPublicOrgRegistration": True}
    assert "public organisation registration" in text and "second factor" in text
    assert fake.org_policies == {}  # no custom policy: nothing more to do


def test_a_second_instance_policy_run_writes_nothing():
    fake = ready()
    cli(fake, "--instance-policy")
    before = len(fake.mutations())
    code, text = cli(fake, "--instance-policy")
    assert code == 0 and fake.mutations()[before:] == [] and "nothing to change" in text


def test_the_instance_policy_dry_run_reads_only():
    fake = ready()
    code, text = cli(fake, "--instance-policy", "--dry-run")
    assert code == 0 and fake.mutations() == [] and fake.default_policy["allowRegister"] and not fake.restrictions
    assert "DRY RUN" in text and "public organisation registration" in text and "PUT /admin/v1/policies/login" in text


def test_a_second_factor_is_added_before_one_is_required():
    fake = ready()
    fake.default_factors.clear()
    code, _ = cli(fake, "--instance-policy")  # the fake fails the run if MFA is forced while there is no factor
    assert code == 0 and fake.default_factors == ["SECOND_FACTOR_TYPE_OTP", "SECOND_FACTOR_TYPE_U2F"]


def test_the_operator_organisations_custom_policy_keeps_its_values_and_requires_a_second_factor():
    fake = ready()
    custom = {**fake.default_policy, "allowRegister": False, "allowDomainDiscovery": True, "hidePasswordReset": True}
    fake.org_policies[ORG] = dict(custom)
    fake.org_factors[ORG] = []
    code, _ = cli(fake, "--instance-policy")
    assert code == 0 and fake.org_factors[ORG]
    assert fake.org_policies[ORG] == {**custom, "forceMfa": True}


def test_self_registration_left_on_in_the_custom_policy_is_turned_off():
    fake = ready()
    fake.org_policies[ORG] = {**fake.default_policy, "forceMfa": True}
    fake.org_factors[ORG] = ["SECOND_FACTOR_TYPE_OTP"]
    cli(fake, "--instance-policy")
    assert fake.org_policies[ORG]["allowRegister"] is False


def test_no_force_mfa_turns_the_requirement_off_and_keeps_registration_off():
    fake = ready()
    fake.org_policies[ORG] = dict(fake.default_policy)
    fake.org_factors[ORG] = ["SECOND_FACTOR_TYPE_OTP"]
    cli(fake, "--instance-policy")
    code, _ = cli(fake, "--instance-policy", "--no-force-mfa")
    assert code == 0
    for policy in (fake.default_policy, fake.org_policies[ORG]):
        assert policy["forceMfa"] is False and policy["allowRegister"] is False
    assert fake.restrictions["disallowPublicOrgRegistration"] is True


def test_both_modes_run_together_and_neither_touches_the_project_or_apps():
    fake = ready()
    code, text = cli(fake, "--instance-policy", "--platform-users")
    assert code == 0 and "FAKE-PRIVATE" in text and fake.default_policy["forceMfa"]
    assert not any("/projects" in path and not path.endswith("/_search") for _, path in fake.mutations())
    assert fake.apps == {} and fake.grants == []


def test_an_api_error_names_the_call_and_status(capsys):
    fake = ready()
    fake.down = True
    code, _ = cli(fake, "--instance-policy")
    err = capsys.readouterr().err
    assert code == 1 and "HTTP 503" in err and "mgmt-token" not in err
