"""Refusals a service raises. They describe the refusal only; `eap_api.service_errors` renders them as HTTP."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """A refused operation: a stable machine `code`, a human `message`, optional structured `details`."""

    code: str = "service_error"

    def __init__(
        self, message: str, *, code: str | None = None, details: list[Any] | None = None, detail_object: Any = None
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details
        # When set, the REST body is exactly `{"detail": detail_object}` (a body the API has always
        # answered with a structured `detail`, e.g. `{code, case, errors}`); `message` is still the text.
        self.detail_object = detail_object


class NotFound(ServiceError):
    """The resource does not exist (or is not visible to the caller's tenant)."""

    code = "not_found"


class Conflict(ServiceError):
    """The operation clashes with the resource's current state (a duplicate, a state, or a stale revision).

    With `current` set the refusal is a revision mismatch: `current` is the resource as a read returns it and
    `current_revision` its revision, so a client can diff without a second request."""

    code = "conflict"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: list[Any] | None = None,
        detail_object: Any = None,
        current: Any = None,
        current_revision: int | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details, detail_object=detail_object)
        self.current = current
        self.current_revision = current_revision


class InvalidInput(ServiceError):
    """The input is well-formed but not acceptable (validation the schema cannot express)."""

    code = "invalid_input"


class Unauthorized(ServiceError):
    """The credential the operation itself takes (a password, a refresh token) was refused."""

    code = "unauthorized"


class Forbidden(ServiceError):
    """The caller is authenticated but may not do this.

    `refused` names what the policy engine refused (`{action, kind, id}`), the caller's own request, so a client can
    ask why (`GET /iam/explain`)."""

    code = "forbidden"

    def __init__(
        self, message: str, *, code: str | None = None, details: list[Any] | None = None, detail_object: Any = None,
        refused: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details, detail_object=detail_object)
        self.refused = refused


class Unavailable(ServiceError):
    """A collaborator the operation needs is missing or down (not configured, unreachable)."""

    code = "unavailable"


class PayloadTooLarge(ServiceError):
    """The input exceeds a size limit the platform enforces (413)."""

    code = "payload_too_large"


class TooManyRequests(ServiceError):
    """A rate or concurrency limit refused the request; `retry_after` is the seconds a caller should wait."""

    code = "too_many_requests"

    def __init__(self, message: str, *, retry_after: int = 30, code: str | None = None) -> None:
        super().__init__(message, code=code)
        self.retry_after = retry_after
