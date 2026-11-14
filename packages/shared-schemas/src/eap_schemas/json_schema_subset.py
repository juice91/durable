"""The JSON Schema subset the platform accepts from users (agent capabilities milestone, AC.1).

One structural validator, two dialects:

- ``"standard"`` (structured output, the eval ``schema_valid`` evaluator): the root is
  ``type: object``. Allowed keywords: ``type`` (``string``, ``number``, ``integer``, ``boolean``,
  ``object``, ``array``, ``null``, or a two-type list of one of them and ``"null"``),
  ``properties``, ``required`` (names declared in ``properties``), ``additionalProperties``
  (boolean), ``items`` (any subset schema), ``enum`` (scalars or null), ``const`` (a scalar or
  null), ``description``, ``title``, ``minimum``/``maximum`` (numbers), ``minLength``/``maxLength``
  (strings), ``minItems``/``maxItems`` (arrays), ``format`` (strings; an annotation, never asserted),
  and ``$schema`` (a string) at the root. Everything else is rejected, notably ``$ref``, ``$defs``,
  ``oneOf``/``anyOf``/``allOf``/``not`` and ``pattern``/``patternProperties`` (Python ``re`` in
  ``jsonschema`` is a ReDoS vector).
- ``"working_memory"``: RM.2's working-memory subset, unchanged (scalar-only array items and enums,
  no ``null``, ``const``, bounds or ``format``; ``required`` accepted and ignored).

Limits are parameters: object nesting depth (the root counts as 1; arrays do not add a level),
properties in total, and the canonical JSON size in bytes (UTF-8, sorted keys, compact). Any schema
also nests at most MAX_JSON_DEPTH objects or arrays, checked without recursion before anything
else walks it.

Messages name a path and the rule, never a value. No I/O.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

# Nesting cap (objects and arrays) for any schema, checked iteratively before the walk.
MAX_JSON_DEPTH = 16
MAX_PROPERTY_NAME_CHARS = 64

# Limits of the "standard" dialect as used for structured output and eval schemas.
OUTPUT_SCHEMA_MAX_DEPTH = 5
OUTPUT_SCHEMA_MAX_PROPERTIES = 100
OUTPUT_SCHEMA_MAX_BYTES = 16 * 1024
OUTPUT_SCHEMA_MAX_ENUM_VALUES = 256

Dialect = Literal["standard", "working_memory"]
# The only `$schema` the standard dialect accepts at the root (or none).
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
# Standard-dialect property names: the same segment syntax as eval field paths and workflow `$ref`s.
PROPERTY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_SCALAR_TYPES = ("string", "number", "integer", "boolean")
_ALL_TYPES = (*_SCALAR_TYPES, "object", "array", "null")


class SchemaSubsetInvalid(ValueError):
    """A JSON Schema is outside the supported subset. Names the path, never a value. Pickle-safe."""

    code = "json_schema_invalid"

    def __init__(self, reason: str, path: str = "") -> None:
        self.reason = reason
        self.path = path
        super().__init__(reason, path)

    def __str__(self) -> str:
        where = f" at {self.path}" if self.path else ""
        return f"the JSON Schema is invalid{where}: {self.reason}"


@dataclass(frozen=True)
class _Rules:
    keywords: frozenset[str]
    nullable: bool  # "null" and [T, "null"] types, null enum/const values
    const: bool
    nested_items: bool  # array items may be objects and arrays
    required_declared: bool  # `required` names must be declared properties
    max_enum: int
    strict_names: bool  # property names must match PROPERTY_NAME_PATTERN; `$schema` only 2020-12
    no_type: str
    string_extra: frozenset[str]
    number_extra: frozenset[str]
    array_extra: frozenset[str]


_DESCRIPTIVE = frozenset({"description", "title"})

_RULES: dict[str, _Rules] = {
    "working_memory": _Rules(
        keywords=frozenset(
            {"type", "properties", "items", "enum", "description", "title", "required", "additionalProperties"}
        ),
        nullable=False,
        const=False,
        nested_items=False,
        required_declared=False,
        max_enum=64,
        strict_names=False,
        no_type='a schema needs a "type" or an "enum"',
        string_extra=frozenset(),
        number_extra=frozenset(),
        array_extra=frozenset(),
    ),
    "standard": _Rules(
        keywords=frozenset(
            {
                "type", "properties", "items", "enum", "const", "description", "title", "required",
                "additionalProperties", "minimum", "maximum", "minLength", "maxLength", "minItems",
                "maxItems", "format",
            }
        ),
        nullable=True,
        const=True,
        nested_items=True,
        required_declared=True,
        max_enum=OUTPUT_SCHEMA_MAX_ENUM_VALUES,
        strict_names=True,
        no_type='a schema needs a "type", an "enum" or a "const"',
        string_extra=frozenset({"minLength", "maxLength", "format"}),
        number_extra=frozenset({"minimum", "maximum"}),
        array_extra=frozenset({"minItems", "maxItems"}),
    ),
}


def validate(
    schema: Any,
    *,
    max_depth: int = OUTPUT_SCHEMA_MAX_DEPTH,
    max_properties: int = OUTPUT_SCHEMA_MAX_PROPERTIES,
    max_bytes: int = OUTPUT_SCHEMA_MAX_BYTES,
    root_object: bool = True,
    dialect: Dialect = "standard",
) -> dict[str, Any]:
    """Check that `schema` is inside the subset; return it unchanged.

    Raises SchemaSubsetInvalid naming the offending path. With `root_object` the root must be
    `{"type": "object", ...}`.
    """
    rules = _RULES[dialect]
    if not isinstance(schema, dict):
        raise SchemaSubsetInvalid("the schema must be a JSON object")
    check_json_depth(schema, "the schema")
    try:
        size = json_size(schema)
    except (TypeError, ValueError):
        raise SchemaSubsetInvalid("the schema must be JSON") from None
    if size > max_bytes:
        raise SchemaSubsetInvalid(f"the schema must be at most {max_bytes} bytes")
    if root_object and schema.get("type") != "object":
        raise SchemaSubsetInvalid('the root must have "type": "object"')
    root = {k: v for k, v in schema.items() if k != "$schema"}
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise SchemaSubsetInvalid('"$schema" must be a string')
    if rules.strict_names and "$schema" in schema and schema["$schema"] != DRAFT_2020_12:
        raise SchemaSubsetInvalid(f'"$schema" may only be {DRAFT_2020_12}')
    walker = _Walker(rules, max_depth=max_depth, max_properties=max_properties)
    walker.node(root, "", depth=1)
    return schema


def validate_output_schema(schema: Any) -> dict[str, Any]:
    """The structured-output (and eval `schema_valid`) subset with its limits: depth 5, 100
    properties, 16 KiB."""
    return validate(
        schema,
        max_depth=OUTPUT_SCHEMA_MAX_DEPTH,
        max_properties=OUTPUT_SCHEMA_MAX_PROPERTIES,
        max_bytes=OUTPUT_SCHEMA_MAX_BYTES,
        root_object=True,
        dialect="standard",
    )


# --- helpers shared with working memory content validation ---------------------------------------


def json_size(value: Any) -> int:
    """UTF-8 length of `value`'s canonical JSON (sorted keys, compact). Raises TypeError/ValueError."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def check_json_depth(value: Any, what: str, limit: int = MAX_JSON_DEPTH) -> None:
    """Raise SchemaSubsetInvalid if `value` nests objects or arrays deeper than `limit` (iterative)."""
    if json_depth_exceeds(value, limit):
        raise SchemaSubsetInvalid(f"{what} nests deeper than {limit} levels")


