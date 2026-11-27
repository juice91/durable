"""Password sign-in and first-run setup of a deployment's built-in account (docs/admin-account.md).

The identity plug-in does the verification, the throttling and the sessions (`eap_iam.PasswordSignInProvider`); this
service finds it, writes the audit rows and turns its refusals into the service errors. With no such provider in the
chain every operation is `NotFound`, like a route that does not exist. A row never holds a password, a token or what was
typed as the username; attempts refused while locked write none, so the lockout bounds what an anonymous caller can add
to the log."""

from __future__ import annotations

import logging
from typing import NoReturn

from eap_iam import PasswordSignInProvider, SessionTokens, SignInError, SignInResult
from eap_run_store import AuditEntry, AuditStore

from eap_api.services.errors import InvalidInput, NotFound, ServiceError, TooManyRequests, Unauthorized

log = logging.getLogger(__name__)

RESOURCE_KIND = "local_admin"
ACTION_SIGN_IN = "local_sign_in"
ACTION_CHANGE = "local_password_change"
ACTION_LOCKOUT = "local_sign_in_lockout"
ACTION_REUSE = "local_session_reuse"
ACTION_SIGN_OUT = "local_sign_out"
ACTION_SETUP = "local_setup"
REASON_CHANGE_REQUIRED = "password_change_required"


class LocalAuthService:
    def __init__(self, provider: PasswordSignInProvider | None, audit: AuditStore, request_id: str | None = None) -> None:
        self._provider = provider
        self._audit = audit
        self._request_id = request_id

    def _require(self) -> PasswordSignInProvider:
        if self._provider is None:
            raise NotFound("Not Found")
        return self._provider

    def _record(self, tenant_id: str, subject: str, action: str, outcome: str, reason: str | None = None) -> None:
        try:
            self._audit.record(AuditEntry(
                tenant_id=tenant_id, actor_kind="user", actor_subject=subject, action=action, outcome=outcome,
                resource_kind=RESOURCE_KIND, request_id=self._request_id,
                detail={"reason": reason} if reason else None,
            ))
        except Exception:  # noqa: BLE001 - never fail the request over its log
            log.exception("audit_log write failed for action %s", action)

    def _refuse(self, action: str | None, exc: SignInError) -> NoReturn:
        if action is not None and exc.code != "locked":
            self._record(exc.tenant_id, exc.subject, action, "denied", exc.code)
        if exc.lockout_started:
            self._record(exc.tenant_id, exc.subject, ACTION_LOCKOUT, "denied", "locked")
        if exc.reused:
            self._record(exc.tenant_id, exc.subject, ACTION_REUSE, "denied", "reused")
        refusal: ServiceError
        if exc.code == "locked":
            refusal = TooManyRequests(exc.detail, retry_after=exc.retry_after or 60, code=exc.code)
        elif exc.code in ("weak_password", "invalid_username"):
            refusal = InvalidInput(exc.detail, code=exc.code)
        else:
            refusal = Unauthorized(exc.detail, code=exc.code)
        raise refusal from None

    def setup_required(self) -> bool:
        return self._require().setup_required()

    def set_up_admin(self, code: str, username: str, password: str, client_address: str) -> str:
        """The username of the admin the setup code made."""
        provider = self._require()
        try:
            tenant_id, subject = provider.set_up_admin(code, username, password, client_address)
        except SignInError as exc:
            if exc.code == "setup_closed":
                raise NotFound(exc.detail) from None  # like a route that is not there, and no row
            self._refuse(ACTION_SETUP, exc)
        self._record(tenant_id, subject, ACTION_SETUP, "success")
        return username

    def sign_in(self, username: str, password: str, client_address: str) -> SignInResult:
        provider = self._require()
        try:
            result = provider.sign_in(username, password, client_address)
        except SignInError as exc:
            self._refuse(ACTION_SIGN_IN, exc)
        self._record(result.tenant_id, result.subject, ACTION_SIGN_IN, "success",
                     None if result.tokens else REASON_CHANGE_REQUIRED)
        return result

    def change_password(self, username: str, current_password: str, new_password: str, client_address: str) -> SignInResult:
        provider = self._require()
        try:
            result = provider.change_password(username, current_password, new_password, client_address)
        except SignInError as exc:
            self._refuse(ACTION_CHANGE, exc)
        self._record(result.tenant_id, result.subject, ACTION_CHANGE, "success")
        return result

    def refresh(self, refresh_token: str) -> SessionTokens:
        provider = self._require()
        try:
            return provider.refresh(refresh_token)
        except SignInError as exc:
            self._refuse(None, exc)  # a stale token is no event; a reused one is recorded

    def sign_out(self, refresh_token: str) -> None:
        ended = self._require().sign_out(refresh_token)
        if ended is not None:
            self._record(ended[0], ended[1], ACTION_SIGN_OUT, "success")
