"""The identity provider of the built-in admin: it verifies its own access tokens and does the password sign-in.

Nothing here logs a password or a token. Every refusal of a sign-in is the same text whether the username or the
password was wrong, every attempt costs one argon2 verification, and attempts are counted before they are checked."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import sys
from collections.abc import Callable
from datetime import datetime, timedelta

from eap_iam import (
    ROLE_ADMIN,
    AuthError,
    IamConfigError,
    Principal,
    RequestView,
    SessionTokens,
    SignInError,
    SignInResult,
)

from eap_iam_localadmin import tokens
from eap_iam_localadmin.passwords import Passwords, encodable, policy_problem
from eap_iam_localadmin.settings import PROVIDER, SUBJECT_PREFIX, USERNAME, USERNAME_RULE, Settings
from eap_iam_localadmin.store import IP_LIMIT, USER_LIMIT, Account, AdminStore

log = logging.getLogger(__name__)

INVALID_TOKEN_DETAIL = "Invalid access token"
INVALID_CREDENTIALS_DETAIL = "Invalid username or password"
LOCKED_DETAIL = "Too many sign-in attempts. Wait 15 minutes, then try again."
SESSION_ENDED_DETAIL = "Your session has ended. Sign in again."
INVALID_CODE_DETAIL = (
    "That setup code is not right. Only the newest one works; print a new one with: "
    "kubectl -n pydanticos exec deploy/api -- python -m eap_iam_localadmin setup-code"
)
SETUP_CLOSED_DETAIL = "Not Found"
UNKNOWN_SUBJECT = SUBJECT_PREFIX + "unknown"


class LocalAdminProvider:
    name = PROVIDER
    kinds = frozenset({"user"})

    def __init__(self, settings: Settings, store: AdminStore | None, passwords: Passwords, clock: Callable[[], datetime]) -> None:
        self.settings = settings
        self.passwords = passwords
        self._store = store
        self._clock = clock

    @property
    def store(self) -> AdminStore:
        if self._store is None:
            raise IamConfigError("the localadmin plug-in has no database")
        return self._store

    # --- start-up ----------------------------------------------------------------------------------------------------

    def provision(self, *, production: bool = False) -> None:
        """Without an account: make it from the env pair, or, with neither value set, print a one-time setup code for
        Studio's setup page. With one: it wins over the env values (a corrected initial password is followed until the
        first change). Says loudly while a change is still due; never refuses for that reason, because the change needs
        a running API."""
        missing = self.store.missing_tables()
        if missing:
            raise IamConfigError(
                f"the localadmin plug-in's tables are missing ({', '.join(sorted(missing))}): run `eap-db plugins upgrade` "
                "with localadmin listed in EAP_IAM_IDENTITY (the migrate job does it)"
            )
        username, initial = self.settings.username, self.settings.initial_password
        account = self.store.account()
        if account is None:
            if username is None and initial is None:
                self._announce_setup_code()
                return
            if username is None or initial is None:
                raise IamConfigError(
                    "set both EAP_LOCAL_ADMIN_USERNAME and EAP_LOCAL_ADMIN_INITIAL_PASSWORD, or neither to create the "
                    "admin account on Studio's setup page"
                )
            self._check_initial(initial, username)
            self.store.create_account(username, self.passwords.hash(initial), source="env")
            account = self.store.account()
            assert account is not None
        if username is not None and username != account.username:
            log.warning(
                "EAP_LOCAL_ADMIN_USERNAME and its initial password are ignored: an admin account with another name exists"
            )
        elif account.must_change and account.source == "env" and initial and not self.passwords.verify(account.password_hash, initial):
            self._check_initial(initial, account.username)
            self.store.set_password(self.passwords.hash(initial), expected_version=account.version, source="env",
                                    must_change=True)
            log.warning("the built-in admin account now uses the new EAP_LOCAL_ADMIN_INITIAL_PASSWORD")
        if account.must_change:
            log.log(
                logging.ERROR if production else logging.WARNING,
                "the built-in admin account still has its initial password (or one set by a reset): anyone who knows it "
                "can take the account. Sign in now and change it.",
            )
        elif initial:
            log.warning(
                "EAP_LOCAL_ADMIN_INITIAL_PASSWORD is set but ignored: the password has been changed. Remove it from the "
                "deploy secrets."
            )

    def _announce_setup_code(self) -> None:
        code = tokens.new_setup_code()
        if self.store.save_setup_code(tokens.setup_code_hash(code)):
            # Standard error, not the logger: the logger may also ship to a log store that more people read.
            print(tokens.setup_code_line(code, self.settings.studio_url), file=sys.stderr, flush=True)

    def _check_initial(self, initial: str, username: str) -> None:
        problem = policy_problem(initial, username)
        if problem:
            raise IamConfigError(f"EAP_LOCAL_ADMIN_INITIAL_PASSWORD is not acceptable. {problem}")

    # --- first-run setup ---------------------------------------------------------------------------------------------

    def setup_required(self) -> bool:
        return self.store.account() is None

    def set_up_admin(self, code: str, username: str, password: str, client_address: str) -> tuple[str, str]:
        """Create the admin with the one-time setup code: its (tenant id, subject). Throttled per address only: a
        shared counter would let anyone block the owner, and 160 random bits need no further protection."""
        if not self.setup_required():
            raise self._refused("setup_closed", SETUP_CLOSED_DETAIL, UNKNOWN_SUBJECT)
        ip_key = "ip:" + client_address[:64]
        self._reserve(ip_key, IP_LIMIT, UNKNOWN_SUBJECT)
        presented, stored = tokens.setup_code_hash(code), self.store.setup_code_hash()
        if stored is None or not hmac.compare_digest(presented, stored):
            locked = self.store.failed(ip_key, IP_LIMIT)
            raise self._refused("invalid_setup_code", INVALID_CODE_DETAIL, UNKNOWN_SUBJECT, lockout_started=locked)
        self.store.clear_throttle(ip_key)
        if not USERNAME.fullmatch(username):
            raise self._refused("invalid_username", f"The username must be {USERNAME_RULE}.", UNKNOWN_SUBJECT)
        problem = policy_problem(password, username)
        if problem:
            raise self._refused("weak_password", problem, SUBJECT_PREFIX + username)
        if not self.store.claim_setup(presented, username, self.passwords.hash(password)):
            raise self._refused("setup_closed", SETUP_CLOSED_DETAIL, UNKNOWN_SUBJECT)
        return self.settings.tenant_id, SUBJECT_PREFIX + username

    # --- the identity chain ------------------------------------------------------------------------------------------

    async def authenticate(self, request: RequestView) -> Principal | None:
        scheme, _, token = (request.header("authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not tokens.is_ours(token.strip()):
            return None
        principal = await asyncio.to_thread(self._principal, token.strip())
        if principal is None:
            raise AuthError(INVALID_TOKEN_DETAIL)
        return principal

    def _principal(self, token: str) -> Principal | None:
        claims = tokens.verify_access(self.settings.signing_key, token, self._clock())
        if claims is None or claims["ten"] != self.settings.tenant_id or self._store is None:
            return None
        session = self._store.live_session(claims["sid"])  # None: signed out, expired, or the password changed
        if session is None or session.username != claims["sub"]:
            return None
        return Principal(
            tenant_id=self.settings.tenant_id, kind="user", subject=SUBJECT_PREFIX + session.username, provider=PROVIDER,
            roles=frozenset({ROLE_ADMIN}),
        )

    # --- sign-in -----------------------------------------------------------------------------------------------------

    def _refused(self, code: str, detail: str, subject: str, **extra) -> SignInError:  # noqa: ANN003
        return SignInError(code, detail, tenant_id=self.settings.tenant_id, subject=subject, **extra)

    def _reserve(self, key: str, limit: int, subject: str) -> None:
        reservation = self.store.reserve(key, limit)
        if reservation.locked_for is not None:
            raise self._refused("locked", LOCKED_DETAIL, subject, retry_after=reservation.locked_for,
                                lockout_started=reservation.lockout_started)

    def _verified(self, username: str, password: str, client_address: str) -> Account:
        """The account when `username` and `password` are right and neither it nor the address is locked."""
        account = self.store.account()
        named = account is not None and encodable(username) and hmac.compare_digest(
            username.encode(), account.username.encode()
        )
        subject = SUBJECT_PREFIX + account.username if named else UNKNOWN_SUBJECT
        # A digest: what was typed into the username field may be a password.
        user_key = "user:" + hashlib.sha256(username[:256].encode(errors="surrogatepass")).hexdigest()[:32]
        ip_key = "ip:" + client_address[:64]
        # The address first: a locked address must not go on counting against the account.
        for key, limit in ((ip_key, IP_LIMIT), (user_key, USER_LIMIT)):
            self._reserve(key, limit, subject)
        matches = self.passwords.verify(account.password_hash if named else None, password)
        if not matches or account is None:
            locked = [self.store.failed(key, limit) for key, limit in ((ip_key, IP_LIMIT), (user_key, USER_LIMIT))]
            if any(locked):
                log.warning("sign-in locked after repeated failures")
            raise self._refused("invalid_credentials", INVALID_CREDENTIALS_DETAIL, subject, lockout_started=any(locked))
        self.store.clear_throttle(ip_key, user_key)
        return account

    def _session(self, account: Account) -> SessionTokens:
        session_id = tokens.new_session_id()
        refresh = tokens.new_refresh_token(session_id)
        self.store.create_session(
            session_id, account.username, tokens.refresh_hash(self.settings.signing_key, refresh), account.version,
            timedelta(hours=self.settings.session_hours),
        )
        return self._tokens(account.username, session_id, refresh)

    def _tokens(self, username: str, session_id: str, refresh: str) -> SessionTokens:
        access = tokens.access_token(
            self.settings.signing_key, tenant_id=self.settings.tenant_id, username=username, session_id=session_id,
            now=self._clock(),
        )
        return SessionTokens(access, refresh, tokens.ACCESS_SECONDS, self.settings.tenant_id, SUBJECT_PREFIX + username)

    def sign_in(self, username: str, password: str, client_address: str) -> SignInResult:
        account = self._verified(username, password, client_address)
        session = None if account.must_change else self._session(account)
        return SignInResult(self.settings.tenant_id, SUBJECT_PREFIX + account.username, session)

    def change_password(self, username: str, current_password: str, new_password: str, client_address: str) -> SignInResult:
        account = self._verified(username, current_password, client_address)
        subject = SUBJECT_PREFIX + account.username
        problem = policy_problem(new_password, account.username)
        if problem is None and new_password == current_password:
            problem = "The new password must differ from the current one."
        if problem:
            raise self._refused("weak_password", problem, subject)
        version = self.store.set_password(
            self.passwords.hash(new_password), expected_version=account.version, source="user", must_change=False
        )
        if version is None:  # changed by someone else meanwhile
            raise self._refused("invalid_credentials", INVALID_CREDENTIALS_DETAIL, subject)
        changed = self.store.account()
        assert changed is not None
        return SignInResult(self.settings.tenant_id, subject, self._session(changed))

    def refresh(self, refresh_token: str) -> SessionTokens:
        session_id = tokens.refresh_session_id(refresh_token)
        reused = False
        if session_id is not None:
            replacement = tokens.new_refresh_token(session_id)
            session, reused = self.store.rotate(
                session_id,
                tokens.refresh_hash(self.settings.signing_key, refresh_token),
                tokens.refresh_hash(self.settings.signing_key, replacement),
            )
            if session is not None:
                return self._tokens(session.username, session_id, replacement)
        if reused:
            log.warning("a spent refresh token was presented again: its session is ended")
        account = self.store.account()
        subject = SUBJECT_PREFIX + account.username if account else UNKNOWN_SUBJECT
        raise self._refused("invalid_token", SESSION_ENDED_DETAIL, subject, reused=reused)

    def sign_out(self, refresh_token: str) -> tuple[str, str] | None:
        session_id = tokens.refresh_session_id(refresh_token)
        if session_id is None:
            return None
        username = self.store.end_session(session_id, tokens.refresh_hash(self.settings.signing_key, refresh_token))
        return None if username is None else (self.settings.tenant_id, SUBJECT_PREFIX + username)
