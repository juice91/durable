"""The bootstrap CLI against the fake Management API: creates everything, a second run writes nothing, and the
generated client ids and the Access secret reach stdout only. The Cloudflare Access application exists only on request
(`--cloudflare-access`)."""

import io
import logging

import pytest

from eap_iam_zitadel.bootstrap import PROJECT_NAME, amain

from .fakes import FakeZitadel, machine_key_json

ARGS = ["--issuer", "https://auth.example.test", "--tenant", "studio"]
ACCESS = ("--cloudflare-access", "acme")


def run_cli(fake, *extra, caplog=None):
    import asyncio

    out = io.StringIO()
    code = asyncio.run(amain([*ARGS, *extra], env={"EAP_ZITADEL_MACHINE_KEY": machine_key_json()}, out=out, http=fake.client))
    return code, out.getvalue()


def apps(fake):
    return [a["name"] for found in fake.apps.values() for a in found]


def test_first_run_creates_project_roles_the_studio_app_and_the_admin_grant_and_nothing_for_cloudflare():
    fake = FakeZitadel()
    code, text = run_cli(fake)
    assert code == 0
    (project,) = fake.projects.values()
    assert project["name"] == PROJECT_NAME and project["projectRoleCheck"] and project["projectRoleAssertion"]
    assert [r["key"] for r in next(iter(fake.roles.values()))] == ["admin", "editor", "viewer"]
    (studio,) = next(iter(fake.apps.values()))
    assert studio["name"] == "Durable Studio"
    cfg = studio["oidcConfig"]
    assert cfg["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_NONE" and cfg["grantTypes"][0] == "OIDC_GRANT_TYPE_AUTHORIZATION_CODE"
    assert cfg["redirectUris"] == ["https://durable.example-org.ai/auth/callback", "http://127.0.0.1:13000/auth/callback"]
    assert cfg["postLogoutRedirectUris"] == ["https://durable.example-org.ai/", "http://127.0.0.1:13000/"]
    assert [(g["userId"], g["roleKeys"]) for g in fake.grants] == [("u-admin", ["admin"])]
    assert "EAP_ZITADEL_ORG_TENANT_MAP=org-1=studio" in text
    assert studio["oidcConfig"]["clientId"] in text
    assert "cloudflare" not in text.lower() and "secret-" not in text and "Client secret" not in text


@pytest.mark.parametrize("team", ["acme", "acme.cloudflareaccess.com"])
def test_with_the_flag_the_access_app_is_created_and_its_setup_printed(team):
    fake = FakeZitadel()
    code, text = run_cli(fake, "--cloudflare-access", team)
    assert code == 0
    studio, access = next(iter(fake.apps.values()))
    assert access["name"] == "Cloudflare Access"
    assert access["oidcConfig"]["redirectUris"] == ["https://acme.cloudflareaccess.com/cdn-cgi/access/callback"]
    assert access["oidcConfig"]["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_BASIC"
    assert "created application Cloudflare Access" in text and "Cloudflare Access identity provider" in text
    assert "https://acme.cloudflareaccess.com/cdn-cgi/access/callback" in text
    assert access["oidcConfig"]["clientId"] in text and "Client secret" in text and "secret-" in text


def test_the_flag_adds_the_access_app_to_a_set_up_instance_once():
    fake = FakeZitadel()
    run_cli(fake)
    code, text = run_cli(fake, *ACCESS)
    assert code == 0 and "created application Cloudflare Access" in text and "secret-" in text
    assert apps(fake) == ["Durable Studio", "Cloudflare Access"]
    code, text = run_cli(fake, *ACCESS)
    assert code == 0 and "nothing to change" in text and apps(fake) == ["Durable Studio", "Cloudflare Access"]


def test_a_run_without_the_flag_leaves_an_existing_access_app_alone():
    fake = FakeZitadel()
    run_cli(fake, *ACCESS)
    _, access = next(iter(fake.apps.values()))
    access["oidcConfig"]["redirectUris"] = ["https://old.cloudflareaccess.com/cdn-cgi/access/callback"]  # even drifted
    kept = {**access["oidcConfig"]}
    before = len(fake.mutations())
    for extra in ((), ("--dry-run",)):
        code, text = run_cli(fake, *extra)
        assert code == 0 and "cloudflare" not in text.lower()
    assert fake.mutations()[before:] == []  # neither updated nor deleted
    assert apps(fake) == ["Durable Studio", "Cloudflare Access"] and access["oidcConfig"] == kept


def test_the_environment_alone_does_not_ask_for_the_access_app():
    import asyncio

    fake = FakeZitadel()
    out = io.StringIO()
    env = {"EAP_ZITADEL_MACHINE_KEY": machine_key_json(), "EAP_CF_ACCESS_TEAM_DOMAIN": "acme.cloudflareaccess.com"}
    assert asyncio.run(amain(ARGS, env=env, out=out, http=fake.client)) == 0
    assert apps(fake) == ["Durable Studio"] and "cloudflare" not in out.getvalue().lower()


@pytest.mark.parametrize("args, message", [
    (("--cloudflare-access", ""), "team"),
    (("--cloudflare-access", "https://acme.cloudflareaccess.com"), "team"),
    (("--cloudflare-access", "sso.example.test"), "team"),
    (("--cloudflare-access",), "--cloudflare-access"),
    (("--rotate-access-secret",), "--rotate-access-secret needs --cloudflare-access"),
    (("--cloudflare-access", "acme", "--platform-users"), "--cloudflare-access"),
    (("--cloudflare-access", "acme", "--instance-policy"), "--cloudflare-access"),
])
def test_a_bad_cloudflare_option_is_a_usage_error_before_any_call(args, message, capsys):
    fake = FakeZitadel()
    with pytest.raises(SystemExit) as stop:
        run_cli(fake, *args)
    assert stop.value.code == 2 and message in capsys.readouterr().err and fake.requests == []


def test_second_run_changes_nothing():
    fake = FakeZitadel()
    run_cli(fake, *ACCESS)
    before = len(fake.requests)
    code, text = run_cli(fake, *ACCESS)
    assert code == 0
    writes = [m for m in fake.requests[before:] if m[0] != "GET" and not m[1].endswith("/_search") and m[1] != "/oauth/v2/token"]
    assert writes == []
    assert "nothing to change" in text
    assert "secret-" not in text and "already issued" in text  # the secret is shown once only


def test_a_drifted_application_is_repaired_and_a_role_added_back():
    fake = FakeZitadel()
    run_cli(fake)
    (studio,) = next(iter(fake.apps.values()))
    studio["oidcConfig"]["redirectUris"] = ["https://old.example.test/cb"]
    fake.roles[next(iter(fake.roles))].pop()  # the viewer role is gone
    fake.grants[0]["roleKeys"] = ["viewer"]
    code, text = run_cli(fake)
    assert code == 0 and "updated application Durable Studio" in text and "created role viewer" in text
    assert studio["oidcConfig"]["redirectUris"][0] == "https://durable.example-org.ai/auth/callback"
    assert sorted(fake.grants[0]["roleKeys"]) == ["admin", "viewer"]


def test_rotate_the_access_secret_prints_a_new_one():
    fake = FakeZitadel()
    run_cli(fake, *ACCESS)
    code, text = run_cli(fake, *ACCESS, "--rotate-access-secret")
    assert code == 0 and "rotated-" in text


def test_the_secrets_never_reach_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeZitadel()
    _, text = run_cli(fake, *ACCESS)
    assert "secret-" in text and "secret-" not in caplog.text and "BEGIN" not in caplog.text and "mgmt-token" not in caplog.text


def test_an_unknown_admin_user_is_a_clear_failure(capsys):
    fake = FakeZitadel()
    code, text = run_cli(fake, "--admin-user", "nobody")
    assert code == 1 and text == "" and "--admin-user" in capsys.readouterr().err


def test_no_key_is_a_usage_error(capsys):
    import asyncio

    assert asyncio.run(amain(ARGS, env={}, out=io.StringIO())) == 2
    assert "machine-user key" in capsys.readouterr().err


@pytest.mark.parametrize("flag", [(), ACCESS], ids=["without", "with"])
def test_dry_run_on_an_empty_instance_reads_only_and_says_what_it_would_create(flag):
    fake = FakeZitadel()
    code, text = run_cli(fake, *flag, "--dry-run")
    assert code == 0
    assert fake.mutations() == []  # not one write reached ZITADEL
    assert not fake.projects and not fake.apps and not fake.grants
    assert "DRY RUN" in text and "ZITADEL v4.20.5" in text and "/management/v1" in text and "/v2/users/human" in text
    assert "GET   /management/v1/orgs/me -> 200" in text
    assert "POST  /management/v1/projects/_search -> 200" in text
    for would in ("create project", "create role admin", "create application Durable Studio", "grant admin to the bootstrap admin"):
        assert would in text, would
    assert ("create application Cloudflare Access" in text) is bool(flag)
    assert ("cloudflare" in text.lower()) is bool(flag)
    assert "secret-" not in text and "Client secret" not in text  # nothing was issued


def test_dry_run_shows_what_the_flag_would_add_to_a_set_up_instance():
    fake = FakeZitadel()
    run_cli(fake)
    before = len(fake.mutations())
    code, text = run_cli(fake, "--dry-run")
    assert code == 0 and "would change nothing" in text
    code, text = run_cli(fake, *ACCESS, "--dry-run")
    assert code == 0 and "create application Cloudflare Access" in text and "create application Durable Studio" not in text
    assert "/apps/oidc" in text and len(fake.mutations()) == before and apps(fake) == ["Durable Studio"]


def test_dry_run_on_a_set_up_instance_would_change_nothing_and_a_drift_is_reported():
    fake = FakeZitadel()
    run_cli(fake, *ACCESS)
    code, text = run_cli(fake, *ACCESS, "--dry-run")
    assert code == 0 and "would change nothing" in text
    studio, access = next(iter(fake.apps.values()))
    studio["oidcConfig"]["redirectUris"] = ["https://old.example.test/cb"]
    before = len(fake.mutations())
    code, text = run_cli(fake, *ACCESS, "--dry-run")
    assert "update application Durable Studio" in text and "update application Cloudflare Access" not in text
    # another team than the app's redirect URL: the difference is shown
    code, text = run_cli(fake, "--cloudflare-access", "other", "--dry-run")
    assert "update application Cloudflare Access" in text and len(fake.mutations()) == before
    assert studio["oidcConfig"]["redirectUris"] == ["https://old.example.test/cb"]  # still drifted: nothing written
    assert access["oidcConfig"]["redirectUris"] == ["https://acme.cloudflareaccess.com/cdn-cgi/access/callback"]


def test_an_api_error_names_the_call_and_the_status_and_no_secret(capsys):
    fake = FakeZitadel()
    fake.down = True
    code, _ = run_cli(fake, "--dry-run")
    err = capsys.readouterr().err
    assert code == 1
    assert "GET /management/v1/orgs/me -> HTTP 503" in err
    assert "mgmt-token" not in err and "BEGIN" not in err and "machine-1" not in err


def test_a_refused_machine_key_names_the_token_call(capsys):
    fake = FakeZitadel()
    fake.refuse_token = True
    code, _ = run_cli(fake)
    err = capsys.readouterr().err
    assert code == 1 and "POST /oauth/v2/token" in err and "HTTP 400" in err
    assert "secret detail" not in err and "BEGIN" not in err


def _puts(fake):
    return [m for m in fake.mutations() if m[0] == "PUT"]


def test_rerun_against_omitted_default_enums_writes_nothing():
    fake = FakeZitadel()
    run_cli(fake, *ACCESS)
    fake.omit_default_enums = True
    before = len(fake.requests)
    code, text = run_cli(fake, *ACCESS)
    assert code == 0 and "nothing to change" in text
    assert [m for m in fake.requests[before:] if m[0] == "PUT"] == []


def test_rotate_on_a_rerun_reaches_generate_client_secret():
    fake = FakeZitadel()
    run_cli(fake, *ACCESS)
    fake.omit_default_enums = True
    before = len(fake.requests)
    code, text = run_cli(fake, *ACCESS, "--rotate-access-secret")
    assert code == 0 and "rotated-" in text
    assert any(p.endswith("/_generate_client_secret") for _, p in fake.requests[before:])
    assert _puts(fake) == []


def test_a_no_changes_400_on_put_is_treated_as_up_to_date():
    fake = FakeZitadel()
    run_cli(fake, *ACCESS)
    studio, _ = next(iter(fake.apps.values()))
    studio["oidcConfig"]["redirectUris"] = ["https://old.example.test/cb"]  # drift, so a PUT is attempted
    fake.reject_no_op_put = True
    code, text = run_cli(fake, *ACCESS, "--rotate-access-secret")
    assert code == 0 and "updated application" not in text and "rotated-" in text


def test_the_admin_is_found_by_user_name_or_login_local_part():
    fake = FakeZitadel()
    assert run_cli(fake, "--admin-user", "admin")[0] == 0  # userName
    fake = FakeZitadel()
    fake.users["u-admin"]["userName"] = "admin@durable.auth.example.test"  # userName is the full login
    assert run_cli(fake, "--admin-user", "admin")[0] == 0
