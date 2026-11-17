"""Secret names and references, shared by the API, the worker and Studio-facing validation.

An agent never sees a secret value, only a reference `secret:<name>`; the worker resolves the
reference right before the value is used. No I/O here."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import StringConstraints

SECRET_NAME_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
SECRET_REF_PREFIX = "secret:"
SECRET_VALUE_MAX_CHARS = 8192

_NAME = re.compile(SECRET_NAME_PATTERN)

# `Annotated` validator for a secret name in a pydantic model.
SecretName = Annotated[str, StringConstraints(pattern=SECRET_NAME_PATTERN)]


def is_secret_name(value: object) -> bool:
    # `$` also matches before a trailing newline, so use fullmatch.
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def is_secret_ref(value: object) -> str | None:
    """The name when `value` is exactly `secret:<valid name>`, else None."""
    if not isinstance(value, str) or not value.startswith(SECRET_REF_PREFIX):
        return None
    name = value[len(SECRET_REF_PREFIX):]
    return name if is_secret_name(name) else None


__all__ = [
    "SECRET_NAME_PATTERN",
    "SECRET_REF_PREFIX",
    "SECRET_VALUE_MAX_CHARS",
    "SecretName",
    "is_secret_name",
    "is_secret_ref",
]
