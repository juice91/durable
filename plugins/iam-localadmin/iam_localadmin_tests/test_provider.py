"""The provider against its own tables: provisioning, the forced change, tokens and sessions."""

from __future__ import annotations

import asyncio
import logging
import secrets

import jwt
import pytest
from eap_iam import AuthError, IamConfigError, PasswordSignInProvider, PluginContext, RequestView, SignInError
from sqlalchemy import text

from eap_iam_localadmin import create_identity

from .conftest import CHOSEN, ENV, INITIAL, KEY

IP = "192.0.2.1"


def ask(provider, token: str | None, accepts=frozenset({"user"})):
    headers = {} if token is None else {"authorization": f"Bearer {token}"}
    return asyncio.run(provider.authenticate(RequestView(headers=headers, peer_host=IP, accepts=accepts)))


def account(engine) -> dict:
    with engine.connect() as c:
        return dict(c.execute(text("SELECT * FROM iam_localadmin_account")).mappings().one())


def refusal(call) -> SignInError:
    with pytest.raises(SignInError) as refused:
        call()
    return refused.value


# --- start-up --------------------------------------------------------------------------------------------------------

def test_without_its_tables_it_refuses_to_start_and_says_how_to_make_them(database):
    with pytest.raises(IamConfigError, match="eap-db plugins upgrade"):
        create_identity(PluginContext(engine=database[0], env=ENV))


def test_the_first_start_stores_an_argon2id_hash_and_warns(start, engine, caplog):
    with caplog.at_level(logging.WARNING):
        provider = start()
    assert isinstance(provider, PasswordSignInProvider)
    row = account(engine)
    assert row["password_hash"].startswith("$argon2id$") and INITIAL not in str(row)
    assert row["must_change"] is True and row["username"] == "admin" and row["tenant_id"] == "studio"
    assert "initial password" in caplog.text and INITIAL not in caplog.text


