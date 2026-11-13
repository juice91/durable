"""Pure evaluation of workflow data: `$ref` resolution, input templates and branch conditions.

Nothing here does I/O or reads a clock, so the interpreter (eap_worker.workflow_run) gets the same
answer on every replay from the same recorded step outputs. Nothing is ever evaluated as code.

Context: `{"input": <workflow input>, "steps": {<step_id>: {"output": <that step's output>}}}`.
A `$ref` path is `input[.seg...]` or `steps.<id>.output[.seg...]`; each segment selects an object
key, or a list index when the value is a list and the segment is a non-negative integer.

Condition typing rules (no coercion between types, ever):

- JSON types: null, boolean, number (int or float; booleans are NOT numbers), string, array,
  object. A path that does not resolve is MISSING, which is not null.
- `eq` / `ne`: deep JSON equality; numbers compare by value (1 == 1.0), every other pair of
  different types is unequal (true != 1, "1" != 1, null != MISSING). Any MISSING operand makes
  both `eq` and `ne` false.
- `gt` / `lt`: two numbers compare numerically, two strings by Unicode code point; any other
  combination (including MISSING, null, booleans) is false.
- `contains`: string left + string right -> substring; array left -> some element `eq` right;
  object left + string right -> the key is present; anything else is false.
- `exists`: the left `$ref` resolves to a value other than null.
"""

from __future__ import annotations

import json
import math
from typing import Any

from eap_schemas.workflows import REF_KEY, Condition

__all__ = ["MISSING", "RefMissing", "evaluate", "json_equal", "non_finite_path", "resolve_ref", "resolve_template"]


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


class RefMissing(Exception):
    """A template `$ref` does not resolve (bounded path in args, pickle-safe)."""

    def __init__(self, path: str) -> None:
        self.path = path[:300]
        super().__init__(self.path)

    def __str__(self) -> str:
        return f"$ref '{self.path}' does not resolve"


def resolve_ref(context: dict[str, Any], path: str) -> Any:
    """The value at `path`, or MISSING."""
    parts = path.split(".")
    if parts[0] == "input":
        value: Any = context.get("input", MISSING)
        rest = parts[1:]
    elif parts[0] == "steps" and len(parts) >= 3 and parts[2] == "output":
        step = context.get("steps", {}).get(parts[1])
        if not isinstance(step, dict) or "output" not in step:
            return MISSING
        value = step["output"]
        rest = parts[3:]
    else:
        return MISSING
    for seg in rest:
        if isinstance(value, dict):
            if seg not in value:
                return MISSING
            value = value[seg]
        elif isinstance(value, list) and seg.isdigit() and int(seg) < len(value):
            value = value[int(seg)]
        else:
            return MISSING
    return value


def _ref_path(value: Any) -> str | None:
    if isinstance(value, dict) and set(value) == {REF_KEY} and isinstance(value[REF_KEY], str):
        return value[REF_KEY]
    return None


def resolve_template(template: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """A step's input mapping with every `$ref` value replaced; RefMissing if one does not resolve."""
    resolved: dict[str, Any] = {}
    for key, value in template.items():
        path = _ref_path(value)
        if path is None:
            resolved[key] = value
            continue
        found = resolve_ref(context, path)
        if found is MISSING:
            raise RefMissing(path)
        resolved[key] = found
    return resolved


def _operand(value: Any, context: dict[str, Any]) -> Any:
    path = _ref_path(value)
    return value if path is None else resolve_ref(context, path)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def json_equal(a: Any, b: Any) -> bool:
    """Deep JSON equality with the typing rules above (iterative; inputs are depth-bounded)."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if x is MISSING or y is MISSING:
            return False
        if _is_number(x) and _is_number(y):
            if not (x == y and not (isinstance(x, float) and math.isnan(x))):
                return False
            continue
        if isinstance(x, bool) or isinstance(y, bool):
            if not (isinstance(x, bool) and isinstance(y, bool) and x == y):
                return False
            continue
        if x is None or y is None:
            if not (x is None and y is None):
                return False
            continue
        if isinstance(x, str) or isinstance(y, str):
            if not (isinstance(x, str) and isinstance(y, str) and x == y):
                return False
            continue
        if isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
            continue
        if isinstance(x, dict) and isinstance(y, dict):
            if set(x) != set(y):
                return False
            stack.extend((x[k], y[k]) for k in x)
            continue
        return False
    return True


def evaluate(condition: Condition | dict[str, Any], context: dict[str, Any]) -> bool:
    """Evaluate one data-only condition against the context (see the module docstring)."""
    if isinstance(condition, dict):
        condition = Condition.model_validate(condition)
    left = _operand(condition.left, context)
    op = condition.op
    if op == "exists":
        return left is not MISSING and left is not None
    right = _operand(condition.right, context)
    if left is MISSING or right is MISSING:
        return False
    if op == "eq":
        return json_equal(left, right)
    if op == "ne":
        return not json_equal(left, right)
    if op in ("gt", "lt"):
        if _is_number(left) and _is_number(right):
            return left > right if op == "gt" else left < right
        if isinstance(left, str) and isinstance(right, str):
            return left > right if op == "gt" else left < right
        return False
    if op == "contains":
        if isinstance(left, str):
            return isinstance(right, str) and right in left
        if isinstance(left, list):
            return any(json_equal(item, right) for item in left)
        if isinstance(left, dict):
            return isinstance(right, str) and right in left
        return False
    return False  # pragma: no cover - ConditionOp is closed


def non_finite_path(value: Any) -> list[int | str] | None:
    """The path of the first non-finite float (NaN, Infinity, -Infinity) inside a JSON-like value,
    or None. Iterative; NaN and Infinity are not JSON and JSONB rejects them."""
    stack: list[tuple[Any, list[int | str]]] = [(value, [])]
    while stack:
        item, path = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return path
        if isinstance(item, dict):
            stack.extend((v, [*path, k]) for k, v in item.items())
        elif isinstance(item, (list, tuple)):
            stack.extend((v, [*path, i]) for i, v in enumerate(item))
    return None


def canonical_json(value: Any) -> str:
    """Canonical JSON text; ValueError for a non-finite float (NaN/Infinity are not JSON and JSONB
    rejects them), so no such value ever reaches an event, a run row or a checkpoint."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def json_size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))
