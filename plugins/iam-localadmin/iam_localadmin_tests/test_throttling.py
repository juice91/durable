"""Throttling and temporary lockout, per username and per client address, held in the database."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from eap_iam import SignInError
from sqlalchemy import text

from eap_iam_localadmin.store import IP_LIMIT, USER_LIMIT

from .conftest import CHOSEN, INITIAL

WRONG = "wrong-password-000"


def attempt(provider, username="admin", password=WRONG, ip="192.0.2.1") -> str:
    try:
        provider.sign_in(username, password, ip)
    except SignInError as exc:
        return exc.code
    return "ok"


def test_the_account_locks_after_ten_failures_from_anywhere(signed_in, clock):
    provider, _ = signed_in
    for n in range(USER_LIMIT - 1):
        assert attempt(provider, ip=f"198.51.100.{n}") == "invalid_credentials"
    with pytest.raises(SignInError) as last:
        provider.sign_in("admin", WRONG, "198.51.100.200")
    assert last.value.code == "invalid_credentials" and last.value.lockout_started is True
    with pytest.raises(SignInError) as locked:
        provider.sign_in("admin", CHOSEN, "203.0.113.9")  # even the right password, from a new address
    assert locked.value.code == "locked" and locked.value.lockout_started is False
    assert 0 < locked.value.retry_after <= 900
    clock.advance(minutes=15, seconds=1)
    assert attempt(provider, password=CHOSEN) == "ok"


def test_an_unknown_username_locks_the_same_way(signed_in):
    provider, _ = signed_in
    codes = [attempt(provider, "ghost", ip=f"198.51.100.{n}") for n in range(USER_LIMIT + 1)]
    assert codes == ["invalid_credentials"] * USER_LIMIT + ["locked"]
    assert attempt(provider, password=CHOSEN) == "ok"  # the admin is not affected


def test_one_address_locks_across_usernames(signed_in):
    provider, _ = signed_in
    codes = [attempt(provider, f"user{n}") for n in range(IP_LIMIT)]
    assert set(codes) == {"invalid_credentials"}
    assert attempt(provider, password=CHOSEN) == "locked"
    assert attempt(provider, password=CHOSEN, ip="203.0.113.9") == "ok"


def test_a_locked_address_cannot_add_failures_to_the_account(signed_in):
    provider, _ = signed_in
    for n in range(IP_LIMIT):
        attempt(provider, f"user{n}")
    for _ in range(USER_LIMIT * 2):
        assert attempt(provider) == "locked"
    assert attempt(provider, password=CHOSEN, ip="203.0.113.9") == "ok"


def test_a_success_clears_the_counters(signed_in):
    provider, _ = signed_in
    for _ in range(3):
        for _ in range(USER_LIMIT - 1):
            assert attempt(provider) == "invalid_credentials"
        assert attempt(provider, password=CHOSEN) == "ok"


def test_failures_age_out_of_the_window(signed_in, clock):
    provider, _ = signed_in
    for _ in range(USER_LIMIT - 1):
        attempt(provider)
    clock.advance(minutes=16)
    for _ in range(USER_LIMIT - 1):
        assert attempt(provider) == "invalid_credentials"
    assert attempt(provider, password=CHOSEN) == "ok"


def test_the_password_change_is_throttled_too(start):
    provider = start()
    for _ in range(USER_LIMIT):
        with pytest.raises(SignInError):
            provider.change_password("admin", WRONG, CHOSEN, "192.0.2.1")
    with pytest.raises(SignInError) as locked:
        provider.change_password("admin", INITIAL, CHOSEN, "192.0.2.1")
    assert locked.value.code == "locked"


def test_parallel_guesses_never_exceed_the_limit(signed_in, monkeypatch):
    provider, _ = signed_in
    verified: list[int] = []
    real = provider.passwords.verify
    monkeypatch.setattr(provider.passwords, "verify", lambda h, p: (verified.append(1), real(h, p))[1])
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda n: attempt(provider, ip=f"198.51.100.{n}"), range(USER_LIMIT * 3)))
    assert len(verified) == USER_LIMIT and codes.count("invalid_credentials") == USER_LIMIT
    assert codes.count("locked") == USER_LIMIT * 2


def test_the_throttle_survives_a_restart_stores_no_typed_name_and_forgets_old_rows(start, signed_in, engine, clock):
    provider, _ = signed_in
    attempt(provider, "my-secret-typed-here")
    for _ in range(USER_LIMIT):
        attempt(provider)
    assert attempt(start(), password=CHOSEN) == "locked"
    with engine.connect() as c:
        assert "my-secret-typed-here" not in str(c.execute(text("SELECT * FROM iam_localadmin_throttle")).all())
    clock.advance(hours=1)
    attempt(provider, ip="203.0.113.9")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM iam_localadmin_throttle")).scalar_one() == 2  # only this attempt's
