"""Typed request context for agent runs (milestone P0, task P0.2).

An agent spec declares `context_fields`; a caller supplies `request_context` values per run or turn.
The API validates them with `validate_request_context` (errors as `{"loc", "msg", "type"}` with `loc`
relative to the context object, never echoing a value), the worker renders `{{context.<name>}}`
placeholders in the instructions with `render_instructions`, and tools read the values.

`tenant_id`, `user_id` and `run_id` are reserved: trusted identity comes only from the principal.

Rendering is plain substitution in one pass: no expressions, no attribute access, and a substituted
value is never scanned again. A placeholder whose name is not a declared field is left as written
(publish rejects such specs through `placeholder_errors`; specs stored earlier keep their text).
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, Literal, Mapping, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_CONTEXT_FIELDS = 32
MAX_CONTEXT_BYTES = 8 * 1024
MAX_STRING_LENGTH = MAX_CONTEXT_BYTES
MAX_ENUM_VALUES = 64
MAX_ENUM_VALUE_LENGTH = 128
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
RESERVED_CONTEXT_NAMES: frozenset[str] = frozenset({"tenant_id", "user_id", "run_id"})
CONTEXT_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"

_NAME_RE = re.compile(CONTEXT_NAME_PATTERN)
# A well-formed placeholder: {{context.<name>}}, optional spaces inside the braces.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*context\.([a-z][a-z0-9_]{0,31})\s*\}\}")
# Anything that looks like a context placeholder (to report malformed ones at publish).
_PLACEHOLDER_LIKE_RE = re.compile(r"\{\{\s*context\b[^{}]*\}\}")

ContextFieldType = Literal["string", "integer", "number", "boolean", "enum"]


class ContextField(BaseModel):
    """One typed request context field an agent accepts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: ContextFieldType
    required: bool = False
    description: str = Field(default="", max_length=500)
    # Strings only: maximum length in characters.
    max_length: int | None = Field(default=None, ge=1, le=MAX_STRING_LENGTH)
    # Enums only: the allowed values.
    values: Tuple[str, ...] = ()

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.fullmatch(v):
            raise ValueError(f"context field name must match {CONTEXT_NAME_PATTERN}")
        if v in RESERVED_CONTEXT_NAMES:
            raise ValueError(f"'{v}' is reserved: identity comes from the authenticated principal")
        return v

    @field_validator("values")
    @classmethod
    def _valid_values(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) > MAX_ENUM_VALUES:
            raise ValueError(f"at most {MAX_ENUM_VALUES} enum values")
        seen: set[str] = set()
        for value in v:
            if not value or len(value) > MAX_ENUM_VALUE_LENGTH:
                raise ValueError(f"enum values must be 1 to {MAX_ENUM_VALUE_LENGTH} characters")
            if value in seen:
                raise ValueError(f"duplicate enum value: {value}")
            seen.add(value)
        return v

    @model_validator(mode="after")
    def _type_specific(self) -> "ContextField":
        if self.max_length is not None and self.type != "string":
            raise ValueError("max_length applies only to string fields")
        if self.type == "enum" and not self.values:
            raise ValueError("an enum field needs at least one value")
        if self.type != "enum" and self.values:
            raise ValueError("values apply only to enum fields")
        return self


class RequestContextError(ValueError):
    """Request context rejected. `errors` is a list of `{"loc", "msg", "type"}` (no values).

    Pickle-safe: the constructor argument goes to `super().__init__`, the message is built in
    `__str__`.
    """

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__(errors)
        self.errors = errors

    def __str__(self) -> str:
        parts = []
        for err in self.errors:
            loc = ".".join(str(p) for p in err.get("loc", ()))
            parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
        return "invalid request context: " + "; ".join(parts)


def _err(loc: list[Any], msg: str, type_: str) -> dict[str, Any]:
    return {"loc": loc, "msg": msg, "type": type_}


def _check_value(field: ContextField, value: Any) -> dict[str, Any] | None:
    loc = [field.name]
    if field.type == "string":
        if not isinstance(value, str):
            return _err(loc, "must be a string", "string_type")
        if field.max_length is not None and len(value) > field.max_length:
            return _err(loc, f"must be at most {field.max_length} characters", "string_too_long")
    elif field.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return _err(loc, "must be an integer", "int_type")
        if not INT64_MIN <= value <= INT64_MAX:
            return _err(loc, "must be a 64-bit integer (-2^63 to 2^63-1)", "int_out_of_range")
    elif field.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _err(loc, "must be a number", "float_type")
        try:
            as_float = float(value)  # a huge int raises OverflowError instead of returning inf
        except OverflowError:
            return _err(loc, "is too large for a number", "number_too_large")
        if not math.isfinite(as_float):
            return _err(loc, "must be a finite number", "finite_number")
    elif field.type == "boolean":
        if not isinstance(value, bool):
            return _err(loc, "must be a boolean", "bool_type")
    elif field.type == "enum":
        if not isinstance(value, str) or value not in field.values:
            return _err(loc, "must be one of the declared values", "enum")
    return None


