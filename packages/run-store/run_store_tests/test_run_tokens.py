"""Run tokens: signed, short-lived, per tenant and run."""

from __future__ import annotations

from eap_run_store.run_tokens import PREFIX, TTL_SECONDS, looks_like_run_token, mint_run_token, signing_key, verify_run_token

KEY = signing_key("platform-secrets-key")


def test_a_minted_token_verifies_and_names_the_tenant_and_run():
    token = mint_run_token(KEY, "t1", "run-1", now=1000)
    assert token.startswith(PREFIX) and looks_like_run_token(token)
    claims = verify_run_token(KEY, token, now=1001)
    assert (claims.tenant_id, claims.run_id, claims.expires_at) == ("t1", "run-1", 1000 + TTL_SECONDS)


def test_expired_forged_and_foreign_tokens_are_refused():
    token = mint_run_token(KEY, "t1", "run-1", now=1000)
    assert verify_run_token(KEY, token, now=1000 + TTL_SECONDS) is None  # expired exactly at its end
    assert verify_run_token(signing_key("another-key"), token, now=1001) is None
    body, _, signature = token[len(PREFIX):].partition(".")
    forged = mint_run_token(KEY, "t2", "run-1", now=1000)
    assert verify_run_token(KEY, f"{PREFIX}{forged[len(PREFIX):].partition('.')[0]}.{signature}", now=1001) is None
    for bad in ("", PREFIX, PREFIX + "x", PREFIX + "x.y", "pos_abc_def", token + "0", token[:-1]):
        assert verify_run_token(KEY, bad, now=1001) is None


def test_the_signing_key_is_derived_not_the_secret():
    assert signing_key("k") != b"k" and signing_key("k") == signing_key("k") and signing_key("k") != signing_key("k2")
