"""Configuration and the password policy. No database."""

from __future__ import annotations

import secrets

import pytest
from eap_iam import IamConfigError, PluginContext

from eap_iam_localadmin import create_identity, create_membership
from eap_iam_localadmin.passwords import Passwords, policy_problem
from eap_iam_localadmin.settings import load_account_settings, load_settings
from eap_iam_localadmin.tokens import setup_code_line

KEY = secrets.token_hex(32)
ENV = {"EAP_LOCAL_ADMIN_USERNAME": "admin", "EAP_LOCAL_AUTH_SIGNING_KEY": KEY}


def test_settings_default_to_the_studio_tenant_and_a_twelve_hour_session():
    s = load_settings(ENV)
    assert (s.tenant_id, s.username) == ("studio", "admin")
    assert s.session_hours == 12 and s.initial_password is None


@pytest.mark.parametrize(
    ("change", "names"),
    [
        ({"EAP_LOCAL_ADMIN_USERNAME": "has space"}, "EAP_LOCAL_ADMIN_USERNAME"),
        ({"EAP_LOCAL_ADMIN_USERNAME": "a:b"}, "EAP_LOCAL_ADMIN_USERNAME"),
        ({"EAP_LOCAL_ADMIN_TENANT": "a/b"}, "EAP_LOCAL_ADMIN_TENANT"),
        ({"EAP_LOCAL_AUTH_SESSION_HOURS": "0"}, "EAP_LOCAL_AUTH_SESSION_HOURS"),
        ({"EAP_LOCAL_AUTH_SESSION_HOURS": "x"}, "EAP_LOCAL_AUTH_SESSION_HOURS"),
    ],
)
def test_a_listed_provider_refuses_to_start_when_half_configured(change, names):
    with pytest.raises(IamConfigError, match=names):
        create_identity(PluginContext(engine=None, env={**ENV, **change}))


WORDS = ("change", "Example", "DEFAULT", "secret", "placeholder", "xxx")
REFUSED_KEYS = {
    "missing": "",
    "blank": "   ",
    "short": secrets.token_urlsafe(48)[:42],
    "short hex": secrets.token_hex(31),
    "one character": "a" * 64,
    **{f"word {word}": word + secrets.token_hex(32) for word in WORDS},
}


@pytest.mark.parametrize("kind", sorted(REFUSED_KEYS))
def test_a_missing_weak_or_placeholder_key_refuses_to_start_and_says_how_to_make_one(kind):
    key = REFUSED_KEYS[kind]
    with pytest.raises(IamConfigError) as refused:
        create_identity(PluginContext(engine=None, env={**ENV, "EAP_LOCAL_AUTH_SIGNING_KEY": key}))
    message = str(refused.value)
    assert "openssl rand -base64 48" in message and "pydanticos-secrets" in message
    assert "kubectl -n pydanticos patch secret" in message  # one command to copy
    if key.strip():
        assert key not in message


@pytest.mark.parametrize("make", [lambda: secrets.token_hex(32), lambda: secrets.token_urlsafe(48)[:43].replace("x", "q").replace("X", "Q")])
def test_a_strong_key_is_accepted(make):
    load_settings({"EAP_LOCAL_AUTH_SIGNING_KEY": make()})


def test_the_key_may_come_from_a_file_and_the_variable_wins(tmp_path):
    from_file, from_env = secrets.token_hex(32), secrets.token_hex(32)
    path = tmp_path / "key"
    path.write_text(from_file + "\n")
    base = {"EAP_LOCAL_AUTH_SIGNING_KEY_FILE": str(path)}
    assert load_settings(base).signing_key == from_file.encode()
    assert load_settings({**base, "EAP_LOCAL_AUTH_SIGNING_KEY": ""}).signing_key == from_file.encode()
    assert load_settings({**base, "EAP_LOCAL_AUTH_SIGNING_KEY": from_env}).signing_key == from_env.encode()


@pytest.mark.parametrize("content", [None, "", "short", b"\xff\xfe" * 40])
def test_a_missing_unreadable_or_weak_key_file_refuses_to_start(tmp_path, content):
    path = tmp_path / "key"
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        path.write_text(content)
    with pytest.raises(IamConfigError, match="EAP_LOCAL_AUTH_SIGNING_KEY") as refused:
        load_settings({"EAP_LOCAL_AUTH_SIGNING_KEY_FILE": str(path)})
    assert "openssl rand -base64 48" in str(refused.value)


