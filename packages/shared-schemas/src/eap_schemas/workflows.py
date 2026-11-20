"""Declarative workflow definitions (milestone 3, task 3.7).

A workflow is DATA: a JSON graph of typed steps (agent, tool, branch, parallel, approval). Inputs
are template mappings whose values are JSON literals, ``{"$ref": "<path>"}`` or ``{"$template": <json>}``
(a literal whose strings may hold ``{{ <ref path> }}`` placeholders: a header ``"Bearer {{ steps.s.output.token }}"``
or a nested body built from step outputs); branch conditions
are ``{"op", "left", "right"}`` objects. Nothing here is ever evaluated as code.

``WorkflowSpec`` validates the graph: unique step ids, existing targets, a DAG reachable from
``start``, parallel blocks that stay inside their join, and ``$ref`` paths that only read the
workflow input or steps that have run before the current step on every path.

``$ref`` availability rules (the run-time interpreter must apply the same ones):

- ``steps.<id>.output`` is readable from step S only if ``<id>`` has finished before S starts on
  every path from ``start``: a skipped branch arm does not count, nor does a sibling parallel
  branch.
- A parallel step has NO output: ``steps.<parallel>.output`` is rejected everywhere, including
  inside its own branches and after its join. Read the outputs of the steps inside its branches
  instead; from the join on, every step that ends a branch (or that runs on every path through it)
  is readable.
- A branch root starts when its parallel step forks, so it can read what was readable at the
  parallel step itself, never anything produced inside the parallel block.

Loops (workflow loops milestone, LP.1; plan D1-D3, D8, D9):

- ``foreach`` and ``loop`` (while/until) are BLOCK steps with a nested ``body: {start, steps}``,
  validated like a top-level graph (targets inside the body, acyclic, reachable). A body step with
  no ``next`` ends the iteration. Step ids are unique across the whole spec, ``MAX_STEPS`` counts
  every step at every depth and loops nest at most ``MAX_LOOP_DEPTH`` (2) deep. A body may hold
  ``agent``, ``tool``, ``branch``, ``evaluate`` and nested loops; never ``approval`` or
  ``parallel`` (``loop_body_step_not_allowed``). No loop step sits in a parallel branch
  (``loop_in_parallel_not_allowed``).
- ``loop.<L>.index`` (0-based), ``loop.<L>.item`` (foreach only) and ``loop.<L>.state[.seg]``
  (loop only) name an ENCLOSING loop ``L``.
- A body step reads ``input``, every step readable at the loop step itself, body steps finished
  before it on every path in the same iteration, and the ``loop.*`` roots of enclosing loops.
  Body steps are never readable outside their body; there only ``steps.<L>.output`` is, and never
  from inside ``L``'s own body.
- ``items`` (foreach) and the initial ``state`` template are evaluated by the parent over the
  outer scope. ``while`` is evaluated by the parent before each iteration: the outer scope plus
  ``loop.<L>.index`` and ``loop.<L>.state``, never a body step. ``carry`` then ``until`` run
  inside the iteration after the body: they also read every body step that finishes on EVERY
  path through the body (``loop_ref_not_on_every_path`` otherwise).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
    model_serializer, model_validator,
)

from eap_schemas._omit import OmitNoneModel
from eap_schemas.evals import EvaluatorSpec
from eap_schemas.limits import (
    COST_DECIMAL_PLACES,
    MAX_COST_USD,
    MIN_COST_USD,
    normalise_usd,
)

__all__ = (
    "MAX_CONDITION_DEPTH",
    "MAX_CONDITION_OPERANDS",
    "MAX_EVALUATE_EVALUATORS",
    "MAX_FOREACH_CONCURRENCY",
    "MAX_LITERAL_DEPTH",
    "MAX_LOOP_DEPTH",
    "MAX_LOOP_ITERATIONS",
    "MAX_SPEC_BYTES",
    "MAX_SPEC_DEPTH",
    "MAX_STEPS",
    "MAX_TEMPLATE_PLACEHOLDERS",
    "PAUSE_STEP_SUFFIX",
    "TEMPLATE_KEY",
    "AgentStep",
    "AllCondition",
    "AnyCondition",
    "ApprovalStep",
    "InputStep",
    "BranchCase",
    "BranchStep",
    "Condition",
    "EvaluateStep",
    "Expression",
    "ForeachStep",
    "LoopBody",
    "LoopStep",
    "NotCondition",
    "ParallelStep",
    "Step",
    "ToolStep",
    "WorkflowLimits",
    "WorkflowSpec",
    "body_reads",
    "canonical_workflow_hash",
    "iter_refs",
    "loop_exit_refs",
    "nesting_depth",
    "parse_ref",
    "run_concurrency",
    "static_execution_bound",
    "step_targets",
    "walk_steps",
)

MAX_STEPS = 50
MAX_SPEC_BYTES = 64 * 1024
MAX_LITERAL_DEPTH = 32  # nesting levels of one template value or condition literal
MAX_SPEC_DEPTH = 64  # nesting levels of the whole spec document
STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PAUSE_STEP_SUFFIX = "_pause"  # reserved: a step-by-step run waits under `{step_id}_pause`
TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SEGMENT = r"[A-Za-z0-9_-]{1,64}"
REF_PATTERN = re.compile(
    r"^(?:input"
    r"|steps\.(?P<step>[a-z][a-z0-9_]{0,63})\.output"
    r"|loop\.(?P<loop>[a-z][a-z0-9_]{0,63})\.(?P<loop_root>index|item|state))"
    rf"(?P<rest>(?:\.{_SEGMENT}){{0,16}})$"
)
REF_KEY = "$ref"
TEMPLATE_KEY = "$template"
MAX_TEMPLATE_PLACEHOLDERS = 64
_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
ConditionOp = Literal["eq", "ne", "gt", "lt", "contains", "exists"]

MAX_LOOP_ITERATIONS = 1000  # schema ceiling of max_items / max_iterations (the environment cap is lower)
MAX_FOREACH_CONCURRENCY = 8
MAX_LOOP_DEPTH = 2  # a loop inside a loop body is depth 2; nothing deeper
MAX_CONDITION_OPERANDS = 8  # per all/any
MAX_CONDITION_DEPTH = 4  # nesting levels of all/any/not
MAX_EVALUATE_EVALUATORS = 8
LOOP_KINDS = ("foreach", "loop")
BODY_STEP_KINDS = ("agent", "tool", "branch", "evaluate", "foreach", "loop")


def _ref_path(value: Any) -> str | None:
    """The path of a ``{"$ref": path}`` value, None for a literal; raises on a malformed ref."""
    if not isinstance(value, dict) or REF_KEY not in value:
        return None
    if set(value) != {REF_KEY} or not isinstance(value[REF_KEY], str):
        raise ValueError('a $ref must be exactly {"$ref": "<path>"} with a string path')
    path = value[REF_KEY]
    match = REF_PATTERN.fullmatch(path)
    if not match:
        raise ValueError(
            f"invalid $ref '{path}': must be 'input[.field...]', 'steps.<step_id>.output[.field...]' or "
            "'loop.<loop_id>.index|item|state[.field...]'"
        )
    if match.group("loop_root") == "index" and match.group("rest"):
        raise ValueError(f"invalid $ref '{path}': loop.<loop_id>.index is a number and has no fields")
    return path


def is_template(value: Any) -> bool:
    """Whether `value` is a ``{"$template": ...}`` input value (the key makes it one)."""
    return isinstance(value, dict) and TEMPLATE_KEY in value


def parse_placeholders(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, path)`` of each ``{{ <ref path> }}`` in `text`; raises on a malformed one (a path that
    is not a valid ref, a stray ``{{`` or ``}}``)."""
    found: list[tuple[int, int, str]] = []
    rest = []
    pos = 0
    for match in _PLACEHOLDER.finditer(text):
        path = match.group(1).strip()
        _ref_path({REF_KEY: path})  # raises ValueError for a path that is not a valid ref
        found.append((match.start(), match.end(), path))
        rest.append(text[pos:match.start()])
        pos = match.end()
    rest.append(text[pos:])
    if any("{{" in part or "}}" in part for part in rest):
        raise ValueError(f"a placeholder in '{text[:60]}' is not of the form {{{{ <ref path> }}}}")
    return found


