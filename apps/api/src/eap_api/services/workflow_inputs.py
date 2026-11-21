"""Checks of a workflow run's `input`: finite numbers and the version's input schema (no web imports)."""

from __future__ import annotations

import math
from typing import Any

from eap_api.text_checks import MAX_REPORTED_ERRORS


def non_finite_errors(value: Any) -> list[dict[str, Any]]:
    """One compact error per non-finite number (NaN, Infinity, -Infinity) anywhere in the input:
    the JSON parser accepts them and jsonschema treats NaN as a number, but they are not JSON and
    can never be stored. Iterative; the body depth is already capped."""
    errors: list[dict[str, Any]] = []
    stack: list[tuple[Any, list[Any]]] = [(value, ["body", "input"])]
    while stack and len(errors) < MAX_REPORTED_ERRORS:
        item, loc = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            errors.append({"loc": loc, "msg": "must be a finite number (NaN and Infinity are not JSON)",
                           "type": "finite_number"})
        elif isinstance(item, dict):
            stack.extend((v, [*loc, str(k)[:100]]) for k, v in item.items())
        elif isinstance(item, list):
            stack.extend((v, [*loc, i]) for i, v in enumerate(item))
    return errors


def input_schema_errors(schema: dict[str, Any], value: Any) -> list[dict[str, Any]]:
    """`value` checked against the version's input_schema with an EMPTY referencing.Registry (no
    remote $ref is ever retrieved). Errors name the input path and the failing JSON-schema keyword,
    never jsonschema's message (it quotes the rejected value). An invalid or unresolvable schema
    is one error at loc ["body", "input"]."""
    import jsonschema
    from referencing import Registry

    try:
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        errors = sorted(cls(schema, registry=Registry()).iter_errors(value), key=lambda e: list(e.absolute_path))
        found = []
        for err in errors[:MAX_REPORTED_ERRORS]:
            loc = ["body", "input", *[p if isinstance(p, int) else str(p)[:100] for p in err.absolute_path]]
            found.append({"loc": loc, "msg": f"does not satisfy the input schema's '{err.validator}' keyword",
                          "type": f"json_schema.{err.validator}"})
        return found
    except Exception:  # noqa: BLE001 - SchemaError, an unresolvable $ref, ...
        return [{"loc": ["body", "input"], "msg": "the workflow's input schema could not be applied",
                 "type": "json_schema.invalid_schema"}]