def validate_request_context(fields: Iterable[ContextField], value: Any) -> dict[str, Any]:
    """Validate `value` against the declared fields; return the accepted context.

    `None` means no context. Unknown keys, reserved names, missing required fields, wrong types
    (`true` is not an integer or number), over-long strings, values outside an enum and a JSON size
    over 8 KiB are rejected with `RequestContextError`. A `null` value for an optional field is the
    same as leaving it out. The result has the declared fields only, in declaration order.
    """
    fields = tuple(fields)
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise RequestContextError([_err([], "request context must be an object", "dict_type")])
    try:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise RequestContextError([_err([], "request context must be plain JSON", "json_invalid")]) from None
    if size > MAX_CONTEXT_BYTES:
        raise RequestContextError([_err([], f"request context must be at most {MAX_CONTEXT_BYTES} bytes", "too_large")])

    by_name = {f.name: f for f in fields}
    errors: list[dict[str, Any]] = []
    for key in value:
        if key in by_name:
            continue
        if key in RESERVED_CONTEXT_NAMES:
            errors.append(_err([key], "is reserved and cannot be supplied", "reserved_name"))
        else:
            errors.append(_err([str(key)[:64]], "is not a context field of this agent", "extra_forbidden"))

    result: dict[str, Any] = {}
    for field in fields:
        supplied = value.get(field.name)
        if supplied is None:
            if field.required:
                errors.append(_err([field.name], "is required", "missing"))
            continue
        problem = _check_value(field, supplied)
        if problem:
            errors.append(problem)
        else:
            result[field.name] = supplied
    if errors:
        raise RequestContextError(errors)
    return result


def context_json_schema(fields: Iterable[ContextField]) -> dict[str, Any]:
    """JSON Schema of the context, in the subset Studio's JsonSchemaForm renders (object of
    string/integer/number/boolean properties, `enum`, `maxLength`, `required`, `description`)."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields:
        prop: dict[str, Any] = {"type": "string" if field.type == "enum" else field.type}
        if field.type == "enum":
            prop["enum"] = list(field.values)
        if field.max_length is not None:
            prop["maxLength"] = field.max_length
        if field.description:
            prop["description"] = field.description
        properties[field.name] = prop
        if field.required:
            required.append(field.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def placeholders(text: str) -> tuple[str, ...]:
    """Field names referenced by well-formed `{{context.<name>}}` placeholders, first-seen order."""
    seen: dict[str, None] = {}
    for match in _PLACEHOLDER_RE.finditer(text):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def placeholder_errors(text: str, fields: Iterable[ContextField]) -> list[str]:
    """Publish check: messages for malformed context placeholders and for placeholders naming no
    declared field. Empty when the instructions are fine."""
    names = {f.name for f in fields}
    errors: list[str] = []
    for match in _PLACEHOLDER_LIKE_RE.finditer(text):
        token = match.group(0)
        if not _PLACEHOLDER_RE.fullmatch(token):
            errors.append(f"malformed placeholder {token[:80]}: use {{{{context.<field name>}}}}")
    for name in placeholders(text):
        if name not in names:
            errors.append(f"placeholder {{{{context.{name}}}}} names no context field")
    return errors


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return ""


def render_instructions(text: str, context: Mapping[str, Any], fields: Iterable[ContextField] | None = None) -> str:
    """Replace `{{context.<name>}}` with the context value, in one pass.

    With `fields`, a placeholder for a declared field without a value renders as an empty string and
    a placeholder for an undeclared name stays as written. Without `fields`, only names present in
    `context` are replaced. Values are inserted verbatim and never re-scanned; nothing is evaluated.
    """
    declared = {f.name for f in fields} if fields is not None else set(context)

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in declared:
            return match.group(0)
        return _format(context.get(name))

    return _PLACEHOLDER_RE.sub(substitute, text)


__all__ = [
    "CONTEXT_NAME_PATTERN",
    "MAX_CONTEXT_BYTES",
    "MAX_CONTEXT_FIELDS",
    "MAX_ENUM_VALUES",
    "MAX_ENUM_VALUE_LENGTH",
    "INT64_MAX",
    "INT64_MIN",
    "RESERVED_CONTEXT_NAMES",
    "ContextField",
    "ContextFieldType",
    "RequestContextError",
    "context_json_schema",
    "placeholder_errors",
    "placeholders",
    "render_instructions",
    "validate_request_context",
]