def template_paths(inner: Any) -> list[str]:
    """Every ref path of the placeholders in the strings (not the keys) of a template's JSON value, in
    order; raises ValueError for a malformed placeholder or more than MAX_TEMPLATE_PLACEHOLDERS."""
    paths: list[str] = []
    stack = [inner]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            paths.extend(path for _s, _e, path in parse_placeholders(item))
        elif isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
        if len(paths) > MAX_TEMPLATE_PLACEHOLDERS:
            raise ValueError(f"a template holds at most {MAX_TEMPLATE_PLACEHOLDERS} placeholders")
    return paths


def parse_ref(path: str) -> tuple[str, str | None, str | None]:
    """``(root, name, loop_root)`` of a valid ref path: ``("input", None, None)``,
    ``("steps", <step_id>, None)`` or ``("loop", <loop_id>, "index"|"item"|"state")``."""
    match = REF_PATTERN.fullmatch(path)
    if match is None:
        raise ValueError(f"invalid $ref '{path}'")
    if match.group("step") is not None:
        return "steps", match.group("step"), None
    if match.group("loop") is not None:
        return "loop", match.group("loop"), match.group("loop_root")
    # For input refs, extract the first field from the rest group
    rest = match.group("rest") or ""
    fields = rest.split(".")
    name = fields[1] if len(fields) > 1 else None  # fields[0] is empty (the leading .)
    return "input", name, None


def nesting_depth(value: Any, limit: int) -> int:
    """Container nesting depth of a JSON-like value (a scalar is 0, ``[]`` or ``{}`` is 1), computed
    without recursion; stops at the first container deeper than ``limit`` and returns its depth."""
    deepest = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list, tuple)):
            depth += 1
            if depth > limit:
                return depth
            deepest = max(deepest, depth)
            stack.extend((child, depth) for child in (item.values() if isinstance(item, dict) else item))
    return deepest


def _check_literal(value: Any, what: str) -> None:
    """A JSON literal at most MAX_LITERAL_DEPTH levels deep, with no $ref anywhere inside it.

    Depth is checked first, so nothing below ever recurses into an unbounded value."""
    if nesting_depth(value, MAX_LITERAL_DEPTH) > MAX_LITERAL_DEPTH:
        raise ValueError(f"{what} is nested more than {MAX_LITERAL_DEPTH} levels deep")
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{what} contains a non-finite number ({item}); JSON numbers must be finite")
        if item is None or isinstance(item, (str, bool, int, float)):
            continue
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict) and all(isinstance(k, str) for k in item):
            if REF_KEY in item:
                raise ValueError("$ref is only allowed as a whole value, not inside a literal")
            stack.extend(item.values())
        else:
            raise ValueError(f"{what} must be a JSON literal or a $ref, got {type(item).__name__}")


def _validate_template(template: Any) -> dict[str, Any]:
    if not isinstance(template, dict):
        raise ValueError("input must be an object mapping field names to literals or $refs")
    for key, value in template.items():
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ValueError("input field names must be 1-128 character strings")
        if is_template(value):
            if set(value) != {TEMPLATE_KEY}:
                raise ValueError('a $template must be exactly {"$template": <json>}')
            _check_literal(value[TEMPLATE_KEY], f"$template of '{key}'")
            template_paths(value[TEMPLATE_KEY])
        elif _ref_path(value) is None:
            _check_literal(value, f"value of '{key}'")
    return template


def _validate_operand(value: Any) -> Any:
    if _ref_path(value) is None:
        _check_literal(value, "operand")
    return value


