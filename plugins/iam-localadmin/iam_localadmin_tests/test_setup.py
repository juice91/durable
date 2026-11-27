"""First-run setup: while there is no admin, a one-time code from the API's log creates it. Needs Postgres."""

from __future__ import annotations

import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from eap_iam import IamConfigError, SignInError
from sqlalchemy import text

from eap_iam_localadmin.__main__ import main
from eap_iam_localadmin.store import IP_LIMIT

from .conftest import CHOSEN, ENV, INITIAL
from .test_provider import ask

IP = "192.0.2.1"
FRESH = {"EAP_LOCAL_ADMIN_USERNAME": "", "EAP_LOCAL_ADMIN_INITIAL_PASSWORD": ""}
CODE = re.compile(r"setup code: ((?:[A-Z2-7]{4}-){7}[A-Z2-7]{4})\b")


@pytest.fixture
def fresh(start, capsys):
    """(provider, the code its start printed): no account and no env pair."""
    provider = start(**FRESH)
    (code,) = CODE.findall(capsys.readouterr().err)
    return provider, code


def refusal(call) -> SignInError:
    with pytest.raises(SignInError) as refused:
        call()
    return refused.value


def rows(engine, table: str) -> list[dict]:
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(f"SELECT * FROM iam_localadmin_{table}")).mappings()]


def test_a_fresh_start_prints_one_code_and_stores_only_its_hash(start, engine, capsys, caplog):
    with caplog.at_level(logging.DEBUG):
        provider = start(**FRESH)
    printed = capsys.readouterr()
    (code,) = CODE.findall(printed.err)
    assert len(code.replace("-", "")) * 5 >= 128  # base32: five random bits a character
    assert provider.setup_required() is True and rows(engine, "account") == []
    stored = str(rows(engine, "setup"))
    assert code not in stored and code.replace("-", "") not in stored
    # Standard error only: never a log record, which a deployment also ships to its log store.
    assert code not in caplog.text and code not in printed.out


def test_each_start_makes_a_new_code_and_the_old_one_dies(start, fresh, capsys):
    provider, old = fresh
    restarted = start(**FRESH)
    (new,) = CODE.findall(capsys.readouterr().err)
    assert new != old
    assert refusal(lambda: restarted.set_up_admin(old, "owner", CHOSEN, IP)).code == "invalid_setup_code"
    assert restarted.set_up_admin(new, "owner", CHOSEN, IP) == ("studio", "localadmin:owner")


def test_the_code_creates_the_admin_once_and_closes_setup(fresh, engine, start, capsys):
    provider, code = fresh
    assert provider.set_up_admin(code, "owner", CHOSEN, IP) == ("studio", "localadmin:owner")
    assert provider.setup_required() is False and rows(engine, "setup") == []
    (account,) = rows(engine, "account")
    assert account["username"] == "owner" and account["must_change"] is False
    assert account["password_hash"].startswith("$argon2id$")
    tokens = provider.sign_in("owner", CHOSEN, IP).tokens
    assert ask(provider, tokens.access_token).subject == "localadmin:owner"
    for again in (code, "AAAA-" * 8):
        assert refusal(lambda: provider.set_up_admin(again, "intruder", CHOSEN, IP)).code == "setup_closed"
    capsys.readouterr()
    assert start(**FRESH).setup_required() is False
    assert "setup code" not in capsys.readouterr().err


def test_the_code_may_be_typed_in_lower_case_without_dashes(fresh):
    provider, code = fresh
    assert provider.set_up_admin(f"  {code.replace('-', ' ').lower()} ", "owner", CHOSEN, IP)


@pytest.mark.parametrize("wrong", ["", "AAAA-" * 7 + "AAAA", "x" * 10_000, "bad\ud800code"])
def test_a_wrong_code_creates_nothing(fresh, engine, wrong):
    provider, _ = fresh
    refused = refusal(lambda: provider.set_up_admin(wrong, "owner", CHOSEN, IP))
    assert refused.code == "invalid_setup_code" and refused.subject == "localadmin:unknown"
    assert rows(engine, "account") == [] and provider.setup_required()


@pytest.mark.parametrize(
    ("username", "password", "problem"),
    [("has space", CHOSEN, "invalid_username"), ("", CHOSEN, "invalid_username"), ("owner", "short", "weak_password"),
     ("administrator", "Administrator", "weak_password")],
)
def test_a_refused_username_or_password_keeps_the_code(fresh, username, password, problem):
    provider, code = fresh
    assert refusal(lambda: provider.set_up_admin(code, username, password, IP)).code == problem
    assert provider.set_up_admin(code, "owner", CHOSEN, IP)


def test_the_policy_is_not_told_to_someone_without_the_code(fresh):
    provider, _ = fresh
    assert refusal(lambda: provider.set_up_admin("AAAA", "owner", "short", IP)).code == "invalid_setup_code"


def test_concurrent_setups_have_exactly_one_winner(fresh, engine):
    provider, code = fresh

    def attempt(n: int) -> str:
        try:
            provider.set_up_admin(code, f"owner{n}", CHOSEN, f"198.51.100.{n}")
        except SignInError as exc:
            return exc.code
        return "created"

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count("created") == 1 and set(results) <= {"created", "setup_closed", "invalid_setup_code"}
    assert len(rows(engine, "account")) == 1


def test_guesses_lock_the_address_not_the_owner(fresh, clock):
    provider, code = fresh
    for n in range(IP_LIMIT):
        refused = refusal(lambda: provider.set_up_admin("AAAA", "owner", CHOSEN, IP))
        assert refused.lockout_started is (n == IP_LIMIT - 1)
    locked = refusal(lambda: provider.set_up_admin(code, "owner", CHOSEN, IP))
    assert locked.code == "locked" and locked.retry_after > 0
    assert provider.set_up_admin(code, "owner", CHOSEN, "203.0.113.9")


def test_the_env_pair_makes_the_account_and_setup_stays_closed(start, capsys):
    provider = start()
    assert provider.setup_required() is False and "setup code" not in capsys.readouterr().err
    assert refusal(lambda: provider.set_up_admin("AAAA", "owner", CHOSEN, IP)).code == "setup_closed"
    assert provider.sign_in("admin", INITIAL, IP).tokens is None


@pytest.mark.parametrize("half", [{"EAP_LOCAL_ADMIN_USERNAME": ""}, {"EAP_LOCAL_ADMIN_INITIAL_PASSWORD": ""}])
def test_half_an_env_pair_refuses_to_start_a_fresh_install(start, half):
    with pytest.raises(IamConfigError, match="EAP_LOCAL_ADMIN_USERNAME and EAP_LOCAL_ADMIN_INITIAL_PASSWORD"):
        start(**half)


def cli(url: str, argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    env = {**ENV, **FRESH, "EAP_DATABASE_URL": url}
    return main(argv, env=env, stdin=io.StringIO(), out=out, err=out), out.getvalue()


def test_the_command_issues_a_new_code_and_unlocks(database, fresh):
    provider, old = fresh
    for _ in range(IP_LIMIT):
        refusal(lambda: provider.set_up_admin("AAAA", "owner", CHOSEN, IP))
    code, output = cli(database[1], ["setup-code"])
    assert code == 0, output
    (new,) = CODE.findall(output)
    assert refusal(lambda: provider.set_up_admin(old, "owner", CHOSEN, IP)).code == "invalid_setup_code"
    assert provider.set_up_admin(new, "owner", CHOSEN, IP)
    code, output = cli(database[1], ["setup-code"])
    assert code == 3 and "already exists" in output and not CODE.search(output)