def test_production_with_the_initial_password_starts_and_warns_loudly(start, caplog):
    """Refusing would make the first change impossible: the account only exists once the API runs."""
    with caplog.at_level(logging.WARNING):
        start(EAP_AUTH_MODE="production")
    assert any(r.levelno >= logging.ERROR and "initial password" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("weak", ["short", "admin"])
def test_a_weak_initial_password_refuses_to_start_without_echoing_it(start, weak):
    with pytest.raises(IamConfigError, match="EAP_LOCAL_ADMIN_INITIAL_PASSWORD") as refused:
        start(EAP_LOCAL_ADMIN_INITIAL_PASSWORD=weak, EAP_LOCAL_ADMIN_USERNAME="administrator")
    if weak == "short":
        assert weak not in str(refused.value)


def test_before_the_first_change_a_new_initial_password_replaces_the_old_one(start):
    start()
    provider = start(EAP_LOCAL_ADMIN_INITIAL_PASSWORD="another-initial-0003")
    assert refusal(lambda: provider.sign_in("admin", INITIAL, IP)).code == "invalid_credentials"
    assert provider.sign_in("admin", "another-initial-0003", IP).tokens is None


def test_after_the_change_the_initial_password_is_ignored(start, signed_in, caplog):
    with caplog.at_level(logging.WARNING):
        provider = start(EAP_LOCAL_ADMIN_INITIAL_PASSWORD="another-initial-0003")
    assert "ignored" in caplog.text
    for stale in (INITIAL, "another-initial-0003"):
        assert refusal(lambda: provider.sign_in("admin", stale, IP)).code == "invalid_credentials"
    assert provider.sign_in("admin", CHOSEN, IP).tokens is not None
    assert start(EAP_LOCAL_ADMIN_INITIAL_PASSWORD="").sign_in("admin", CHOSEN, IP).tokens is not None


# --- sign-in and the forced change -----------------------------------------------------------------------------------

def test_the_initial_password_gets_no_token(start, engine):
    result = start().sign_in("admin", INITIAL, IP)
    assert result.tokens is None and result.subject == "localadmin:admin" and result.tenant_id == "studio"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM iam_localadmin_session")).scalar_one() == 0


def test_the_change_signs_in_as_the_tenant_admin(signed_in):
    provider, tokens = signed_in
    principal = ask(provider, tokens.access_token)
    assert (principal.tenant_id, principal.kind, principal.subject, principal.provider) == (
        "studio", "user", "localadmin:admin", "localadmin")
    assert principal.roles == frozenset({"admin"})
    assert tokens.expires_in == 900
    assert tokens.access_token not in repr(tokens) and tokens.refresh_token not in repr(tokens)


def test_the_configured_tenant_is_the_principals(start):
    provider = start(EAP_LOCAL_ADMIN_TENANT="acme")
    tokens = provider.change_password("admin", INITIAL, CHOSEN, IP).tokens
    assert ask(provider, tokens.access_token).tenant_id == "acme"
    other = start()  # the same database, tenant `studio`: another account, and acme's token is nobody there
    with pytest.raises(AuthError):
        ask(other, tokens.access_token)


@pytest.mark.parametrize(
    ("new", "problem"),
    [("short", "at least 12"), (INITIAL, "differ"), ("x" * 129, "at most 128")],
)
def test_a_new_password_must_satisfy_the_policy(start, engine, new, problem):
    provider = start()
    refused = refusal(lambda: provider.change_password("admin", INITIAL, new, IP))
    assert refused.code == "weak_password" and problem in refused.detail
    assert account(engine)["must_change"] is True


def test_the_username_is_no_password(start):
    provider = start(EAP_LOCAL_ADMIN_USERNAME="administrator")
    refused = refusal(lambda: provider.change_password("administrator", INITIAL, "Administrator", IP))
    assert refused.code == "weak_password" and "username" in refused.detail


def test_the_change_needs_the_current_password(start, engine):
    provider = start()
    refused = refusal(lambda: provider.change_password("admin", "wrong-password-000", CHOSEN, IP))
    assert refused.code == "invalid_credentials" and account(engine)["must_change"] is True


def test_wrong_username_and_wrong_password_answer_alike_and_do_one_hash_each(start, monkeypatch):
    provider = start()
    seen: list[str | None] = []
    real = provider.passwords.verify
    monkeypatch.setattr(provider.passwords, "verify", lambda hashed, pw: (seen.append(hashed), real(hashed, pw))[1])
    a = refusal(lambda: provider.sign_in("nobody", INITIAL, IP))
    b = refusal(lambda: provider.sign_in("admin", "wrong-password-000", IP))
    c = refusal(lambda: provider.sign_in("x" * 5000, "y" * 5000, IP))
    assert {(r.code, r.detail) for r in (a, b, c)} == {("invalid_credentials", "Invalid username or password")}
    assert len(seen) == 3  # exactly one verification per attempt, account or not
    assert a.subject == "localadmin:unknown" and b.subject == "localadmin:admin"
    assert "nobody" not in a.subject


# --- tokens and sessions ---------------------------------------------------------------------------------------------

def test_a_request_without_our_token_is_left_to_the_next_provider(signed_in):
    provider, _ = signed_in
    foreign = jwt.encode({"sub": "1"}, secrets.token_hex(32), algorithm="HS256")
    for token in (None, "dev:studio:operator", "pos_abc_def", foreign, "a.b.c", "x" * 20_000):
        assert ask(provider, token) is None


@pytest.mark.parametrize("tamper", ["key", "alg_none", "signature", "audience", "no_session", "tenant"])
def test_a_forged_token_is_refused(signed_in, tamper):
    provider, tokens = signed_in
    header = jwt.get_unverified_header(tokens.access_token)
    claims = jwt.decode(tokens.access_token, KEY, algorithms=["HS256"], audience="eap-api")
    key, alg = KEY, "HS256"
    if tamper == "key":
        key = secrets.token_hex(32)
    if tamper == "audience":
        claims["aud"] = "somewhere-else"
    if tamper == "no_session":
        claims["sid"] = "0" * 32
    if tamper == "tenant":
        claims["ten"] = "other"
    if tamper == "alg_none":
        key, alg = None, "none"
    forged = jwt.encode(claims, key, algorithm=alg, headers={"typ": header["typ"]})
    if tamper == "signature":
        forged = tokens.access_token[:-4] + ("AAAA" if not tokens.access_token.endswith("AAAA") else "BBBB")
    with pytest.raises(AuthError) as refused:
        ask(provider, forged)
    assert refused.value.status == 401 and forged not in refused.value.detail


def test_an_access_token_expires_after_fifteen_minutes(signed_in, clock):
    provider, tokens = signed_in
    clock.advance(minutes=14)
    assert ask(provider, tokens.access_token)
    clock.advance(minutes=2)
    with pytest.raises(AuthError):
        ask(provider, tokens.access_token)


def test_refresh_rotates_the_refresh_token(signed_in, clock):
    provider, tokens = signed_in
    clock.advance(minutes=20)
    renewed = provider.refresh(tokens.refresh_token)
    assert renewed.refresh_token != tokens.refresh_token
    assert ask(provider, renewed.access_token).subject == "localadmin:admin"
    assert provider.refresh(renewed.refresh_token).refresh_token != renewed.refresh_token


def test_a_spent_refresh_token_ends_the_session(signed_in):
    provider, tokens = signed_in
    renewed = provider.refresh(tokens.refresh_token)
    reused = refusal(lambda: provider.refresh(tokens.refresh_token))
    assert reused.code == "invalid_token" and reused.reused is True
    assert refusal(lambda: provider.refresh(renewed.refresh_token)).code == "invalid_token"
    with pytest.raises(AuthError):
        ask(provider, renewed.access_token)


def test_a_made_up_refresh_token_ends_nothing(signed_in):
    provider, tokens = signed_in
    sid = tokens.refresh_token.split(".")[1]
    for made_up in ("", "lar", "lar.x.y", f"lar.{sid}.wrong", "x" * 10_000, "lar.\ud800.\ud800"):
        refused = refusal(lambda: provider.refresh(made_up))
        assert refused.code == "invalid_token" and refused.reused is False
        assert provider.sign_out(made_up) is None
    assert provider.refresh(tokens.refresh_token)


def test_the_database_holds_no_refresh_token(signed_in, engine):
    _, tokens = signed_in
    with engine.connect() as c:
        row = dict(c.execute(text("SELECT * FROM iam_localadmin_session")).mappings().one())
    assert tokens.refresh_token.split(".")[2] not in str(row)


def test_sign_out_ends_the_session_at_once(signed_in):
    provider, tokens = signed_in
    assert provider.sign_out(tokens.refresh_token) == ("studio", "localadmin:admin")
    assert provider.sign_out(tokens.refresh_token) is None
    with pytest.raises(AuthError):
        ask(provider, tokens.access_token)
    assert refusal(lambda: provider.refresh(tokens.refresh_token)).code == "invalid_token"


def test_a_session_ends_after_its_lifetime_however_often_it_is_renewed(signed_in, clock):
    provider, tokens = signed_in
    clock.advance(hours=11)
    renewed = provider.refresh(tokens.refresh_token)
    clock.advance(hours=2)
    assert refusal(lambda: provider.refresh(renewed.refresh_token)).code == "invalid_token"


def test_a_password_change_ends_every_session(signed_in):
    provider, first = signed_in
    second = provider.sign_in("admin", CHOSEN, IP).tokens
    third = provider.change_password("admin", CHOSEN, "third-password-0003", IP).tokens
    for old in (first, second):
        with pytest.raises(AuthError):
            ask(provider, old.access_token)
        assert refusal(lambda: provider.refresh(old.refresh_token)).code == "invalid_token"
    assert ask(provider, third.access_token)


def test_rotating_the_signing_key_signs_everybody_out(start, signed_in):
    _, tokens = signed_in
    rotated = start(EAP_LOCAL_AUTH_SIGNING_KEY=secrets.token_hex(32))
    with pytest.raises(AuthError):
        ask(rotated, tokens.access_token)
    assert refusal(lambda: rotated.refresh(tokens.refresh_token)).code == "invalid_token"


def test_an_existing_admin_wins_over_other_env_values(start, signed_in, caplog):
    _, tokens = signed_in
    with caplog.at_level(logging.WARNING):
        renamed = start(EAP_LOCAL_ADMIN_USERNAME="root-admin", EAP_LOCAL_ADMIN_INITIAL_PASSWORD="another-initial-0003")
    assert "ignored" in caplog.text and renamed.setup_required() is False
    assert ask(renamed, tokens.access_token).subject == "localadmin:admin"
    assert refusal(lambda: renamed.sign_in("root-admin", "another-initial-0003", IP)).code == "invalid_credentials"
    assert renamed.sign_in("admin", CHOSEN, IP).tokens is not None


def test_the_membership_source_reads_the_admin_from_the_database(engine, signed_in):
    from eap_iam_localadmin import create_membership

    source = create_membership(PluginContext(engine=engine, env={"EAP_LOCAL_ADMIN_USERNAME": "someone-else"}))
    assert asyncio.run(source.roles_of("studio", "localadmin:admin")) == frozenset({"admin"})
    assert asyncio.run(source.roles_of("studio", "localadmin:someone-else")) == frozenset()
    assert [m.id for m in asyncio.run(source.list_members("studio"))] == ["admin"]
