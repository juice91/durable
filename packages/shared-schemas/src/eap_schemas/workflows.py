"""Declarative workflow definitions (milestone 3, task 3.7).

A workflow is DATA: a JSON graph of typed steps (agent, tool, branch, parallel, approval). Inputs
are template mappings whose values are JSON literals or ``{"$ref": "<path>"}``; branch conditions
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
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = (
    "MAX_LITERAL_DEPTH",
    "MAX_SPEC_BYTES",
    "MAX_SPEC_DEPTH",
    "MAX_STEPS",
    "AgentStep",
    "ApprovalStep",
    "BranchCase",
    "BranchStep",
    "Condition",
    "ParallelStep",
    "Step",
    "ToolStep",
    "WorkflowSpec",
    "canonical_workflow_hash",
    "iter_refs",
    "nesting_depth",
    "step_targets",
)

MAX_STEPS = 50
MAX_SPEC_BYTES = 64 * 1024
MAX_LITERAL_DEPTH = 32  # nesting levels of one template value or condition literal
MAX_SPEC_DEPTH = 64  # nesting levels of the whole spec document
STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SEGMENT = r"[A-Za-z0-9_-]{1,64}"
REF_PATTERN = re.compile(
    rf"^(?:input|steps\.(?P<step>[a-z][a-z0-9_]{{0,63}})\.output)(?:\.{_SEGMENT}){{0,16}}$"
)
REF_KEY = "$ref"
ConditionOp = Literal["eq", "ne", "gt", "lt", "contains", "exists"]


def _ref_path(value: Any) -> str | None:
    """The path of a ``{"$ref": path}`` value, None for a literal; raises on a malformed ref."""
    if not isinstance(value, dict) or REF_KEY not in value:
        return None
    if set(value) != {REF_KEY} or not isinstance(value[REF_KEY], str):
        raise ValueError('a $ref must be exactly {"$ref": "<path>"} with a string path')
    path = value[REF_KEY]
    if not REF_PATTERN.fullmatch(path):
        raise ValueError(
            f"invalid $ref '{path}': must be 'input[.field...]' or 'steps.<step_id>.output[.field...]'"
        )
    return path


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
        if _ref_path(value) is None:
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


def _check_step_id(value: str, what: str = "step id") -> str:
    if not isinstance(value, str) or not STEP_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{what} '{value}' must match ^[a-z][a-z0-9_]{{0,63}}$")
    return value


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _check_step_id(value)

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
                where = ".".join(str(part) for part in err["loc"])
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


class BranchCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    when: Condition
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


Step = Annotated[
    Union[AgentStep, ToolStep, BranchStep, ParallelStep, ApprovalStep],
    Field(discriminator="type"),
]


def step_targets(step: Step) -> list[str]:
    """Every step id this step can hand control to (a parallel's join included)."""
    if isinstance(step, BranchStep):
        return [case.next for case in step.cases] + [step.default]
    if isinstance(step, ParallelStep):
        return [*step.branches, step.join]
    return [step.next] if step.next is not None else []


def iter_refs(step: Step) -> list[str]:
    """Every ``$ref`` path the step reads (input templates and branch conditions)."""
    if isinstance(step, (AgentStep, ToolStep)):
        return [p for p in (_ref_path(v) for v in step.input.values()) if p is not None]
    if isinstance(step, BranchStep):
        return [p for case in step.cases for p in case.when.refs()]
    return []


def _labelled_targets(step: Step) -> list[tuple[str, str]]:
    if isinstance(step, BranchStep):
        cases = [(f"case {i} target", case.next) for i, case in enumerate(step.cases, 1)]
        return cases + [("default target", step.default)]
    if isinstance(step, ParallelStep):
        return [("branch target", b) for b in step.branches] + [("join target", step.join)]
    return [("next target", step.next)] if step.next is not None else []


def _check_structure(steps: tuple[Step, ...], start: str) -> dict[str, Step]:
    by_id: dict[str, Step] = {}
    for step in steps:
        if step.id in by_id:
            raise ValueError(f"duplicate step id '{step.id}'")
        by_id[step.id] = step
    if start not in by_id:
        raise ValueError(f"start step '{start}' does not exist")
    joins: dict[str, str] = {}
    for step in steps:
        for label, target in _labelled_targets(step):
            if target not in by_id:
                raise ValueError(f"step '{step.id}': {label} '{target}' does not exist")
        if isinstance(step, ParallelStep):
            if step.join in step.branches:
                raise ValueError(f"step '{step.id}': join '{step.join}' cannot also be one of its branches")
            if step.join in joins:
                raise ValueError(f"step '{step.join}' is the join of both '{joins[step.join]}' and '{step.id}'")
            joins[step.join] = step.id
    return by_id


def _check_acyclic(by_id: dict[str, Step]) -> None:
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    path: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        path.append(node)
        for target in step_targets(by_id[node]):
            if state.get(target) == 1:
                cycle = path[path.index(target):] + [target]
                raise ValueError(f"workflow graph has a cycle: {' -> '.join(cycle)}")
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


def _check_refs(
    by_id: dict[str, Step], start: str, preds: dict[str, list[str]], regions: dict[str, list[tuple[str, set[str]]]]
) -> None:
    """A ``steps.<id>`` ref must name a step that has run before the current one on EVERY path.

    ``done(n)`` = steps guaranteed finished when ``n`` starts. A join starts after all its branches,
    so it adds, per branch, what is guaranteed at every end of that branch (steps whose path ends
    there or goes to the join; ends inside a nested parallel continue at that inner join)."""
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

    for step in by_id.values():
        for path in iter_refs(step):
            target = REF_PATTERN.fullmatch(path).group("step")  # type: ignore[union-attr]
            if target is None:
                continue
            if target not in by_id:
                raise ValueError(f"step '{step.id}': $ref '{path}' refers to unknown step '{target}'")
            if isinstance(by_id[target], ParallelStep):
                raise ValueError(
                    f"step '{step.id}': $ref '{path}' refers to parallel step '{target}', which has no output "
                    "(refer to the steps inside its branches instead)"
                )
            if target not in done(step.id):
                raise ValueError(
                    f"step '{step.id}': $ref '{path}' refers to step '{target}', which does not run before "
                    f"'{step.id}' on every path"
                )


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
    stored as null). Iterative; callers bound the depth first."""
    stack: list[tuple[Any, str]] = [(data, "")]
    while stack:
        item, where = stack.pop()
        if isinstance(item, str):
            problem = _unstorable(item)
            if problem:
                raise ValueError(f"workflow spec contains {problem} at {where or 'the top level'}")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"workflow spec contains a non-finite number ({item}) at {where or 'the top level'}")
        elif isinstance(item, dict):
            for key, value in item.items():
                label = _UNSTORABLE.sub("?", key[:64]) if isinstance(key, str) else str(key)
                path = f"{where}.{label}" if where else label
                if isinstance(key, str) and (problem := _unstorable(key)):
                    raise ValueError(f"workflow spec contains {problem} in the key at {path}")
                stack.append((value, path))
        elif isinstance(item, (list, tuple)):
            stack.extend((value, f"{where}.{i}" if where else str(i)) for i, value in enumerate(item))


def _check_raw_step_depths(steps: Any) -> None:
    """Name the step whose template value or condition operand is nested too deeply (raw input)."""
    if not isinstance(steps, (list, tuple)):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        values: list[tuple[str, Any]] = []
        if isinstance(step.get("input"), dict):
            values += [(f"input: value of '{key}'", value) for key, value in step["input"].items()]
        if isinstance(step.get("cases"), (list, tuple)):
            for i, case in enumerate(step["cases"]):
                when = case.get("when") if isinstance(case, dict) else None
                if isinstance(when, dict):
                    values += [(f"cases.{i}.when.{side}: operand", when.get(side)) for side in ("left", "right")]
        for where, value in values:
            if nesting_depth(value, MAX_LITERAL_DEPTH) > MAX_LITERAL_DEPTH:
                raise ValueError(f"step '{step_id}': {where} is nested more than {MAX_LITERAL_DEPTH} levels deep")


def _spec_bytes(data: Any) -> int:
    return len(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))


class WorkflowSpec(BaseModel):
    """A versionable workflow definition (validated graph of typed steps)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_version: Literal[1] = 1
    name: str
    description: str = Field(default="", max_length=2000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    steps: tuple[Step, ...]
    start: str

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
        by_id = _check_structure(self.steps, self.start)
        _check_acyclic(by_id)
        reachable = set(_reachable(by_id, [self.start]))
        for step in self.steps:
            if step.id not in reachable:
                raise ValueError(f"step '{step.id}' is not reachable from start step '{self.start}'")
        preds: dict[str, list[str]] = {sid: [] for sid in by_id}
        for step in self.steps:
            for target in dict.fromkeys(step_targets(step)):
                preds[target].append(step.id)
        regions = _check_parallel_scopes(by_id, preds)
        _check_refs(by_id, self.start, preds, regions)
        if _spec_bytes(self.model_dump(mode="json")) > MAX_SPEC_BYTES:
            raise ValueError(f"workflow spec must be at most {MAX_SPEC_BYTES // 1024} KiB as JSON")
        return self


def canonical_workflow_hash(spec: WorkflowSpec) -> str:
    """SHA-256 of the spec's JSON with sorted keys: equal specs hash equal, any change changes it."""
    payload = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
