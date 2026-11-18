"""Pure evaluation of workflow data: `$ref` resolution, input templates and branch conditions.

Nothing here does I/O or reads a clock, so the interpreter (eap_worker.workflow_run) gets the same
answer on every replay from the same recorded step outputs. Nothing is ever evaluated as code.

Context: `{"input": <workflow input>, "steps": {<step_id>: {"output": <that step's output>}},
"loop": {<loop_id>: {"index": <int>, "item": <foreach item>, "state": <loop state>}}}`. The `loop`
key is present only inside an iteration (or while a `while` is checked); it holds one frame per
enclosing loop, so a nested body reads both loops. A foreach frame has no `state` and a `loop`
frame has no `item` (see `with_loop_frame`). A `$ref` path is `input[.seg...]`,
`steps.<id>.output[.seg...]` or `loop.<L>.index|item|state[.seg...]`; each segment selects an
object key, or a list index when the value is a list and the segment is a non-negative integer.
A loop root whose frame (or whose `item`/`state` slot) is not in the context is MISSING.

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

Three-valued (Kleene) logic (D9, LP.3 review 2026-11-15): a comparison with a MISSING operand
(other than `exists`, which is always true or false) is UNKNOWN. `{"not": c}` of UNKNOWN is
UNKNOWN. `{"all": [...]}` is false if any operand is false, else UNKNOWN if any is UNKNOWN, else
true. `{"any": [...]}` is true if any operand is true, else UNKNOWN if any is UNKNOWN, else false.
At the top level UNKNOWN counts as NOT satisfied: a branch case is not taken, a `while` stops, an
`until` is not done (the loop runs on, to its max_iterations or until the condition resolves). So
a missing output field can never make a loop exit "by condition", e.g.
`{"not": {"op": "eq", "left": {"$ref": "input.nope"}, "right": 1}}` does NOT hold. `evaluate`
returns a bool; `evaluate3` / `outcome` expose UNKNOWN so the interpreter can record a
`condition_unknown` warning. A bare comparison behaves exactly as before (MISSING -> false).
Different JSON types (e.g. `gt` of a string and a number) are false, not UNKNOWN: only MISSING
is unknown.

Loops (D3): the parent evaluates a foreach's `items` (`foreach_items`), a loop's initial `state`
(`initial_state`) and its `while` (`while_holds`) over the outer scope. Inside the iteration, after
the body, `iteration_exit` evaluates `carry` over the state BEFORE the update, then `until` over
the state AFTER it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import TypeAdapter

from eap_secrets.redaction import REDACTED
from eap_schemas.workflows import (
    REF_KEY,
    AllCondition,
    AnyCondition,
    Condition,
    Expression,
    ForeachStep,
    LoopStep,
    NotCondition,
    TEMPLATE_KEY,
    is_template,
    parse_placeholders,
)

__all__ = [
    "MISSING",
    "ConditionOutcome",
    "IterationExit",
    "LoopItemsError",
    "RefMissing",
    "build_context",
    "evaluate",
    "evaluate3",
    "foreach_items",
    "initial_state",
    "iteration_exit",
    "json_copy",
    "json_equal",
    "non_finite_path",
    "outcome",
    "resolve_ref",
    "mask_recorded_input",
    "resolve_template",
    "while_holds",
    "while_outcome",
    "with_loop_frame",
]


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()

_LOOP_ROOTS = frozenset({"index", "item", "state"})
_EXPRESSION: TypeAdapter[Any] = TypeAdapter(Expression)


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
    elif parts[0] == "loop" and len(parts) >= 3 and parts[2] in _LOOP_ROOTS:
        frames = context.get("loop")
        frame = frames.get(parts[1]) if isinstance(frames, Mapping) else None
        if not isinstance(frame, Mapping) or parts[2] not in frame:
            return MISSING
        value = frame[parts[2]]
        if value is MISSING or (parts[2] == "index" and len(parts) > 3):
            return MISSING
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


def _scalar_text(value: Any) -> str | None:
    """A placeholder value inside a longer string: a string as is, a number or boolean in JSON form."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return None


def _render_string(text: str, context: dict[str, Any]) -> Any:
    """One template string: a string that is exactly one placeholder is the value itself (any JSON type); one
    with other text around it is concatenated (each value a string, number or boolean). The resolved values
    are never scanned again for placeholders."""
    spans = parse_placeholders(text)
    if not spans:
        return text
    if len(spans) == 1 and (spans[0][0], spans[0][1]) == (0, len(text)):
        found = resolve_ref(context, spans[0][2])
        if found is MISSING:
            raise RefMissing(spans[0][2])
        return found
    parts: list[str] = []
    pos = 0
    for start, end, path in spans:
        found = resolve_ref(context, path)
        rendered = None if found is MISSING else _scalar_text(found)
        if rendered is None:
            raise RefMissing(path)
        parts.extend((text[pos:start], rendered))
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def render_template(inner: Any, context: dict[str, Any]) -> Any:
    """The JSON value of a `{"$template": inner}` with its placeholders resolved (keys are never templated);
    RefMissing for a placeholder that does not resolve or is not a scalar inside a longer string."""
    if isinstance(inner, str):
        return _render_string(inner, context)
    if isinstance(inner, list):
        return [render_template(v, context) for v in inner]
    if isinstance(inner, dict):
        return {k: render_template(v, context) for k, v in inner.items()}
    return inner


SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "x-api-key"})


def sensitive_header(name: str) -> bool:
    lowered = name.lower()
    return lowered in SENSITIVE_HEADERS or "token" in lowered or "secret" in lowered


def mask_recorded_input(resolved: dict[str, Any]) -> dict[str, Any]:
    """A tool step's resolved input as it is RECORDED (events, spans, the run row): the values of sensitive
    headers (authorization, cookie, proxy-authorization, x-api-key, any name containing "token" or "secret",
    case-insensitively) of a `headers` object are replaced by REDACTED. The live call keeps the real values."""
    headers = resolved.get("headers")
    if not isinstance(headers, dict) or not any(sensitive_header(str(k)) for k in headers):
        return resolved
    return {**resolved, "headers": {k: REDACTED if sensitive_header(str(k)) else v for k, v in headers.items()}}


def resolve_template(template: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """A step's input mapping with every `$ref` and `$template` value replaced; RefMissing if one does not
    resolve."""
    resolved: dict[str, Any] = {}
    for key, value in template.items():
        if is_template(value):
            resolved[key] = render_template(value[TEMPLATE_KEY], context)
            continue
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


ConditionOutcome = Literal["true", "false", "unknown"]


def evaluate3(condition: Any, context: dict[str, Any]) -> bool | None:
    """Evaluate one data-only condition (a comparison or an all/any/not combinator, as a model or
    its JSON) in Kleene three-valued logic: True, False, or None for UNKNOWN (see the module
    docstring). Recursion is bounded by the schema's MAX_CONDITION_DEPTH."""
    if isinstance(condition, dict):
        condition = _EXPRESSION.validate_python(condition)
    if isinstance(condition, AllCondition):
        results = [evaluate3(c, context) for c in condition.all]
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True
    if isinstance(condition, AnyCondition):
        results = [evaluate3(c, context) for c in condition.any]
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    if isinstance(condition, NotCondition):
        inner = evaluate3(condition.not_, context)
        return None if inner is None else not inner
    if not isinstance(condition, Condition):
        raise TypeError(f"not a workflow condition: {type(condition).__name__}")
    return _compare(condition, context)


def outcome(condition: Any, context: dict[str, Any]) -> ConditionOutcome:
    """`evaluate3` as a label: "true", "false" or "unknown" (for a `condition_unknown` warning)."""
    result = evaluate3(condition, context)
    return "unknown" if result is None else ("true" if result else "false")


def evaluate(condition: Any, context: dict[str, Any]) -> bool:
    """Whether the condition is satisfied: `evaluate3` is True (UNKNOWN counts as not satisfied)."""
    return evaluate3(condition, context) is True


def _compare(condition: Condition, context: dict[str, Any]) -> bool | None:
    left = _operand(condition.left, context)
    op = condition.op
    if op == "exists":
        return left is not MISSING and left is not None
    right = _operand(condition.right, context)
    if left is MISSING or right is MISSING:
        return None  # UNKNOWN
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


# --------------------------------------------------------------------------- loops (D3)


class LoopItemsError(Exception):
    """A foreach's `items` is not a list (`loop_items_not_list`) or is longer than `max_items`
    (`loop_items_exceeded`). Pickle-safe: args are the constructor arguments."""

    def __init__(self, code: str, step_id: str, count: int | None = None, limit: int | None = None) -> None:
        self.code = code
        self.step_id = step_id
        self.count = count
        self.limit = limit
        super().__init__(code, step_id, count, limit)

    def __str__(self) -> str:
        if self.code == "loop_items_exceeded":
            return f"step {self.step_id!r}: {self.count} items exceed max_items {self.limit}"
        return f"step {self.step_id!r}: the foreach items value is not a list"


@dataclass(frozen=True)
class IterationExit:
    """What an iteration hands back after its body: the carried state (a fresh copy, sharing
    nothing with the context or the spec), whether `until` holds (always False for a `while` loop,
    whose condition the parent checks before the next one), and the `until` outcome ("unknown"
    means a MISSING operand made it UNKNOWN, so the loop continues; None for a `while` loop)."""

    state: dict[str, Any]
    done: bool
    until: ConditionOutcome | None = None


def json_copy(value: Any) -> Any:
    """A deep copy of a JSON-like value (dicts and lists copied, scalars shared because they are
    immutable). Iterative, so a deeply nested value never hits the recursion limit."""
    if not isinstance(value, (dict, list)):
        return value
    root: Any = {} if isinstance(value, dict) else []
    stack: list[tuple[Any, Any]] = [(value, root)]
    while stack:
        src, dst = stack.pop()
        items = src.items() if isinstance(src, dict) else enumerate(src)
        for key, item in items:
            if isinstance(item, (dict, list)):
                child: Any = {} if isinstance(item, dict) else []
                stack.append((item, child))
            else:
                child = item
            if isinstance(dst, dict):
                dst[key] = child
            else:
                dst.append(child)
    return root


def build_context(
    input: Any,
    outputs: Mapping[str, Any],
    loops: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """A fresh evaluation context from the workflow input, the readable step outputs
    (`{step_id: output}`) and the enclosing loop frames (`{loop_id: frame}`, see
    `with_loop_frame`). The `loop` key is omitted when there are no frames, so a loop-free context
    is exactly today's shape."""
    context: dict[str, Any] = {"input": input, "steps": {k: {"output": v} for k, v in outputs.items()}}
    if loops:
        context["loop"] = {k: dict(v) for k, v in loops.items()}
    return context


def with_loop_frame(
    context: Mapping[str, Any],
    loop_id: str,
    index: int,
    *,
    item: Any = MISSING,
    state: Any = MISSING,
) -> dict[str, Any]:
    """A new context with the frame of loop `loop_id` set (replacing any earlier frame of that loop)
    and every other key and frame kept. Pass `item` for a foreach and `state` for a `loop`; a slot
    left MISSING is not in the frame, so its ref is MISSING. `context` is not modified."""
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError(f"a loop index must be a non-negative int, got {index!r}")
    frame: dict[str, Any] = {"index": index}
    if item is not MISSING:
        frame["item"] = item
    if state is not MISSING:
        frame["state"] = state
    frames = dict(context.get("loop") or {})
    frames[loop_id] = frame
    return {**context, "loop": frames}


def foreach_items(step: ForeachStep, context: dict[str, Any]) -> list[Any]:
    """The list a foreach iterates, read from the outer scope, as a fresh copy (sharing nothing
    with the context). RefMissing when `items` does not resolve; LoopItemsError
    `loop_items_not_list` when it is not a list, `loop_items_exceeded` when it has more than
    `max_items` elements (never truncated)."""
    path = step.items[REF_KEY]
    items = resolve_ref(context, path)
    if items is MISSING:
        raise RefMissing(path)
    if not isinstance(items, list):
        raise LoopItemsError("loop_items_not_list", step.id)
    if len(items) > step.max_items:
        raise LoopItemsError("loop_items_exceeded", step.id, len(items), step.max_items)
    return json_copy(items)


def initial_state(step: LoopStep, context: dict[str, Any]) -> dict[str, Any]:
    """The loop's state before iteration 0: its `state` template over the outer scope, or `{}`
    when it has none, as a fresh copy (sharing nothing with the spec's literals or the context).
    RefMissing when a `$ref` does not resolve."""
    return json_copy(resolve_template(step.state, context)) if step.state is not None else {}


def while_outcome(step: LoopStep, context: dict[str, Any], index: int, state: dict[str, Any]) -> ConditionOutcome:
    """The outcome of `while` before iteration `index`: the parent evaluates it over the outer
    scope plus `loop.<L>.index` (= `index`) and `loop.<L>.state` (the state carried out of
    iteration `index` - 1, or the initial state). "true" for an `until` loop (only the iteration
    cap stops it before its condition). "unknown" stops the loop like "false"."""
    if step.while_ is None:
        return "true"
    return outcome(step.while_, with_loop_frame(context, step.id, index, state=state))


def while_holds(step: LoopStep, context: dict[str, Any], index: int, state: dict[str, Any]) -> bool:
    """Whether iteration `index` starts: `while_outcome` is "true"."""
    return while_outcome(step, context, index, state) == "true"


def iteration_exit(step: LoopStep, context: dict[str, Any]) -> IterationExit:
    """Evaluate `carry` then `until` inside an iteration, after its body. `context` is the
    iteration's context: the body step outputs and the loop's own frame with its index and the
    state carried INTO this iteration (a loop always has a state, `{}` without a template, so a
    frame without one is a wiring error: ValueError). `carry` reads that state and replaces it
    (no `carry` keeps it); `until` then reads the UPDATED state. The returned state is a fresh
    copy. RefMissing when a `carry` `$ref` does not resolve."""
    frame = (context.get("loop") or {}).get(step.id)
    if not isinstance(frame, Mapping) or "index" not in frame:
        raise ValueError(f"the context has no frame for loop {step.id!r}")
    if not isinstance(frame.get("state"), dict):
        raise ValueError(f"the frame of loop {step.id!r} has no state")
    state = frame["state"]
    if step.carry is not None:
        state = resolve_template(step.carry, context)
    state = json_copy(state)
    if step.until is None:
        return IterationExit(state=state, done=False)
    until = outcome(step.until, with_loop_frame(context, step.id, frame["index"], state=state))
    return IterationExit(state=state, done=until == "true", until=until)


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