def test_no_setting_value_reaches_a_repr():
    s = load_settings({**ENV, "EAP_LOCAL_ADMIN_INITIAL_PASSWORD": "the-initial-password"})
    assert KEY not in repr(s) and "the-initial-password" not in repr(s)


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("elevenchars", "at least 12"),
        ("x" * 129, "at most 128"),
        ("Admin-account", "username"),
        ("ADMIN-ACCOUNT", "username"),
        ("twelve chars\ud800", "characters"),
    ],
)
def test_policy_refusals(password, problem):
    assert problem in policy_problem(password, "admin-account")


def test_policy_accepts_a_long_password():
    assert policy_problem("correct horse battery", "admin") is None


def test_hashes_are_argon2id_and_verify():
    passwords = Passwords()
    hashed = passwords.hash("correct horse battery")
    assert hashed.startswith("$argon2id$") and "correct" not in hashed
    assert passwords.verify(hashed, "correct horse battery")
    assert not passwords.verify(hashed, "correct horse batterz")
    assert not passwords.verify(None, "correct horse battery")  # no account: a dummy hash is verified, never a match
    assert not passwords.verify("not-a-hash", "correct horse battery")
    assert not passwords.verify(hashed, "x" * 100_000)
    assert not passwords.verify(hashed, "bad\ud800surrogate")


def test_the_membership_source_knows_exactly_the_admin():
    import asyncio

    from eap_iam import MembershipError

    source = create_membership(PluginContext(env=ENV))
    ask = lambda tenant, subject: asyncio.run(source.roles_of(tenant, subject))  # noqa: E731
    assert ask("studio", "localadmin:admin") == frozenset({"admin"})
    assert ask("other", "localadmin:admin") == frozenset()
    assert ask("studio", "localadmin:someone") == frozenset()
    assert ask("studio", "admin") == frozenset()
    members = asyncio.run(source.list_members("studio"))
    assert [(m.id, m.display_name, m.roles) for m in members] == [("admin", "admin", ("admin",))]
    assert asyncio.run(source.list_members("other")) == []
    assert source.name == "localadmin" and source.mutable is False
    for call in (source.invite("studio", "a@example.test", "viewer"), source.set_role("studio", "admin", "viewer"),
                 source.remove("studio", "admin")):
        with pytest.raises(MembershipError):
            asyncio.run(call)


def test_the_membership_source_needs_no_signing_key():
    assert create_membership(PluginContext(env={"EAP_LOCAL_ADMIN_USERNAME": "admin"})).name == "localadmin"


def test_the_account_values_are_optional_the_setup_page_makes_the_account():
    s = load_settings({"EAP_LOCAL_AUTH_SIGNING_KEY": KEY})
    assert s.username is None and s.initial_password is None
    import asyncio

    nobody = create_membership(PluginContext(env={}))
    assert asyncio.run(nobody.roles_of("studio", "localadmin:admin")) == frozenset()
    assert asyncio.run(nobody.list_members("studio")) == []


CODE = "ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567"


def test_the_setup_code_line_carries_the_link_when_studio_s_address_is_known():
    plain = setup_code_line(CODE)
    assert f"setup code: {CODE}" in plain and "Open " not in plain
    linked = setup_code_line(CODE, "http://localhost:3000")
    assert linked.startswith(plain) and "\n" not in linked
    assert linked.endswith(f"Open http://localhost:3000/setup#code={CODE}")


@pytest.mark.parametrize(("value", "expected"), [
    ("", None), ("http://localhost:3000/", "http://localhost:3000"), (" https://studio.example.test ", "https://studio.example.test"),
])
def test_studio_s_address_is_optional_and_loses_its_trailing_slash(value, expected):
    assert load_account_settings({"EAP_STUDIO_URL": value}).studio_url == expected


@pytest.mark.parametrize("value", ["localhost:3000", "http://a b", "https://x.test/#y", "https://x.test/?y=1", "ftp://x.test"])
def test_a_malformed_studio_address_refuses_to_start(value):
    with pytest.raises(IamConfigError, match="EAP_STUDIO_URL"):
        load_account_settings({"EAP_STUDIO_URL": value})