def json_depth_exceeds(value: Any, limit: int) -> bool:
    """True if `value` nests objects or arrays deeper than `limit` (iterative, no recursion)."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def is_scalar(value: Any) -> bool:
    """A JSON string, boolean, integer or finite number."""
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, (str, bool, int))


def typed_key(value: Any) -> tuple[str, Any]:
    """A key that tells JSON values apart the way JSON does (True is not 1; 1 and 1.0 are equal)."""
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    return ("string", value)


def matches_type(value: Any, type_: str) -> bool:
    """`value` is an instance of the JSON Schema scalar type `type_` (or null for "null")."""
    if type_ == "null":
        return value is None
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "boolean":
        return isinstance(value, bool)
    if type_ == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
    if type_ == "number":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))
    return False


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and is_scalar(value)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class _Walker:
    def __init__(self, rules: _Rules, *, max_depth: int, max_properties: int) -> None:
        self.rules = rules
        self.max_depth = max_depth
        self.max_properties = max_properties
        self.count = 0

    def node(self, node: Any, path: str, *, depth: int, in_array: bool = False) -> None:
        rules = self.rules
        where = path or "(root)"
        if not isinstance(node, dict):
            raise SchemaSubsetInvalid("a schema must be a JSON object", where)
        unknown = sorted(set(node) - rules.keywords)
        if unknown:
            raise SchemaSubsetInvalid(f"unsupported keyword {unknown[0]!r}", where)
        for key in ("description", "title"):
            if key in node and not isinstance(node[key], str):
                raise SchemaSubsetInvalid(f'"{key}" must be a string', where)
        type_, nullable = self._type(node.get("type"), where)
        if "enum" in node:
            self._enum(node, type_, nullable, where)
            return
        if rules.const and "const" in node:
            self._const(node, type_, nullable, where)
            return
        if type_ in _SCALAR_TYPES or (rules.nullable and type_ == "null"):
            allowed = {"type"} | _DESCRIPTIVE
            if type_ == "string":
                allowed |= rules.string_extra
            elif type_ in ("number", "integer"):
                allowed |= rules.number_extra
            extra = set(node) - allowed
            if extra:
                raise SchemaSubsetInvalid(f"unsupported keyword {sorted(extra)[0]!r} on a {type_}", where)
            self._bounds(node, where)
            return
        if in_array:
            raise SchemaSubsetInvalid("array items must be a string, number, integer, boolean or enum", where)
        if type_ == "array":
            extra = set(node) - ({"type", "items"} | _DESCRIPTIVE | rules.array_extra)
            if extra:
                raise SchemaSubsetInvalid(f"unsupported keyword {sorted(extra)[0]!r} on an array", where)
            if "items" not in node:
                raise SchemaSubsetInvalid('an array needs "items"', where)
            self._bounds(node, where)
            self.node(node["items"], f"{path}[]", depth=depth, in_array=not rules.nested_items)
            return
        if type_ == "object":
            self._object(node, path, where, depth=depth)
            return
        if type_ is None:
            raise SchemaSubsetInvalid(rules.no_type, where)
        if rules is _RULES["working_memory"]:
            raise SchemaSubsetInvalid(f"unsupported type {type_!r}", where)  # RM.2 message, unchanged
        raise SchemaSubsetInvalid("unsupported type", where)

    def _type(self, raw: Any, where: str) -> tuple[Any, bool]:
        """(base type, nullable). Working memory: the raw value, never nullable."""
        if not self.rules.nullable:
            return raw, False
        if isinstance(raw, list):
            if (
                len(raw) != 2
                or "null" not in raw
                or not all(isinstance(t, str) for t in raw)
                or raw[0] == raw[1]
                or any(t not in _ALL_TYPES for t in raw)
            ):
                raise SchemaSubsetInvalid('"type" must be one type or a list of one type and "null"', where)
            return next(t for t in raw if t != "null"), True
        if raw is not None and not isinstance(raw, str):
            raise SchemaSubsetInvalid('"type" must be one type or a list of one type and "null"', where)
        return raw, raw == "null"

    def _value_ok(self, value: Any) -> bool:
        return is_scalar(value) or (self.rules.nullable and value is None)

    def _value_matches(self, value: Any, type_: Any, nullable: bool) -> bool:
        if value is None:
            return nullable
        return matches_type(value, type_)

    def _enum(self, node: dict[str, Any], type_: Any, nullable: bool, where: str) -> None:
        rules = self.rules
        values = node["enum"]
        if not isinstance(values, list) or not 1 <= len(values) <= rules.max_enum:
            raise SchemaSubsetInvalid(f'"enum" must be a list of 1 to {rules.max_enum} values', where)
        if not all(self._value_ok(v) for v in values):
            kinds = "strings, numbers, booleans or null" if rules.nullable else "strings, numbers or booleans"
            raise SchemaSubsetInvalid(f'"enum" values must be {kinds}', where)
        if len({typed_key(v) for v in values}) != len(values):
            raise SchemaSubsetInvalid('"enum" values must be unique', where)
        if type_ is not None:
            if type_ not in _SCALAR_TYPES and not (rules.nullable and type_ == "null"):
                raise SchemaSubsetInvalid('an "enum" may only have a scalar "type"', where)
            if not all(self._value_matches(v, type_, nullable) for v in values):
                raise SchemaSubsetInvalid(f'"enum" values must all be of type {type_}', where)
        extra = set(node) - {"enum", "type", "description", "title"}
        if extra:
            raise SchemaSubsetInvalid(f"unsupported keyword {sorted(extra)[0]!r} on an enum", where)

    def _const(self, node: dict[str, Any], type_: Any, nullable: bool, where: str) -> None:
        value = node["const"]
        if not self._value_ok(value):
            raise SchemaSubsetInvalid('"const" must be a string, number, boolean or null', where)
        if type_ is not None:
            if type_ not in _SCALAR_TYPES and type_ != "null":
                raise SchemaSubsetInvalid('a "const" may only have a scalar "type"', where)
            if not self._value_matches(value, type_, nullable):
                raise SchemaSubsetInvalid(f'"const" must be of type {type_}', where)
        extra = set(node) - {"const", "type", "description", "title"}
        if extra:
            raise SchemaSubsetInvalid(f"unsupported keyword {sorted(extra)[0]!r} on a const", where)

    def _bounds(self, node: dict[str, Any], where: str) -> None:
        for low, high, check, what in (
            ("minimum", "maximum", _is_number, "a finite number"),
            ("minLength", "maxLength", _is_count, "a non-negative integer"),
            ("minItems", "maxItems", _is_count, "a non-negative integer"),
        ):
            for key in (low, high):
                if key in node and not check(node[key]):
                    raise SchemaSubsetInvalid(f'"{key}" must be {what}', where)
            if low in node and high in node and node[low] > node[high]:
                raise SchemaSubsetInvalid(f'"{low}" must not be greater than "{high}"', where)
        if "format" in node and not isinstance(node["format"], str):
            raise SchemaSubsetInvalid('"format" must be a string', where)

    def _object(self, node: dict[str, Any], path: str, where: str, *, depth: int) -> None:
        rules = self.rules
        if depth > self.max_depth:
            raise SchemaSubsetInvalid(f"objects may be nested at most {self.max_depth} deep", where)
        extra = set(node) - ({"type", "properties", "required", "additionalProperties"} | _DESCRIPTIVE)
        if extra:
            raise SchemaSubsetInvalid(f'unsupported keyword "{sorted(extra)[0]}" on an object', where)
        properties = node.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaSubsetInvalid('"properties" must be an object', where)
        additional = node.get("additionalProperties", True)
        if not isinstance(additional, bool):
            raise SchemaSubsetInvalid('"additionalProperties" must be true or false', where)
        required = node.get("required", [])
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            raise SchemaSubsetInvalid('"required" must be a list of property names', where)
        if rules.required_declared:
            if len(set(required)) != len(required):
                raise SchemaSubsetInvalid('"required" names must be unique', where)
            undeclared = [r for r in required if r not in properties]
            if undeclared:
                raise SchemaSubsetInvalid('"required" may only name properties declared in "properties"', where)
        for name, child in properties.items():
            if rules.strict_names:
                if not isinstance(name, str) or not PROPERTY_NAME_PATTERN.fullmatch(name):
                    raise SchemaSubsetInvalid("property names must match ^[A-Za-z0-9_-]{1,64}$", where)
            elif not isinstance(name, str) or not name or len(name) > MAX_PROPERTY_NAME_CHARS:
                raise SchemaSubsetInvalid(f"property names must be 1 to {MAX_PROPERTY_NAME_CHARS} characters", where)
            self.count += 1
            if self.count > self.max_properties:
                raise SchemaSubsetInvalid(f"at most {self.max_properties} properties in total")
            self.node(child, _join(path, name), depth=depth + 1)


__all__ = [
    "DRAFT_2020_12",
    "Dialect",
    "PROPERTY_NAME_PATTERN",
    "MAX_JSON_DEPTH",
    "OUTPUT_SCHEMA_MAX_BYTES",
    "OUTPUT_SCHEMA_MAX_DEPTH",
    "OUTPUT_SCHEMA_MAX_ENUM_VALUES",
    "OUTPUT_SCHEMA_MAX_PROPERTIES",
    "SchemaSubsetInvalid",
    "check_json_depth",
    "is_scalar",
    "json_depth_exceeds",
    "json_size",
    "matches_type",
    "typed_key",
    "validate",
    "validate_output_schema",
]
