"""Run in the API pod:

    kubectl -n pydanticos exec -it deploy/api -- python -m eap_iam_localadmin reset-password
    kubectl -n pydanticos exec deploy/api -- python -m eap_iam_localadmin setup-code

`reset-password`: the new password is prompted for twice on a terminal, or read as one line from stdin; it is never
taken from the command line. It ends every session, clears the lockouts, and must be changed at the next sign-in.
`setup-code`: while there is no admin account, print a new one-time code for Studio's setup page (the older one dies);
exit status 3 when the account exists. The deploy and dev scripts read both the line and the status.

Both need EAP_DATABASE_URL (set in the API pod)."""

from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

from eap_iam import IamConfigError
from sqlalchemy import create_engine, pool

from eap_iam_localadmin import tokens
from eap_iam_localadmin.passwords import Passwords, policy_problem
from eap_iam_localadmin.settings import AccountSettings, load_account_settings
from eap_iam_localadmin.store import AdminStore

ADMIN_EXISTS = 3  # `setup-code` when there is nothing to set up (scripts rely on it)

USAGE = (
    "usage: python -m eap_iam_localadmin {reset-password | setup-code}   "
    "(a password is prompted for, or read from stdin)"
)


def _read_password(stdin: TextIO, ask: Callable[[str], str]) -> str | None:
    """The new password, or None when the two typed ones differ."""
    if stdin.isatty():
        first = ask("New password: ")
        return first if ask("Again: ") == first else None
    return stdin.readline().rstrip("\r\n")


def _audit(engine, tenant_id: str, command: str, action: str, username: str | None, err: TextIO) -> None:  # noqa: ANN001
    from eap_run_store import AuditEntry, AuditStore

    try:
        AuditStore(engine).record(AuditEntry(
            tenant_id=tenant_id, actor_kind="service", actor_subject=f"cli:{command}", action=action,
            outcome="success", resource_kind="local_admin", resource_id=username,
        ))
    except Exception:  # noqa: BLE001 - the change happened; say the row is missing
        print("warning: the audit row could not be written", file=err)


def _setup_code(engine, store: AdminStore, settings: AccountSettings, out: TextIO, err: TextIO) -> int:  # noqa: ANN001
    code = tokens.new_setup_code()
    if not store.save_setup_code(tokens.setup_code_hash(code)):
        print("An admin account already exists: setup is closed. To set a new password use reset-password.", file=err)
        return ADMIN_EXISTS
    store.clear_throttle()
    _audit(engine, settings.tenant_id, "setup-code", "local_setup_code_issued", None, err)
    print(tokens.setup_code_line(code, settings.studio_url), file=out)
    return 0


def _reset_password(
    engine, store: AdminStore, settings: AccountSettings, stdin: TextIO, ask: Callable[[str], str], out: TextIO,  # noqa: ANN001
    err: TextIO,
) -> int:
    account = store.account()
    username = account.username if account else settings.username
    if username is None:
        print(
            "There is no admin account yet: create it on Studio's setup page (`python -m eap_iam_localadmin setup-code` "
            "prints a code), or set EAP_LOCAL_ADMIN_USERNAME and run this again.", file=err,
        )
        return 1
    password = _read_password(stdin, ask)
    if password is None:
        print("The two passwords differ. Nothing was changed.", file=err)
        return 2
    problem = policy_problem(password, username)
    if problem:
        print(f"{problem} Nothing was changed.", file=err)
        return 2
    hashed = Passwords().hash(password)
    if account is None:
        done = store.create_account(username, hashed, source="cli")
    else:
        done = store.set_password(hashed, expected_version=account.version, source="cli", must_change=True) is not None
    if not done:
        print("The account changed while it was being reset. Run the command again.", file=err)
        return 1
    store.clear_throttle()
    _audit(engine, settings.tenant_id, "reset-password", "local_password_reset", username, err)
    print(
        f"The password of the built-in admin of tenant {settings.tenant_id} is reset. Every session is signed out. "
        "Sign in with it and change it.", file=out,
    )
    return 0


def main(
    argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, stdin: TextIO | None = None,
    out: TextIO | None = None, err: TextIO | None = None, ask: Callable[[str], str] = getpass.getpass,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    stdin, out, err = stdin or sys.stdin, out or sys.stdout, err or sys.stderr
    if argv not in (["reset-password"], ["setup-code"]):
        print(USAGE, file=err)  # never echo the arguments: one of them may be a password
        return 2
    try:
        settings = load_account_settings(env)
    except IamConfigError as exc:
        print(str(exc), file=err)
        return 2
    url = env.get("EAP_DATABASE_URL")
    if not url:
        print("EAP_DATABASE_URL is not set", file=err)
        return 2
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        store = AdminStore(engine, settings.tenant_id)
        if store.missing_tables():
            print("The localadmin tables are missing: run `eap-db plugins upgrade` first.", file=err)
            return 1
        if argv == ["setup-code"]:
            return _setup_code(engine, store, settings, out, err)
        return _reset_password(engine, store, settings, stdin, ask, out, err)
    except Exception as exc:  # noqa: BLE001 - a database error may quote values: name its class only
        print(f"{argv[0]} failed: {type(exc).__name__}", file=err)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