class Condition(BaseModel):
    """A data-only comparison: ``left <op> right``. Operands are $refs or JSON literals (objects and
    arrays at most MAX_LITERAL_DEPTH deep, no $ref inside); ``exists`` takes only ``left`` (a $ref)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    op: ConditionOp
    left: Any
    right: Any = None

    @field_validator("left", "right")
    @classmethod
    def _operand(cls, value: Any) -> Any:
        return _validate_operand(value)

    @model_validator(mode="after")
    def _exists_shape(self) -> Condition:
        if self.op == "exists":
            if _ref_path(self.left) is None:
                raise ValueError("'exists' needs a $ref as its left operand")
            if self.right is not None:
                raise ValueError("'exists' takes no right operand")
        return self

    def refs(self) -> list[str]:
        return [p for p in (_ref_path(self.left), _ref_path(self.right)) if p is not None]


def _expression_tag(value: Any) -> str | None:
    """Which expression a raw value is: a comparison (``op``) or one of the combinators."""
    if isinstance(value, dict):
        for key in ("op", "all", "any", "not"):
            if key in value:
                return "cmp" if key == "op" else key
        return None
    return {Condition: "cmp", AllCondition: "all", AnyCondition: "any", NotCondition: "not"}.get(type(value))


def _combinator_depth(value: Any) -> int:
    """Nesting levels of all/any/not in a validated expression (a bare comparison is 0)."""
    if isinstance(value, (AllCondition, AnyCondition)):
        return 1 + max(_combinator_depth(c) for c in value.operands)
    if isinstance(value, NotCondition):
        return 1 + _combinator_depth(value.not_)
    return 0


class _Combinator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, serialize_by_alias=True, validate_by_alias=True)

    @model_validator(mode="after")
    def _depth(self) -> Any:
        if _combinator_depth(self) > MAX_CONDITION_DEPTH:
            raise ValueError(f"a condition nests all/any/not more than {MAX_CONDITION_DEPTH} levels deep")
        return self

    @property
    def operands(self) -> tuple[Expression, ...]:
        raise NotImplementedError

    def refs(self) -> list[str]:
        return [p for operand in self.operands for p in operand.refs()]


def _check_operands(value: tuple[Any, ...], key: str) -> tuple[Any, ...]:
    if not 1 <= len(value) <= MAX_CONDITION_OPERANDS:
        raise ValueError(f"'{key}' takes 1 to {MAX_CONDITION_OPERANDS} conditions, got {len(value)}")
    return value


class AllCondition(_Combinator):
    """``{"all": [c, ...]}``: every condition holds (1 to 8 conditions)."""

    all: tuple[Expression, ...]

    @field_validator("all")
    @classmethod
    def _count(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        return _check_operands(value, "all")

    @property
    def operands(self) -> tuple[Expression, ...]:
        return self.all


class AnyCondition(_Combinator):
    """``{"any": [c, ...]}``: at least one condition holds (1 to 8 conditions)."""

    any: tuple[Expression, ...]

    @field_validator("any")
    @classmethod
    def _count(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        return _check_operands(value, "any")

    @property
    def operands(self) -> tuple[Expression, ...]:
        return self.any


class NotCondition(_Combinator):
    """``{"not": c}``: the condition does not hold."""

    not_: Expression = Field(alias="not")

    @property
    def operands(self) -> tuple[Expression, ...]:
        return (self.not_,)


Expression = Annotated[
    Union[
        Annotated[Condition, Tag("cmp")],
        Annotated[AllCondition, Tag("all")],
        Annotated[AnyCondition, Tag("any")],
        Annotated[NotCondition, Tag("not")],
    ],
    Discriminator(
        _expression_tag,
        custom_error_type="invalid_condition",
        custom_error_message=(
            'a condition must be a comparison {"op", "left", "right"} or {"all": [...]}, {"any": [...]} or '
            '{"not": ...}'
        ),
    ),
]
"""A branch, while or until condition: a bare comparison (unchanged JSON) or a combinator."""

for _model in (AllCondition, AnyCondition, NotCondition):
    _model.model_rebuild()


def _check_step_id(value: str, what: str = "step id") -> str:
    if not isinstance(value, str) or not STEP_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{what} '{value}' must match ^[a-z][a-z0-9_]{{0,63}}$")
    return value


_STEP_TAGS = frozenset({"agent", "tool", "branch", "parallel", "approval", "input", "foreach", "loop", "evaluate"})


def _json_loc(loc: tuple[Any, ...]) -> list[Any]:
    """An error location as a JSON path: pydantic's union tags are not JSON keys, so leave out a
    step kind after a list index (``body.steps.0.tool``), a bare comparison's ``cmp`` and a
    combinator tag before its own field (``when.all.all.0``)."""
    parts = list(loc)
    kept: list[Any] = []
    for i, part in enumerate(parts):
        if part == "cmp":
            continue
        if part in _STEP_TAGS and i > 0 and isinstance(parts[i - 1], int):
            continue
        if part in ("all", "any", "not") and i + 1 < len(parts) and parts[i + 1] == part:
            continue
        kept.append(part)
    return kept


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _check_step_id(value)

    @model_validator(mode="before")
    @classmethod
    def _on_error_only_on_tool_steps(cls, data: Any) -> Any:
        kind = cls.model_fields["type"].default if "type" in cls.model_fields else None
        if isinstance(data, dict) and "on_error" in data and kind != "tool":
            raise ValueError(f"on_error is only allowed on tool steps, not on a step of type {kind}")
        return data

    @model_validator(mode="wrap")
    @classmethod
    def _name_the_step(cls, data: Any, handler: Any) -> Any:
        """Report a step's field errors as one message naming the step: "step 'x': field: problem"."""
        try:
            return handler(data)
        except ValidationError as exc:
            step_id = data.get("id") if isinstance(data, dict) else None
            if not isinstance(step_id, str):
                raise
            problems = []
            for err in exc.errors():
                where = ".".join(str(part) for part in _json_loc(err["loc"]))
                msg = err["msg"].removeprefix("Value error, ")
                problems.append(f"{where}: {msg}" if where else msg)
            raise ValueError(f"step '{step_id}': " + "; ".join(problems)) from None


class _Sequential(_StepBase):
    """A step that continues to ``next`` (null ends the workflow, or the parallel branch it is in)."""

    next: str | None = None

    @field_validator("next")
    @classmethod
    def _next(cls, value: str | None) -> str | None:
        return value if value is None else _check_step_id(value, "next target")


class _WithInput(_Sequential):
    input: dict[str, Any] = Field(default_factory=dict)

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: Any) -> dict[str, Any]:
        return _validate_template(value)


class AgentStep(_WithInput):
    """Run a tenant agent; ``version`` null means its latest published version at run time."""

    type: Literal["agent"] = "agent"
    agent_id: UUID
    version: int | None = Field(default=None, ge=1)


class ToolStep(_WithInput):
    """Call a platform tool from ``eap_schemas.tools``."""

    type: Literal["tool"] = "tool"
    tool_id: str
    # "continue": an `{error: {code, ...}}` result is kept as the step's output and the run goes on
    # (best-effort steps); "fail" (default) fails the run with the error's code.
    on_error: Literal["fail", "continue"] = "fail"

    @model_serializer(mode="wrap")
    def _omit_default_on_error(self, handler: Any) -> Any:
        """The default is left out of the dump, so every spec saved before `on_error` keeps its canonical JSON and hash."""
        data = handler(self)
        if isinstance(data, dict) and data.get("on_error") == "fail":
            del data["on_error"]
        return data

    @field_validator("tool_id")
    @classmethod
    def _tool_id(cls, value: str) -> str:
        if not TOOL_ID_PATTERN.fullmatch(value):
            raise ValueError(f"tool id '{value}' must match ^[a-z][a-z0-9_.-]{{0,63}}$")
        return value


class ApprovalStep(_Sequential):
    """Pause until a person approves (continue to ``next``) or the timeout passes."""

    type: Literal["approval"] = "approval"
    prompt: str = Field(min_length=1, max_length=2000)
    timeout_hours: int = Field(ge=1, le=168)


MAX_RESUME_SCHEMA_BYTES = 16 * 1024


class InputStep(_Sequential):
    """Pause until a person submits data that satisfies ``resume_schema`` (a JSON Schema object),
    or the timeout passes. The submitted object is the step's output under ``data`` (``decided_by``
    names who submitted it). Unlike an approval step this is a data form, not an approve/reject
    decision; a person may still reject it (the run then fails ``approval_rejected``)."""

    type: Literal["input"] = "input"
    prompt: str = Field(min_length=1, max_length=2000)
    timeout_hours: int = Field(ge=1, le=168)
    resume_schema: dict[str, Any]

    @field_validator("resume_schema")
    @classmethod
    def _resume_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        import jsonschema

        if value.get("type") != "object":
            raise ValueError("resume_schema must be a JSON Schema with type 'object'")
        if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_RESUME_SCHEMA_BYTES:
            raise ValueError(f"resume_schema must be at most {MAX_RESUME_SCHEMA_BYTES // 1024} KiB as JSON")
        if "$ref" in json.dumps(value):
            raise ValueError("resume_schema must not use $ref")
        try:
            jsonschema.validators.validator_for(value).check_schema(value)
        except jsonschema.SchemaError:
            raise ValueError("resume_schema is not a valid JSON Schema") from None
        return value


class BranchCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    when: Expression
    next: str

    @field_validator("next")
    @classmethod
    def _next(cls, value: str) -> str:
        return _check_step_id(value, "case target")


