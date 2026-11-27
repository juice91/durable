"""`python -m eap_iam_localadmin reset-password`: the password comes from the terminal or stdin, never argv."""

from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest
from eap_iam import AuthError, SignInError

from eap_iam_localadmin.__main__ import main

from .conftest import CHOSEN, ENV, INITIAL
from .test_provider import ask

NEW = "reset-password-0004"


class Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def run(url: str, stdin, argv=("reset-password",), prompts=None, env_extra=None):
    out, err = io.StringIO(), io.StringIO()
    answers = iter(prompts or [])
    env = {**ENV, "EAP_DATABASE_URL": url, **(env_extra or {})}
    code = main(list(argv), env=env, stdin=stdin, out=out, err=err, ask=lambda _prompt: next(answers))
    return code, out.getvalue() + err.getvalue()


def test_reset_from_stdin_ends_sessions_unlocks_and_forces_a_change(database, signed_in, start):
    provider, tokens = signed_in
    for _ in range(12):
        with pytest.raises(SignInError):
            provider.sign_in("admin", "wrong-password-000", "192.0.2.1")
    code, output = run(database[1], Pipe(NEW + "\n"))
    assert code == 0 and NEW not in output and "change it" in output
    with pytest.raises(AuthError):
        ask(provider, tokens.access_token)
    assert provider.sign_in("admin", NEW, "192.0.2.1").tokens is None  # unlocked, and a change is due
    with pytest.raises(SignInError):
        provider.sign_in("admin", CHOSEN, "192.0.2.1")
    # a restart does not put the initial password back
    assert start().sign_in("admin", NEW, "192.0.2.1").tokens is None
    with pytest.raises(SignInError):
        start().sign_in("admin", INITIAL, "192.0.2.1")


def test_reset_prompts_twice_on_a_terminal(database, start):
    provider = start()
    code, output = run(database[1], Tty(), prompts=[NEW, NEW])
    assert code == 0 and NEW not in output
    assert provider.sign_in("admin", NEW, "192.0.2.1").tokens is None


def test_reset_creates_the_account_when_there_is_none(database, engine, start):
    code, _ = run(database[1], Pipe(NEW + "\r\n"))
    assert code == 0
    assert start(EAP_LOCAL_ADMIN_INITIAL_PASSWORD="").sign_in("admin", NEW, "192.0.2.1").tokens is None


@pytest.mark.parametrize(("stdin", "prompts"), [(Pipe("short\n"), None), (Pipe(""), None), (Tty(), [NEW, NEW + "x"])])
def test_reset_refuses_a_weak_empty_or_mistyped_password(database, start, stdin, prompts):
    provider = start()
    code, output = run(database[1], stdin, prompts=prompts)
    assert code != 0 and "short" not in output.replace("shorter", "")
    assert provider.sign_in("admin", INITIAL, "192.0.2.1").tokens is None


@pytest.mark.parametrize("argv", [["reset-password", NEW], ["reset-password", "--password", NEW], [NEW], []])
def test_reset_takes_no_password_on_the_command_line_and_does_not_echo_one(database, start, argv):
    start()
    code, output = run(database[1], Pipe(""), argv=argv)
    assert code == 2 and NEW not in output and "usage" in output


def test_reset_names_what_is_missing(database, engine):
    code, output = run(database[1], Pipe(NEW + "\n"), env_extra={"EAP_LOCAL_ADMIN_USERNAME": ""})
    assert code == 1 and "setup-code" in output
    code, output = run("", Pipe(NEW + "\n"))
    assert code == 2 and "EAP_DATABASE_URL" in output


def test_reset_needs_no_username_when_the_admin_exists(database, start):
    provider = start()
    code, _ = run(database[1], Pipe(NEW + "\n"), env_extra={"EAP_LOCAL_ADMIN_USERNAME": ""})
    assert code == 0 and provider.sign_in("admin", NEW, "192.0.2.1").tokens is None


def test_the_module_runs_as_the_documented_command(database, start):
    start()
    env = {**os.environ, **ENV, "EAP_DATABASE_URL": database[1]}
    done = subprocess.run(
        [sys.executable, "-m", "eap_iam_localadmin", "reset-password"], input=NEW + "\n", env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert NEW not in done.stdout + done.stderr
