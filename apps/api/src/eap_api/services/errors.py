"""Refusals a service raises. They describe the refusal only; `eap_api.service_errors` renders them as HTTP."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """A refused operation: a stable machine `code`, a human `message`, optional structured `details`."""

    code: str = "service_error"

    def __init__(self, message: str, *, code: str | None = None, details: list[Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details


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
        current: Any = None,
        current_revision: int | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.current = current
        self.current_revision = current_revision


class InvalidInput(ServiceError):
    """The input is well-formed but not acceptable (validation the schema cannot express)."""

    code = "invalid_input"


class Forbidden(ServiceError):
    """The caller is authenticated but may not do this."""

    code = "forbidden"


class Unavailable(ServiceError):
    """A collaborator the operation needs is missing or down (not configured, unreachable)."""

    code = "unavailable"