class BranchStep(_StepBase):
    """Go to the first case whose condition holds, else to ``default``."""

    type: Literal["branch"] = "branch"
    cases: tuple[BranchCase, ...]
    default: str

    @field_validator("cases")
    @classmethod
    def _cases(cls, value: tuple[BranchCase, ...]) -> tuple[BranchCase, ...]:
        if not 1 <= len(value) <= 32:
            raise ValueError(f"a branch needs 1 to 32 cases, got {len(value)}")
        return value

    @field_validator("default")
    @classmethod
    def _default(cls, value: str) -> str:
        return _check_step_id(value, "default target")


class ParallelStep(_StepBase):
    """Run 2-8 branches concurrently; ``join`` runs once every branch has finished.

    A branch ends where its path reaches ``join`` or a step with no ``next``.
    """

    type: Literal["parallel"] = "parallel"
    branches: tuple[str, ...] = Field(min_length=2, max_length=8)
    join: str

    @field_validator("branches")
    @classmethod
    def _branches(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for target in value:
            _check_step_id(target, "branch target")
        if len(set(value)) != len(value):
            raise ValueError("parallel branches must be distinct steps")
        return value

    @field_validator("join")
    @classmethod
    def _join(cls, value: str) -> str:
        return _check_step_id(value, "join target")


def _is_none(value: Any) -> bool:
    return value is None


def _require_ref(value: Any, what: str) -> dict[str, str]:
    if _ref_path(value) is None:
        raise ValueError(f'{what} must be a $ref ({{"$ref": "<path>"}})')
    return value


def _optional_template(value: Any) -> dict[str, Any] | None:
    return None if value is None else _validate_template(value)


class LoopBody(BaseModel):
    """The steps one iteration runs: a graph of its own, starting at ``start``. A body step with no
    ``next`` ends the iteration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: str
    steps: tuple[Step, ...]

    @field_validator("start")
    @classmethod
    def _start(cls, value: str) -> str:
        return _check_step_id(value, "body start")

    @field_validator("steps", mode="before")
    @classmethod
    def _not_empty(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)) and not value:
            raise ValueError("a loop body needs at least one step")
        return value


class ForeachStep(_Sequential):
    """Run ``body`` once per element of the list ``items`` (a $ref), at most ``concurrency`` at a
    time. A list longer than ``max_items`` fails the step (``loop_items_exceeded``); it is never
    truncated. Output: the body results in item order (``on_item_error: collect`` puts an error
    entry in a failed item's place)."""

    type: Literal["foreach"] = "foreach"
    items: dict[str, str]
    max_items: int = Field(ge=1, le=MAX_LOOP_ITERATIONS)
    concurrency: int = Field(default=1, ge=1, le=MAX_FOREACH_CONCURRENCY)
    on_item_error: Literal["fail", "collect"] = "fail"
    body: LoopBody

    @field_validator("items", mode="before")
    @classmethod
    def _items(cls, value: Any) -> Any:
        return _require_ref(value, "items")


class LoopStep(_Sequential):
    """Repeat ``body`` while ``while`` holds (checked before each iteration) or until ``until`` holds
    (checked after each), exactly one of them, at most ``max_iterations`` times. ``state`` (a
    template over the outer scope) is the initial loop state; after each iteration ``carry``
    replaces it. Output: ``{iterations, exited, last, state}``."""

    model_config = ConfigDict(serialize_by_alias=True, validate_by_alias=True)

    type: Literal["loop"] = "loop"
    while_: Expression | None = Field(default=None, alias="while", exclude_if=_is_none)
    until: Expression | None = Field(default=None, exclude_if=_is_none)
    max_iterations: int = Field(ge=1, le=MAX_LOOP_ITERATIONS)
    on_max_iterations: Literal["fail", "exit"] = "fail"
    state: dict[str, Any] | None = Field(default=None, exclude_if=_is_none)
    carry: dict[str, Any] | None = Field(default=None, exclude_if=_is_none)
    body: LoopBody

    @field_validator("state", "carry", mode="before")
    @classmethod
    def _templates(cls, value: Any) -> Any:
        return _optional_template(value)

    @model_validator(mode="after")
    def _one_condition(self) -> LoopStep:
        if (self.while_ is None) == (self.until is None):
            raise ValueError(f"step '{self.id}': a loop needs exactly one of 'while' and 'until'")
        return self


_EVALUATE_KINDS_WITHOUT = {
    "max_duration": "there is no run duration to measure",
    "max_cost": "there is no run cost to measure",
}


class EvaluateStep(_Sequential):
    """Score ``value`` (a $ref) with 1 to 8 evaluators; ``pass`` says whether all or any must pass.
    Output: ``{passed, score, reason, results}``."""

    model_config = ConfigDict(serialize_by_alias=True, validate_by_alias=True)

    type: Literal["evaluate"] = "evaluate"
    value: dict[str, str]
    expected: Any = Field(default=None, exclude_if=_is_none)
    evaluators: tuple[EvaluatorSpec, ...] = Field(min_length=1, max_length=MAX_EVALUATE_EVALUATORS)
    pass_: Literal["all", "any"] = Field(default="all", alias="pass")

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: Any) -> Any:
        return _require_ref(value, "value")

    @field_validator("expected")
    @classmethod
    def _expected(cls, value: Any) -> Any:
        return value if value is None else _validate_operand(value)

    @model_validator(mode="after")
    def _evaluators(self) -> EvaluateStep:
        names: set[str] = set()
        for i, evaluator in enumerate(self.evaluators):
            where = f"step '{self.id}': evaluators.{i} ({evaluator.kind})"
            if evaluator.name is not None:
                if evaluator.name in names:
                    raise ValueError(f"step '{self.id}': duplicate evaluator name {evaluator.name!r}")
                names.add(evaluator.name)
            if evaluator.kind in _EVALUATE_KINDS_WITHOUT:
                raise ValueError(
                    f"{where} is not available in an evaluate step: {_EVALUATE_KINDS_WITHOUT[evaluator.kind]}"
                )
            needs_expected = (
                evaluator.kind == "equals_expected"
                or (evaluator.kind == "contains" and evaluator.value is None)
                or (evaluator.kind == "llm_judge" and evaluator.include_expected)
            )
            if needs_expected and self.expected is None:
                raise ValueError(f"{where} needs the step's 'expected'")
            if evaluator.kind == "schema_valid" and evaluator.json_schema is None:
                raise ValueError(f"{where} needs a json_schema (an evaluate step has no agent output schema)")
        return self


Step = Annotated[
    Union[AgentStep, ToolStep, BranchStep, ParallelStep, ApprovalStep, InputStep, ForeachStep, LoopStep, EvaluateStep],
    Field(discriminator="type"),
]

LOOP_STEP_TYPES = (ForeachStep, LoopStep)

for _model in (LoopBody, ForeachStep, LoopStep):
    _model.model_rebuild()


def step_targets(step: Step) -> list[str]:
    """Every step id this step can hand control to (a parallel's join included). A loop's body is
    not a target: the loop step runs it and then continues to ``next``."""
    if isinstance(step, BranchStep):
        return [case.next for case in step.cases] + [step.default]
    if isinstance(step, ParallelStep):
        return [*step.branches, step.join]
    return [step.next] if step.next is not None else []


def _template_refs(template: dict[str, Any] | None) -> list[str]:
    paths: list[str] = []
    for value in (template or {}).values():
        if is_template(value):
            paths.extend(template_paths(value[TEMPLATE_KEY]))
        elif (path := _ref_path(value)) is not None:
            paths.append(path)
    return paths


def iter_refs(step: Step) -> list[str]:
    """Every ``$ref`` path the step reads in ITS OWN scope (where the step sits): input templates,
    branch conditions, an evaluate step's ``value`` and ``expected``, a foreach's ``items``, a
    loop's initial ``state`` and its ``while``. A loop's ``carry`` and ``until`` read the body's
    scope: see ``loop_exit_refs``. Body steps are not included (see ``walk_steps``)."""
    if isinstance(step, (AgentStep, ToolStep)):
        return _template_refs(step.input)
    if isinstance(step, BranchStep):
        return [p for case in step.cases for p in case.when.refs()]
    if isinstance(step, EvaluateStep):
        return [p for p in (_ref_path(step.value), _ref_path(step.expected)) if p is not None]
    if isinstance(step, ForeachStep):
        return [step.items[REF_KEY]]
    if isinstance(step, LoopStep):
        return _template_refs(step.state) + (step.while_.refs() if step.while_ is not None else [])
    return []


def loop_exit_refs(step: Step) -> list[str]:
    """The refs a loop step's ``carry`` then ``until`` read, evaluated inside each iteration after the
    body (empty for every other step)."""
    if isinstance(step, LoopStep):
        return _template_refs(step.carry) + (step.until.refs() if step.until is not None else [])
    return []


def walk_steps(steps: tuple[Step, ...], loops: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Step]]:
    """Every step at every depth, depth first in spec order, with the ids of the loops enclosing it
    (outermost first; empty for a top-level step)."""
    for step in steps:
        yield loops, step
        if isinstance(step, LOOP_STEP_TYPES):
            yield from walk_steps(step.body.steps, (*loops, step.id))


