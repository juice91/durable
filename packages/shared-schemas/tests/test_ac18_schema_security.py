"""AC.18 security: JSON Schema keyword injection and oversize schemas, on every surface that takes a
user schema (structured output `OutputSpec`, the eval `schema_valid` evaluator, and the shared
subset validator itself).

AC.1's tests cover each rejected keyword once at a fixed position and each limit at its boundary.
This file attacks positions: every reference and regex keyword (local and remote `$ref`, `$id`,
`$anchor`, `$dynamicRef`, `$defs`, `pattern`, `patternProperties`, and the conditional and
unevaluated applicators) at the root, inside `properties`, inside `items`, inside a nested object
and inside an array of arrays; a remote `$schema` dialect; and every size limit, including a nesting
bomb far beyond Python's recursion limit. Each refusal names the keyword and the path, never echoes
the attacker's value, and no validation opens a socket.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from eap_schemas.evals import EvaluatorSpec
from eap_schemas.json_schema_subset import SchemaSubsetInvalid, validate_output_schema
from eap_schemas.output import OutputSpec

REMOTE = "https://attacker.example/evil.json"
INJECTIONS: dict[str, Any] = {
    "$ref-local": {"$ref": "#/$defs/x"},
    "$ref-remote": {"$ref": REMOTE},
    "$ref-file": {"$ref": "file:///etc/passwd"},
    "$id": {"$id": REMOTE, "type": "string"},
    "$anchor": {"$anchor": "a", "type": "string"},
    "$dynamicRef": {"$dynamicRef": REMOTE},
    "$defs": {"$defs": {"x": {"type": "string"}}, "type": "string"},
    "pattern": {"type": "string", "pattern": "^(a+)+$"},
    "patternProperties": {"type": "object", "patternProperties": {"^(a+)+$": {"type": "string"}}},
    "if": {"type": "string", "if": {"type": "string"}, "then": {"type": "string"}},
    "unevaluatedProperties": {"type": "object", "unevaluatedProperties": False},
    "dependentSchemas": {"type": "object", "dependentSchemas": {"a": {"type": "object"}}},
    "contentSchema": {"type": "string", "contentSchema": {"$ref": REMOTE}},
}


def placed(where: str, sub: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The injected sub-schema at one position of an otherwise valid schema; (schema, its path)."""
    if where == "root":
        return {**sub, "type": "object", "properties": {"a": {"type": "string"}}}, ""  # the root stays an object
    if where == "property":
        return {"type": "object", "properties": {"a": sub}}, "a"
    if where == "items":
        return {"type": "object", "properties": {"a": {"type": "array", "items": sub}}}, "a"
    if where == "nested":
        return {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": sub}}}}, "a.b"
    assert where == "array-of-arrays"
    return {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "array", "items": sub}}}}, "a"


def keyword_of(name: str) -> str:
    return name.split("-")[0]


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("schema validation opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.mark.parametrize("where", ["root", "property", "items", "nested", "array-of-arrays"])
@pytest.mark.parametrize("name", sorted(INJECTIONS))
def test_reference_and_regex_keywords_are_refused_at_every_position(no_network, name: str, where: str) -> None:
    schema, path = placed(where, INJECTIONS[name])
    with pytest.raises(SchemaSubsetInvalid) as caught:
        validate_output_schema(schema)
    message = str(caught.value)
    assert keyword_of(name) in message or "unsupported keyword" in message, message
    assert path in message
    assert REMOTE not in message and "etc/passwd" not in message and "(a+)+" not in message, message
    # The same schema through the two contracts that take user schemas.
    with pytest.raises(ValidationError) as via_output:
        OutputSpec.model_validate({"json_schema": schema})
    with pytest.raises(ValidationError) as via_evaluator:
        TypeAdapter(EvaluatorSpec).validate_python({"kind": "schema_valid", "json_schema": schema})
    for err in (via_output.value, via_evaluator.value):
        text = json.dumps(err.errors(include_input=False, include_url=False), default=str)
        assert REMOTE not in text and "etc/passwd" not in text, text


@pytest.mark.parametrize("dialect", [REMOTE, "http://json-schema.org/draft-04/schema#", ""])
def test_only_the_2020_12_dialect_and_only_at_the_root(no_network, dialect: str) -> None:
    with pytest.raises(SchemaSubsetInvalid) as caught:
        validate_output_schema({"$schema": dialect, "type": "object", "properties": {}})
    assert REMOTE not in str(caught.value)
    ok = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {}}
    assert validate_output_schema(ok) == ok
    with pytest.raises(SchemaSubsetInvalid):  # not below the root
        validate_output_schema({"type": "object", "properties": {"a": {**ok, "type": "string"}}})


def deep_objects(levels: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(levels):
        schema = {"type": "object", "properties": {"a": schema}}
    return schema


OVERSIZE = {
    "bytes": {"type": "object", "properties": {"a": {"type": "string", "description": "x" * 16_400}}},
    "depth": deep_objects(6),
    "properties": {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(101)}},
    "enum": {"type": "object", "properties": {"a": {"type": "string", "enum": [f"v{i}" for i in range(257)]}}},
}


@pytest.mark.parametrize("what", sorted(OVERSIZE))
def test_oversize_schemas_are_refused_on_every_surface(what: str) -> None:
    schema = OVERSIZE[what]
    with pytest.raises(SchemaSubsetInvalid):
        validate_output_schema(schema)
    with pytest.raises(ValidationError):
        OutputSpec.model_validate({"json_schema": schema})
    with pytest.raises(ValidationError):
        TypeAdapter(EvaluatorSpec).validate_python({"kind": "schema_valid", "json_schema": schema})


def test_a_nesting_bomb_is_refused_fast_without_recursion_errors() -> None:
    bomb: Any = {"type": "string"}
    for _ in range(50_000):  # far past sys.getrecursionlimit()
        bomb = {"type": "array", "items": bomb}
    schema = {"type": "object", "properties": {"a": bomb}}
    started = time.monotonic()
    with pytest.raises(SchemaSubsetInvalid):
        validate_output_schema(schema)
    with pytest.raises(ValidationError):
        OutputSpec.model_validate({"json_schema": schema})
    assert time.monotonic() - started < 2.0
