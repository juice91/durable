"""The bootstrap CLI against the fake Management API: creates everything, a second run writes nothing, and the
generated client ids and the Access secret reach stdout only."""

import io
import logging

from eap_iam_zitadel.bootstrap import PROJECT_NAME, amain

from .fakes import FakeZitadel, machine_key_json

ARGS = ["--issuer", "https://auth.example.test", "--tenant", "studio", "--access-team", "acme"]


def run_cli(fake, *extra, caplog=None):
    import asyncio

    out = io.StringIO()
    code = asyncio.run(amain([*ARGS, *extra], env={"EAP_ZITADEL_MACHINE_KEY": machine_key_json()}, out=out, http=fake.client))
    return code, out.getvalue()


def test_first_run_creates_project_roles_apps_and_the_admin_grant():
    fake = FakeZitadel()
    code, text = run_cli(fake)
    assert code == 0
    (project,) = fake.projects.values()
    assert project["name"] == PROJECT_NAME and project["projectRoleCheck"] and project["projectRoleAssertion"]
    assert [r["key"] for r in next(iter(fake.roles.values()))] == ["admin", "editor", "viewer"]
    studio, access = next(iter(fake.apps.values()))
    cfg = studio["oidcConfig"]
    assert cfg["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_NONE" and cfg["grantTypes"][0] == "OIDC_GRANT_TYPE_AUTHORIZATION_CODE"
    assert cfg["redirectUris"] == ["https://durable.example-org.ai/auth/callback", "http://127.0.0.1:13000/auth/callback"]
    assert cfg["postLogoutRedirectUris"] == ["https://durable.example-org.ai/", "http://127.0.0.1:13000/"]
    assert access["oidcConfig"]["redirectUris"] == ["https://acme.cloudflareaccess.com/cdn-cgi/access/callback"]
    assert access["oidcConfig"]["authMethodType"] == "OIDC_AUTH_METHOD_TYPE_BASIC"
    assert [(g["userId"], g["roleKeys"]) for g in fake.grants] == [("u-admin", ["admin"])]
    assert "EAP_ZITADEL_ORG_TENANT_MAP=org-1=studio" in text
    assert studio["oidcConfig"]["clientId"] in text and "Client secret" in text and "secret-" in text


def test_second_run_changes_nothing():
    fake = FakeZitadel()
    run_cli(fake)
    before = len(fake.requests)
    code, text = run_cli(fake)
    assert code == 0
    writes = [m for m in fake.requests[before:] if m[0] != "GET" and not m[1].endswith("/_search") and m[1] != "/oauth/v2/token"]
    assert writes == []
    assert "nothing to change" in text
    assert "secret-" not in text and "already issued" in text  # the secret is shown once only


def test_a_drifted_application_is_repaired_and_a_role_added_back():
    fake = FakeZitadel()
    run_cli(fake)
    studio, _ = next(iter(fake.apps.values()))
    studio["oidcConfig"]["redirectUris"] = ["https://old.example.test/cb"]
    fake.roles[next(iter(fake.roles))].pop()  # the viewer role is gone
    fake.grants[0]["roleKeys"] = ["viewer"]
    code, text = run_cli(fake)
    assert code == 0 and "updated application Durable Studio" in text and "created role viewer" in text
    assert studio["oidcConfig"]["redirectUris"][0] == "https://durable.example-org.ai/auth/callback"
    assert sorted(fake.grants[0]["roleKeys"]) == ["admin", "viewer"]


def test_rotate_the_access_secret_prints_a_new_one():
    fake = FakeZitadel()
    run_cli(fake)
    code, text = run_cli(fake, "--rotate-access-secret")
    assert code == 0 and "rotated-" in text


def test_the_secrets_never_reach_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeZitadel()
    _, text = run_cli(fake)
    assert "secret-" in text and "secret-" not in caplog.text and "BEGIN" not in caplog.text and "mgmt-token" not in caplog.text


def test_an_unknown_admin_user_is_a_clear_failure(capsys):
    fake = FakeZitadel()
    code, text = run_cli(fake, "--admin-user", "nobody")
    assert code == 1 and text == "" and "--admin-user" in capsys.readouterr().err


def test_no_key_is_a_usage_error(capsys):
    import asyncio

    assert asyncio.run(amain(ARGS, env={}, out=io.StringIO())) == 2
    assert "machine-user key" in capsys.readouterr().err


def test_dry_run_on_an_empty_instance_reads_only_and_says_what_it_would_create(caplog):
    fake = FakeZitadel()
    code, text = run_cli(fake, "--dry-run")
    assert code == 0
    assert fake.mutations() == []  # not one write reached ZITADEL
    assert not fake.projects and not fake.apps and not fake.grants
    assert "DRY RUN" in text and "ZITADEL v4.20.5" in text and "/management/v1" in text and "/v2/users/human" in text
    assert "GET   /management/v1/orgs/me -> 200" in text
    assert "POST  /management/v1/projects/_search -> 200" in text
    for would in ("create project", "create role admin", "create application Durable Studio", "create application Cloudflare Access", "grant admin to the bootstrap admin"):
        assert would in text, would
    assert "secret-" not in text and "Client secret" not in text  # nothing was issued


def test_dry_run_on_a_set_up_instance_would_change_nothing_and_a_drift_is_reported():
    fake = FakeZitadel()
    run_cli(fake)
    code, text = run_cli(fake, "--dry-run")
    assert code == 0 and "would change nothing" in text
    studio, _ = next(iter(fake.apps.values()))
    studio["oidcConfig"]["redirectUris"] = ["https://old.example.test/cb"]
    before = len(fake.mutations())
    code, text = run_cli(fake, "--dry-run")
    assert "update application Durable Studio" in text and len(fake.mutations()) == before
    assert studio["oidcConfig"]["redirectUris"] == ["https://old.example.test/cb"]  # still drifted: nothing written


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
    run_cli(fake)
    fake.omit_default_enums = True
    before = len(fake.requests)
    code, text = run_cli(fake)
    assert code == 0 and "nothing to change" in text
    assert [m for m in fake.requests[before:] if m[0] == "PUT"] == []


def test_rotate_on_a_rerun_reaches_generate_client_secret():
    fake = FakeZitadel()
    run_cli(fake)
    fake.omit_default_enums = True
    before = len(fake.requests)
    code, text = run_cli(fake, "--rotate-access-secret")
    assert code == 0 and "rotated-" in text
    assert any(p.endswith("/_generate_client_secret") for _, p in fake.requests[before:])
    assert _puts(fake) == []


def test_a_no_changes_400_on_put_is_treated_as_up_to_date():
    fake = FakeZitadel()
    run_cli(fake)
    studio, _ = next(iter(fake.apps.values()))
    studio["oidcConfig"]["redirectUris"] = ["https://old.example.test/cb"]  # drift, so a PUT is attempted
    fake.reject_no_op_put = True
    code, text = run_cli(fake, "--rotate-access-secret")
    assert code == 0 and "updated application" not in text and "rotated-" in text


def test_the_admin_is_found_by_user_name_or_login_local_part():
    fake = FakeZitadel()
    assert run_cli(fake, "--admin-user", "admin")[0] == 0  # userName
    fake = FakeZitadel()
    fake.users["u-admin"]["userName"] = "admin@durable.auth.example.test"  # userName is the full login
    assert run_cli(fake, "--admin-user", "admin")[0] == 0