def _loop_bound(step: ForeachStep | LoopStep) -> int:
    return step.max_items if isinstance(step, ForeachStep) else step.max_iterations


def body_reads(step: ForeachStep | LoopStep) -> tuple[str, ...]:
    """Ids of the steps OUTSIDE ``step``'s body whose outputs one iteration reads (sorted): every
    ``steps.<id>`` ref in the body at any depth (nested loops' items, state, while, carry and until
    included) and in ``step``'s own ``carry`` and ``until``. ``input`` and the parent-side refs
    (``items``, the initial ``state``, ``while``) are not included."""
    inner = {s.id for _, s in walk_steps(step.body.steps)}
    paths = list(loop_exit_refs(step))
    for _, s in walk_steps(step.body.steps):
        paths += iter_refs(s) + loop_exit_refs(s)
    reads = {name for root, name, _ in map(parse_ref, paths) if root == "steps" and name not in inner}
    return tuple(sorted(reads))  # type: ignore[arg-type]


def _execution_bound(steps: tuple[Step, ...]) -> int:
    total = 0
    for step in steps:
        total += 1
        if isinstance(step, LOOP_STEP_TYPES):
            total += _loop_bound(step) * _execution_bound(step.body.steps)
    return total


def static_execution_bound(spec: WorkflowSpec) -> int:
    """An upper bound on step executions in one run: each step counts 1 (whether or not a branch
    skips it), and a loop step counts 1 plus its bound (``max_items`` or ``max_iterations``) times
    its body's bound."""
    return _execution_bound(spec.steps)


def _concurrency(steps: tuple[Step, ...]) -> int:
    best = 1
    for step in steps:
        if isinstance(step, LOOP_STEP_TYPES):
            width = step.concurrency if isinstance(step, ForeachStep) else 1
            best = max(best, width * _concurrency(step.body.steps))
    return best


def run_concurrency(spec: WorkflowSpec) -> int:
    """The most iterations one run can execute at once: the largest product of ``concurrency`` of the
    foreach steps along one nesting chain (a while/until loop runs one iteration at a time). 1 for a
    spec with no foreach. Loops never sit in a parallel branch, so parallel fan-out does not count."""
    return _concurrency(spec.steps)


def _labelled_targets(step: Step) -> list[tuple[str, str]]:
    if isinstance(step, BranchStep):
        cases = [(f"case {i} target", case.next) for i, case in enumerate(step.cases, 1)]
        return cases + [("default target", step.default)]
    if isinstance(step, ParallelStep):
        return [("branch target", b) for b in step.branches] + [("join target", step.join)]
    return [("next target", step.next)] if step.next is not None else []


def _check_structure(
    steps: tuple[Step, ...], start: str, prefix: str = "", elsewhere: frozenset[str] = frozenset()
) -> dict[str, Step]:
    """Ids unique, ``start`` and every target inside this graph. ``prefix`` names the loop for a
    body; ``elsewhere`` holds the ids of steps in other graphs (for a clearer message)."""
    by_id: dict[str, Step] = {}
    for step in steps:
        if step.id in by_id:
            raise ValueError(f"duplicate step id '{step.id}'")
        by_id[step.id] = step
    if start not in by_id:
        hint = " in the body" if prefix else ""
        raise ValueError(f"{prefix}start step '{start}' does not exist{hint}")
    joins: dict[str, str] = {}
    for step in steps:
        for label, target in _labelled_targets(step):
            if target not in by_id:
                hint = ""
                if target in elsewhere:
                    hint = (
                        f" in this {'body' if prefix else 'workflow'} (step '{target}' is in another graph: a step "
                        "can only hand control to steps of the same loop body, or of the top level)"
                    )
                raise ValueError(f"{prefix}step '{step.id}': {label} '{target}' does not exist{hint}")
        if isinstance(step, ParallelStep):
            if step.join in step.branches:
                raise ValueError(f"step '{step.id}': join '{step.join}' cannot also be one of its branches")
            if step.join in joins:
                raise ValueError(f"step '{step.join}' is the join of both '{joins[step.join]}' and '{step.id}'")
            joins[step.join] = step.id
    return by_id


def _check_acyclic(by_id: dict[str, Step], prefix: str = "") -> None:
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    path: list[str] = []
    graph = "body graph" if prefix else "workflow graph"

    def visit(node: str) -> None:
        state[node] = 1
        path.append(node)
        for target in step_targets(by_id[node]):
            if state.get(target) == 1:
                cycle = path[path.index(target):] + [target]
                raise ValueError(f"{prefix}{graph} has a cycle: {' -> '.join(cycle)}")
            if target not in state:
                visit(target)
        path.pop()
        state[node] = 2

    for node in by_id:
        if node not in state:
            visit(node)


def _reachable(by_id: dict[str, Step], roots: list[str], stop: str | None = None) -> list[str]:
    """Steps reachable from ``roots`` (roots included), never entering ``stop``; in visit order."""
    seen: dict[str, None] = {}
    todo = [r for r in roots if r != stop]
    while todo:
        node = todo.pop(0)
        if node in seen:
            continue
        seen[node] = None
        todo.extend(t for t in step_targets(by_id[node]) if t != stop and t not in seen)
    return list(seen)


def _check_parallel_scopes(
    by_id: dict[str, Step], preds: dict[str, list[str]]
) -> dict[str, list[tuple[str, set[str]]]]:
    """Each parallel branch's region (steps it runs before the join) must stay inside the block:
    no step after the join, no step shared with a sibling branch or entered from outside, and the
    join is entered only from the parallel step or its branches. Returns the regions per parallel."""
    regions: dict[str, list[tuple[str, set[str]]]] = {}
    for p in by_id.values():
        if not isinstance(p, ParallelStep):
            continue
        after_join = set(_reachable(by_id, [p.join]))
        branch_regions = [(b, set(_reachable(by_id, [b], stop=p.join))) for b in p.branches]
        owner = {}
        for branch, region in branch_regions:
            for node in _reachable(by_id, [branch], stop=p.join):
                if node in after_join:
                    raise ValueError(
                        f"parallel step '{p.id}': branch '{branch}' jumps past the join '{p.join}' to step '{node}'"
                    )
                if node in owner:
                    raise ValueError(
                        f"parallel step '{p.id}': branches '{owner[node]}' and '{branch}' both reach step "
                        f"'{node}' before the join"
                    )
                owner[node] = branch
        for branch, region in branch_regions:
            for node in sorted(region):
                for pred in preds[node]:
                    if pred != p.id and pred not in region:
                        raise ValueError(
                            f"parallel step '{p.id}': step '{node}' in branch '{branch}' is also entered from "
                            f"step '{pred}' outside the parallel block"
                        )
        for pred in preds[p.join]:
            if pred != p.id and pred not in owner:
                raise ValueError(
                    f"step '{pred}' targets join '{p.join}' of parallel step '{p.id}' from outside the parallel block"
                )
        regions[p.id] = branch_regions
    return regions


def _done_sets(
    by_id: dict[str, Step], start: str, preds: dict[str, list[str]], regions: dict[str, list[tuple[str, set[str]]]]
) -> Any:
    """``done(n)`` = steps of this graph guaranteed finished when ``n`` starts. A join starts after
    all its branches, so it adds, per branch, what is guaranteed at every end of that branch (steps
    whose path ends there or goes to the join; ends inside a nested parallel continue at that inner
    join)."""
    join_of = {p.join: p for p in by_id.values() if isinstance(p, ParallelStep)}
    inside = {pid: set().union(*(r for _, r in rs)) for pid, rs in regions.items()}
    nested: dict[str, set[str]] = {}  # steps inside a parallel nested in this one
    for pid in regions:
        nested[pid] = set().union(*(inside[other] for other in regions if other in inside[pid]))
    memo: dict[str, frozenset[str]] = {}

    def done(node: str) -> frozenset[str]:
        if node in memo:
            return memo[node]
        if node == start:
            result: frozenset[str] = frozenset()
        elif node in join_of:
            p = join_of[node]
            acc = set(done(p.id)) | {p.id}
            for _, region in regions[p.id]:
                ends = [
                    n for n in region
                    if n not in nested[p.id] and (not step_targets(by_id[n]) or node in step_targets(by_id[n]))
                ]
                acc |= frozenset.intersection(*(done(e) | {e} for e in ends)) if ends else frozenset()
            result = frozenset(acc)
        else:
            # A fork edge (parallel -> branch root) passes on done(parallel) without the parallel.
            result = frozenset.intersection(*(
                done(q) if isinstance(by_id[q], ParallelStep) and node in by_id[q].branches else done(q) | {q}
                for q in preds[node]
            ))
        memo[node] = result
        return result

    return done


class _Graph:
    """One validated graph (the top level or a loop body) and its every-path sets."""

    def __init__(self, steps: tuple[Step, ...], start: str, prefix: str, elsewhere: frozenset[str]) -> None:
        self.steps = steps
        self.by_id = _check_structure(steps, start, prefix, elsewhere)
        _check_acyclic(self.by_id, prefix)
        reachable = set(_reachable(self.by_id, [start]))
        for step in steps:
            if step.id not in reachable:
                where = "body start step" if prefix else "start step"
                raise ValueError(f"{prefix}step '{step.id}' is not reachable from {where} '{start}'")
        self.preds: dict[str, list[str]] = {sid: [] for sid in self.by_id}
        for step in steps:
            for target in dict.fromkeys(step_targets(step)):
                self.preds[target].append(step.id)
        self.regions = _check_parallel_scopes(self.by_id, self.preds)
        self.done = _done_sets(self.by_id, start, self.preds, self.regions)

    def exit_done(self) -> frozenset[str]:
        """Steps finished on EVERY path through the graph when it ends (the virtual exit node)."""
        ends = [sid for sid, step in self.by_id.items() if not step_targets(step)]
        return frozenset.intersection(*(self.done(e) | {e} for e in ends))


def _check_loops_in_parallel(graph: _Graph) -> None:
    for pid, branch_regions in graph.regions.items():
        for branch, region in branch_regions:
            for sid in sorted(region):
                step = graph.by_id[sid]
                if isinstance(step, LOOP_STEP_TYPES):
                    raise ValueError(
                        f"parallel step '{pid}': step '{sid}' in branch '{branch}' is a {step.type} step; loops are "
                        "not allowed inside a parallel branch (loop_in_parallel_not_allowed)"
                    )


class _RefScope:
    """What one expression slot may read: ``readable`` steps, the ``loops`` it may name (id -> kind),
    the loops enclosing it (``chain``), and the loop whose ``while`` (or ``carry``/``until``) it is."""

    def __init__(
        self,
        readable: frozenset[str],
        loops: dict[str, str],
        chain: tuple[str, ...],
        while_of: str | None = None,
        exit_of: str | None = None,
    ) -> None:
        self.readable = readable
        self.loops = loops
        self.chain = chain
        self.while_of = while_of
        self.exit_of = exit_of


def _check_ref(
    where: str, who: str, path: str, scope: _RefScope, info: dict[str, tuple[tuple[str, ...], Step]]
) -> None:
    root, name, loop_root = parse_ref(path)
    if root == "input":
        return
    if root == "loop":
        assert name is not None
        if name not in scope.loops:
            if name == who and isinstance(info[name][1], LOOP_STEP_TYPES):
                raise ValueError(
                    f"{where}: $ref '{path}' reads loop '{name}' itself, but {where.rsplit(': ', 1)[-1]} is evaluated "
                    f"before the loop starts and cannot read loop.{name}.*"
                )
            if name in info and isinstance(info[name][1], LOOP_STEP_TYPES):
                raise ValueError(f"{where}: $ref '{path}' reads loop '{name}', which does not enclose '{who}'")
            raise ValueError(f"{where}: $ref '{path}' refers to unknown loop '{name}'")
        kind = scope.loops[name]
        if loop_root == "item" and kind != "foreach":
            raise ValueError(
                f"{where}: $ref '{path}': '.item' exists only in a foreach loop, and '{name}' is a loop step "
                f"(read loop.{name}.index or loop.{name}.state)"
            )
        if loop_root == "state" and kind != "loop":
            raise ValueError(
                f"{where}: $ref '{path}': '.state' exists only in a loop step, and '{name}' is a foreach step "
                f"(read loop.{name}.index or loop.{name}.item)"
            )
        return
    target = name
    assert target is not None
    if target not in info:
        raise ValueError(f"{where}: $ref '{path}' refers to unknown step '{target}'")
    if isinstance(info[target][1], ParallelStep):
        raise ValueError(
            f"{where}: $ref '{path}' refers to parallel step '{target}', which has no output "
            "(refer to the steps inside its branches instead)"
        )
    if target in scope.readable:
        return
    target_chain = info[target][0]
    if scope.while_of is not None and scope.while_of in target_chain:
        raise ValueError(
            f"{where}: $ref '{path}' reads body step '{target}', but 'while' is evaluated before each iteration "
            f"and cannot read body steps (carry what it needs into loop.{scope.while_of}.state instead)"
        )
    if target in scope.chain:
        raise ValueError(
            f"{where}: $ref '{path}' refers to loop step '{target}' from inside its own body; its output exists "
            f"only after the loop ends (read loop.{target}.* instead)"
        )
    outside = [loop for loop in target_chain if loop not in scope.chain]
    if outside:
        raise ValueError(
            f"{where}: $ref '{path}' refers to step '{target}' inside the body of loop '{outside[0]}', which is "
            f"not readable outside that body (read steps.{outside[0]}.output instead)"
        )
    if scope.exit_of is not None and target_chain and target_chain[-1] == scope.exit_of:
        raise ValueError(
            f"{where}: $ref '{path}' refers to body step '{target}', which does not run on every path through "
            f"the body of loop '{scope.exit_of}' (loop_ref_not_on_every_path)"
        )
    raise ValueError(
        f"{where}: $ref '{path}' refers to step '{target}', which does not run before '{who}' on every path"
    )


def _check_refs(
    graph: _Graph,
    graphs: dict[str, _Graph],
    info: dict[str, tuple[tuple[str, ...], Step]],
    outer: frozenset[str] = frozenset(),
    loops: dict[str, str] | None = None,
    chain: tuple[str, ...] = (),
) -> None:
    """Apply the module docstring's ``$ref`` rules to one graph, then to each loop body in it."""
    loops = loops or {}
    for step in graph.steps:
        here = outer | graph.done(step.id)
        if not isinstance(step, LOOP_STEP_TYPES):
            for path in iter_refs(step):
                _check_ref(f"step '{step.id}'", step.id, path, _RefScope(here, loops, chain), info)
            continue
        sid = step.id
        inner_loops = {**loops, sid: step.type}
        inner_chain = (*chain, sid)
        if isinstance(step, ForeachStep):
            _check_ref(f"step '{sid}': items", sid, step.items[REF_KEY], _RefScope(here, loops, chain), info)
        else:
            for path in _template_refs(step.state):
                _check_ref(f"step '{sid}': state", sid, path, _RefScope(here, loops, chain), info)
            if step.while_ is not None:
                while_scope = _RefScope(here, {**loops, sid: "loop"}, chain, while_of=sid)
                for path in step.while_.refs():
                    _check_ref(f"step '{sid}': while", sid, path, while_scope, info)
        body = graphs[sid]
        _check_refs(body, graphs, info, here, inner_loops, inner_chain)
        if isinstance(step, LoopStep):
            exit_scope = _RefScope(here | body.exit_done(), inner_loops, inner_chain, exit_of=sid)
            for label, paths in (("carry", _template_refs(step.carry)),
                                 ("until", step.until.refs() if step.until is not None else [])):
                for path in paths:
                    _check_ref(f"step '{sid}': {label}", sid, path, exit_scope, info)


def _check_graphs(
    steps: tuple[Step, ...], start: str
) -> tuple[dict[str, tuple[tuple[str, ...], Step]], _Graph, dict[str, _Graph]]:
    """Whole-spec rules (count, global ids, loop depth, body kinds), then every graph's structure."""
    walked = list(walk_steps(steps))
    if len(walked) > MAX_STEPS:
        raise ValueError(f"a workflow has at most {MAX_STEPS} steps in all (loop bodies included), got {len(walked)}")
    info: dict[str, tuple[tuple[str, ...], Step]] = {}
    for chain, step in walked:
        if step.id in info:
            raise ValueError(f"duplicate step id '{step.id}'")
        info[step.id] = (chain, step)
        if chain and step.type not in BODY_STEP_KINDS:
            raise ValueError(
                f"step '{chain[-1]}': body step '{step.id}' is a {step.type} step, which is not allowed inside a "
                "loop body (loop_body_step_not_allowed)"
            )
        if isinstance(step, LOOP_STEP_TYPES) and len(chain) >= MAX_LOOP_DEPTH:
            raise ValueError(
                f"step '{step.id}': loops nest at most {MAX_LOOP_DEPTH} deep, and '{step.id}' is inside "
                + " > ".join(f"'{loop}'" for loop in chain)
            )
    all_ids = frozenset(info)
    top = _Graph(steps, start, "", all_ids - {s.id for s in steps})
    _check_loops_in_parallel(top)
    graphs: dict[str, _Graph] = {}
    for _, step in walked:
        if isinstance(step, LOOP_STEP_TYPES):
            body_ids = {s.id for s in step.body.steps}
            graphs[step.id] = _Graph(step.body.steps, step.body.start, f"step '{step.id}': body: ", all_ids - body_ids)
    return info, top, graphs


_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def _unstorable(text: str) -> str | None:
    """Why PostgreSQL cannot store ``text`` (JSONB or text column), or None if it can."""
    match = _UNSTORABLE.search(text)
    if match is None:
        return None
    if match.group() == "\x00":
        return "a NUL character (\\u0000)"
    return f"an unpaired surrogate (\\u{ord(match.group()):04x})"


def _check_storable_values(data: Any) -> None:
    """Every value in the raw spec must be storable as JSON: no string (keys included) with U+0000
    (JSONB and text columns reject it) or an unpaired surrogate (invalid JSON for JSONB, not
    encodable as UTF-8), and no inf/-inf/nan (JSON has none: 1e309 parses to inf and would be
    stored as null). The message names the innermost REAL step around the value (``steps.<n>`` at
    the top level or ``<step>.body.steps.<n>``; never a look-alike inside a literal). Iterative;
    callers bound the depth first."""
    # role: "root" (the spec), "steps" (a real steps list), "step" (a real step), "body" (its body).
    stack: list[tuple[Any, str, str | None, str | None]] = [(data, "", None, "root")]
    while stack:
        item, where, step_id, role = stack.pop()
        if role == "step" and isinstance(item, dict) and isinstance(item.get("id"), str):
            step_id = _UNSTORABLE.sub("?", item["id"][:64])
        in_step = f" (step '{step_id}')" if step_id else ""
        if isinstance(item, str):
            problem = _unstorable(item)
            if problem:
                raise ValueError(f"workflow spec contains {problem} at {where or 'the top level'}{in_step}")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError(
                f"workflow spec contains a non-finite number ({item}) at {where or 'the top level'}{in_step}"
            )
        elif isinstance(item, dict):
            for key, value in item.items():
                label = _UNSTORABLE.sub("?", key[:64]) if isinstance(key, str) else str(key)
                path = f"{where}.{label}" if where else label
                if isinstance(key, str) and (problem := _unstorable(key)):
                    raise ValueError(f"workflow spec contains {problem} in the key at {path}{in_step}")
                child = None
                if key == "steps" and role in ("root", "body"):
                    child = "steps"
                elif key == "body" and role == "step":
                    child = "body"
                stack.append((value, path, step_id, child))
        elif isinstance(item, (list, tuple)):
            child = "step" if role == "steps" else None
            stack.extend(
                (value, f"{where}.{i}" if where else str(i), step_id, child) for i, value in enumerate(item)
            )


def _raw_condition_operands(when: Any, where: str) -> list[tuple[str, Any]]:
    """The operands of a raw condition, combinators followed (a bounded number of levels: deeper
    ones are rejected by validation)."""
    found: list[tuple[str, Any]] = []
    todo: list[tuple[Any, str, int]] = [(when, where, 0)]
    while todo:
        cond, at, level = todo.pop(0)
        if not isinstance(cond, dict) or level > MAX_CONDITION_DEPTH + 1:
            continue
        if "op" in cond:
            found += [(f"{at}.{side}: operand", cond.get(side)) for side in ("left", "right")]
        for key in ("all", "any"):
            if isinstance(cond.get(key), (list, tuple)):
                todo += [(sub, f"{at}.{key}.{i}", level + 1) for i, sub in enumerate(cond[key])]
        if "not" in cond:
            todo.append((cond["not"], f"{at}.not", level + 1))
    return found


def _check_raw_step_depths(steps: Any) -> None:
    """Name the step whose template value, condition operand or evaluate ``expected`` is nested too
    deeply (raw input), in loop bodies too (a bounded number of levels: deeper loops are rejected
    by validation)."""
    todo: list[tuple[Any, int]] = [(steps, 0)]
    while todo:
        items, level = todo.pop(0)
        if not isinstance(items, (list, tuple)):
            continue
        for step in items:
            if not isinstance(step, dict):
                continue
            step_id = step.get("id")
            values: list[tuple[str, Any]] = []
            for field in ("input", "state", "carry"):
                if isinstance(step.get(field), dict):
                    values += [(f"{field}: value of '{key}'", value) for key, value in step[field].items()]
            if isinstance(step.get("cases"), (list, tuple)):
                for i, case in enumerate(step["cases"]):
                    when = case.get("when") if isinstance(case, dict) else None
                    values += _raw_condition_operands(when, f"cases.{i}.when")
            for field in ("while", "until"):
                values += _raw_condition_operands(step.get(field), field)
            if "expected" in step:
                values.append(("expected", step["expected"]))
            for where, value in values:
                if nesting_depth(value, MAX_LITERAL_DEPTH) > MAX_LITERAL_DEPTH:
                    raise ValueError(f"step '{step_id}': {where} is nested more than {MAX_LITERAL_DEPTH} levels deep")
            body = step.get("body")
            if isinstance(body, dict) and level <= MAX_LOOP_DEPTH:
                todo.append((body.get("steps"), level + 1))


def _spec_bytes(data: Any) -> int:
    return len(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))


class WorkflowLimits(OmitNoneModel):
    """Caps on one workflow run. ``max_cost_usd`` caps the cost of the whole run tree (the run, its
    child runs and its judge rows), checked before every loop iteration and top-level agent and
    evaluate step; reaching it fails the step with ``run_budget_exceeded``."""

    max_cost_usd: Decimal | None = Field(default=None, description="USD, 0.0001 to 1000, at most 4 decimals")

    @field_validator("max_cost_usd")
    @classmethod
    def _cost(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return v
        if not v.is_finite():
            raise ValueError("max_cost_usd must be a finite number")
        if not MIN_COST_USD <= v <= MAX_COST_USD:
            raise ValueError(f"max_cost_usd must be between {MIN_COST_USD} and {MAX_COST_USD}")
        if v != v.quantize(Decimal(1).scaleb(-COST_DECIMAL_PLACES)):
            raise ValueError(f"max_cost_usd may have at most {COST_DECIMAL_PLACES} decimal places")
        return normalise_usd(v)

    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in type(self).model_fields)


def _no_limits(value: WorkflowLimits | None) -> bool:
    return value is None or value.is_empty()


class WorkflowSpec(BaseModel):
    """A versionable workflow definition (validated graph of typed steps).

    ``limits`` is left out of every dump while it is unset or empty, so a spec without it keeps its
    JSON and ``canonical_workflow_hash``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_version: Literal[1] = 1
    name: str
    description: str = Field(default="", max_length=2000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    steps: tuple[Step, ...]
    start: str
    limits: WorkflowLimits | None = Field(default=None, exclude_if=_no_limits)

    @model_validator(mode="before")
    @classmethod
    def _depth_and_size(cls, data: Any) -> Any:
        # Bound nesting before anything recurses into the raw data (json.dumps below, pydantic).
        if isinstance(data, dict):
            _check_raw_step_depths(data.get("steps"))
            if nesting_depth(data, MAX_SPEC_DEPTH) > MAX_SPEC_DEPTH:
                raise ValueError(f"workflow spec is nested more than {MAX_SPEC_DEPTH} levels deep")
            _check_storable_values(data)
            if _spec_bytes(data) > MAX_SPEC_BYTES:
                raise ValueError(f"workflow spec must be at most {MAX_SPEC_BYTES // 1024} KiB as JSON")
        return data

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value:
            raise ValueError("name cannot be empty")
        if len(value) > 100:
            raise ValueError("name must be at most 100 characters")
        return value

    @field_validator("input_schema")
    @classmethod
    def _input_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError('input_schema must be a JSON Schema object schema ({"type": "object", ...})')
        return value

    @field_validator("steps", mode="before")
    @classmethod
    def _step_count(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            if not value:
                raise ValueError("a workflow needs at least one step")
            if len(value) > MAX_STEPS:
                raise ValueError(f"a workflow has at most {MAX_STEPS} steps, got {len(value)}")
        return value

    @model_validator(mode="after")
    def _graph(self) -> WorkflowSpec:
        for _, step in walk_steps(self.steps):
            if step.id.endswith(PAUSE_STEP_SUFFIX):
                raise ValueError(
                    f"step_id_reserved_suffix: step id '{step.id}' must not end in '{PAUSE_STEP_SUFFIX}'"
                )
        info, top, graphs = _check_graphs(self.steps, self.start)
        _check_refs(top, graphs, info)
        if _spec_bytes(self.model_dump(mode="json")) > MAX_SPEC_BYTES:
            raise ValueError(f"workflow spec must be at most {MAX_SPEC_BYTES // 1024} KiB as JSON")
        return self


def canonical_workflow_hash(spec: WorkflowSpec) -> str:
    """SHA-256 of the spec's JSON with sorted keys: equal specs hash equal, any change changes it."""
    payload = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class WorkflowScheduleSpec(BaseModel):
    """Body of POST /workflows/{id}/schedules: a named cron (5 fields) that runs the latest
    published version with a fixed input. The routes validate cron, timezone and input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    cron: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    input: Any = Field(default_factory=dict)

    @field_validator("name", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class WorkflowScheduleUpdate(BaseModel):
    """Body of PATCH /workflows/schedules/{id}: any subset of the fields (and `active`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    cron: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    input: Any = None
    active: bool | None = None

    @model_validator(mode="after")
    def _not_null(self) -> "WorkflowScheduleUpdate":
        for key in self.model_fields_set:
            if getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null")
        return self
