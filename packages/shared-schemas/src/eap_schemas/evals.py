"""Test suites (evals) contracts (agent capabilities milestone, AC.1).

Declared here so the API validates suites, cases and evaluators without importing the worker or
`pydantic_evals`. Evaluators are a union discriminated by `kind`:

- `equals_expected`: deep JSON equality with the case's expected output (numbers by value);
- `contains {value?, case_sensitive}`: the output contains `value` (default: the expected output);
- `regex {pattern}`: RE2 pattern (at most 200 characters). This contract checks only the length
  (eap_schemas imports no eap_processors); the API refuses a pattern RE2 does not compile (422 at
  `<path>.pattern`, fixed message) on every suite and case write and on import, and the worker
  compiles with the same `eap_processors.compile_re2`;
- `schema_valid {json_schema?}`: the output validates against the schema (default: the agent
  version's output schema);
- `field_equals {path, value}`: the structured output's value at `path` (dotted segments, a digit
  segment indexes a list, as in workflow `$ref`s) equals `value`;
- `max_duration {seconds}`, `max_cost {usd}`;
- `llm_judge {rubric, model, include_input, include_expected, pass_threshold?}`: the judge's
  `pass` is the assertion, its `score` (0..1) the score.

Every evaluator has an optional `name` (unique within a suite's evaluators plus one case's).

`EvalSuiteSnapshot` is what an eval run executes (`runs.input`): the suite's cases and evaluators
frozen at run creation. `canonical_json()` is the stored text; `suite_hash()` is the sha256 of the
canonical JSON of the cases and evaluators only, so two runs of the same tests compare as
`same_suite` regardless of the suite's name or the run's concurrency.

No I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal
from typing import Annotated, Any, Literal, Tuple, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, field_validator, model_validator

from eap_schemas.json_schema_subset import SchemaSubsetInvalid, json_size, validate_output_schema
from eap_schemas.limits import COST_DECIMAL_PLACES, normalise_usd
from eap_schemas.model_ref import ModelRef

MAX_SUITE_EVALUATORS = 10
MAX_CASE_EVALUATORS = 5
MAX_SUITE_CASES = 500
MAX_CASE_INPUT_BYTES = 32 * 1024
MAX_EXPECTED_OUTPUT_BYTES = 32 * 1024
MAX_CASE_METADATA_KEYS = 16
MAX_METADATA_KEY_CHARS = 64
MAX_METADATA_VALUE_CHARS = 1000
MAX_REGEX_PATTERN_CHARS = 200
MAX_RUBRIC_BYTES = 4 * 1024
MAX_CONTAINS_VALUE_BYTES = 4 * 1024
MAX_FIELD_PATH_SEGMENTS = 16
MAX_DURATION_SECONDS = 3600.0
MAX_EVAL_COST_USD = Decimal("1000")
DEFAULT_MAX_CONCURRENCY = 4
MAX_MAX_CONCURRENCY = 8
MAX_NAME_CHARS = 100
MAX_JUDGE_REASON_CHARS = 2000

_FIELD_PATH = re.compile(rf"^[A-Za-z0-9_-]{{1,64}}(?:\.[A-Za-z0-9_-]{{1,64}}){{0,{MAX_FIELD_PATH_SEGMENTS - 1}}}$")

EvaluatorKind = Literal[
    "equals_expected", "contains", "regex", "schema_valid", "field_equals", "max_duration", "max_cost", "llm_judge"
]


class EvalSuiteInvalid(ValueError):
    """A suite, case or evaluator breaks the rules (`eval_suite_invalid`). Pickle-safe."""

    code = "eval_suite_invalid"

    def __init__(self, reason: str, path: str = "") -> None:
        self.reason = reason
        self.path = path
        super().__init__(reason, path)

    def __str__(self) -> str:
        where = f" at {self.path}" if self.path else ""
        return f"the test suite is invalid{where}: {self.reason}"


def _check_json_bytes(value: Any, limit: int, what: str) -> Any:
    """Finite JSON (no NaN or Infinity at any depth) of at most `limit` bytes."""
    try:
        json.dumps(value, allow_nan=False)
    except ValueError:
        raise ValueError(f"{what} must not contain NaN or Infinity") from None
    if json_size(value) > limit:
        raise ValueError(f"{what} must be at most {limit} bytes as JSON")
    return value


def _check_field_path(v: str, name: str = "field") -> str:
    if not _FIELD_PATH.fullmatch(v):
        raise ValueError(
            f"{name} must be 1 to {MAX_FIELD_PATH_SEGMENTS} dot-separated segments of [A-Za-z0-9_-] (1 to 64 each)"
        )
    return v


class _Evaluator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_CHARS)


class EqualsExpected(_Evaluator):
    kind: Literal["equals_expected"] = "equals_expected"


class Contains(_Evaluator):
    kind: Literal["contains"] = "contains"
    value: JsonValue = Field(default=None, description="Default (None): the case's expected output")
    case_sensitive: bool = True

    @field_validator("value")
    @classmethod
    def _size(cls, v: Any) -> Any:
        return _check_json_bytes(v, MAX_CONTAINS_VALUE_BYTES, "value")


class Regex(_Evaluator):
    kind: Literal["regex"] = "regex"
    pattern: str = Field(min_length=1, max_length=MAX_REGEX_PATTERN_CHARS, description="RE2 syntax")
    negate: bool = Field(default=False, description="Passes when the pattern does not match")
    field: str | None = Field(
        default=None,
        description="Dotted path into the structured output (as field_equals.path); the pattern runs over that "
        "field's string value instead of the whole output text",
    )

    @field_validator("field")
    @classmethod
    def _field(cls, v: str | None) -> str | None:
        return None if v is None else _check_field_path(v)


class SchemaValid(_Evaluator):
    kind: Literal["schema_valid"] = "schema_valid"
    json_schema: dict[str, Any] | None = Field(default=None, description="Default: the agent's output schema")

    @field_validator("json_schema")
    @classmethod
    def _schema(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        try:
            return validate_output_schema(v)
        except SchemaSubsetInvalid as exc:
            raise ValueError(str(exc)) from None


class FieldEquals(_Evaluator):
    kind: Literal["field_equals"] = "field_equals"
    path: str = Field(description="Dotted path into the structured output; a digit segment indexes a list")
    value: JsonValue

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        return _check_field_path(v, "path")

    @field_validator("value")
    @classmethod
    def _size(cls, v: Any) -> Any:
        return _check_json_bytes(v, MAX_CONTAINS_VALUE_BYTES, "value")


class MaxDuration(_Evaluator):
    kind: Literal["max_duration"] = "max_duration"
    seconds: float = Field(gt=0, le=MAX_DURATION_SECONDS)


class MaxCost(_Evaluator):
    kind: Literal["max_cost"] = "max_cost"
    usd: Decimal = Field(gt=0, le=MAX_EVAL_COST_USD)

    @field_validator("usd", mode="before")
    @classmethod
    def _finite(cls, v: Any) -> Any:
        if isinstance(v, (Decimal, float)) and not Decimal(str(v)).is_finite():
            raise ValueError("usd must be a finite number")
        return v

    @field_validator("usd")
    @classmethod
    def _normalised(cls, v: Decimal) -> Decimal:
        if not v.is_finite():
            raise ValueError("usd must be a finite number")
        if v != v.quantize(Decimal(1).scaleb(-COST_DECIMAL_PLACES)):
            raise ValueError(f"usd may have at most {COST_DECIMAL_PLACES} decimal places")
        return normalise_usd(v)


class LlmJudge(_Evaluator):
    kind: Literal["llm_judge"] = "llm_judge"
    rubric: str = Field(min_length=1)
    model: ModelRef = Field(description="Judge model (must be allowlisted)")
    include_input: bool = False
    include_expected: bool = False
    pass_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("rubric")
    @classmethod
    def _rubric(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("rubric cannot be blank")
        if len(v.encode("utf-8")) > MAX_RUBRIC_BYTES:
            raise ValueError(f"rubric must be at most {MAX_RUBRIC_BYTES} bytes in UTF-8")
        return v


EvaluatorSpec = Annotated[
    Union[EqualsExpected, Contains, Regex, SchemaValid, FieldEquals, MaxDuration, MaxCost, LlmJudge],
    Field(discriminator="kind"),
]


def _unique_names(evaluators: tuple[Any, ...], where: str) -> None:
    seen: set[str] = set()
    for evaluator in evaluators:
        if evaluator.name is None:
            continue
        if evaluator.name in seen:
            raise ValueError(f"duplicate evaluator name {evaluator.name!r} in {where}")
        seen.add(evaluator.name)


MetadataValue = Union[str, int, float, bool, None]


class EvalCase(BaseModel):
    """One test case: an input, an optional expected output, flat metadata and extra evaluators."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    input: str = Field(min_length=1)
    expected_output: JsonValue = None
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)
    evaluators: Tuple[EvaluatorSpec, ...] = Field(default=(), max_length=MAX_CASE_EVALUATORS)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name cannot be blank")
        return v

    @field_validator("input")
    @classmethod
    def _input(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_CASE_INPUT_BYTES:
            raise ValueError(f"input must be at most {MAX_CASE_INPUT_BYTES} bytes in UTF-8")
        return v

    @field_validator("expected_output")
    @classmethod
    def _expected(cls, v: Any) -> Any:
        return _check_json_bytes(v, MAX_EXPECTED_OUTPUT_BYTES, "expected_output")

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > MAX_CASE_METADATA_KEYS:
            raise ValueError(f"metadata may have at most {MAX_CASE_METADATA_KEYS} keys")
        for key, value in v.items():
            if not key or len(key) > MAX_METADATA_KEY_CHARS:
                raise ValueError(f"metadata keys must be 1 to {MAX_METADATA_KEY_CHARS} characters")
            if isinstance(value, str) and len(value) > MAX_METADATA_VALUE_CHARS:
                raise ValueError(f"metadata values must be at most {MAX_METADATA_VALUE_CHARS} characters")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("metadata numbers must be finite")
        return v

    @model_validator(mode="after")
    def _names(self) -> "EvalCase":
        _unique_names(self.evaluators, "the case's evaluators")
        return self


class EvalCaseSnapshot(EvalCase):
    """A case as frozen into an eval run: with its id (child run ids derive from it) and position."""

    id: UUID
    position: int = Field(ge=0)


class EvalSuiteSnapshot(BaseModel):
    """What one eval run executes (`runs.input`): the suite's cases and evaluators at creation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: UUID
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    evaluators: Tuple[EvaluatorSpec, ...] = Field(default=(), max_length=MAX_SUITE_EVALUATORS)
    cases: Tuple[EvalCaseSnapshot, ...] = Field(min_length=1, max_length=MAX_SUITE_CASES)
    max_concurrency: int = Field(default=DEFAULT_MAX_CONCURRENCY, ge=1, le=MAX_MAX_CONCURRENCY)

    @model_validator(mode="after")
    def _consistent(self) -> "EvalSuiteSnapshot":
        _unique_names(self.evaluators, "the suite's evaluators")
        ids: set[UUID] = set()
        names: set[str] = set()
        for case in self.cases:
            if case.id in ids:
                raise ValueError(f"duplicate case id: {case.id}")
            if case.name in names:
                raise ValueError(f"duplicate case name: {case.name!r}")
            ids.add(case.id)
            names.add(case.name)
            _unique_names((*self.evaluators, *case.evaluators), f"case {case.name!r}")
        return self

    def ordered_cases(self) -> tuple[EvalCaseSnapshot, ...]:
        """Cases in execution order: by position, then id (deterministic on replay)."""
        return tuple(sorted(self.cases, key=lambda c: (c.position, str(c.id))))

    def canonical_json(self) -> str:
        """The stored snapshot text (`runs.input`): sorted keys, compact."""
        return _canonical(self.model_dump(mode="json"))

    def suite_hash(self) -> str:
        """sha256 of the canonical JSON of the cases (in `ordered_cases()` order, so listing order
        never matters) and the evaluators only, without None model fields."""
        # None model fields are left out (so a later optional field keeps every hash); None inside
        # JSON values (expected_output, value) is content and stays.
        data = {
            "cases": [case.model_dump(mode="json", exclude_none=True) for case in self.ordered_cases()],
            "evaluators": [e.model_dump(mode="json", exclude_none=True) for e in self.evaluators],
        }
        return hashlib.sha256(_canonical(data).encode()).hexdigest()


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def field_path_value(value: Any, path: str) -> tuple[bool, Any]:
    """(found, value) at the dotted `path` inside `value`, following `field_equals` rules. Never
    raises: anything that does not resolve is (False, None)."""
    for segment in path.split("."):
        if isinstance(value, dict):
            if segment not in value:
                return False, None
            value = value[segment]
        elif isinstance(value, list) and segment.isascii() and segment.isdecimal() and int(segment) < len(value):
            value = value[int(segment)]
        else:
            return False, None
    return True, value


class JudgeVerdict(BaseModel):
    """The `eap-judge` agent's output: `pass` is the assertion, `score` (0..1) the score."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True)

    reason: str = Field(max_length=MAX_JUDGE_REASON_CHARS, description="Why the output passes or fails")
    # Strict: only a JSON boolean is a verdict ("yes", "true", 1 are refused, never coerced).
    passed: StrictBool = Field(alias="pass", description="Whether the output meets the rubric")
    score: float = Field(ge=0.0, le=1.0, description="How well the output meets the rubric, 0 to 1")


__all__ = [
    "Contains",
    "DEFAULT_MAX_CONCURRENCY",
    "EqualsExpected",
    "EvalCase",
    "EvalCaseSnapshot",
    "EvalSuiteInvalid",
    "EvalSuiteSnapshot",
    "EvaluatorKind",
    "EvaluatorSpec",
    "FieldEquals",
    "JudgeVerdict",
    "LlmJudge",
    "MAX_CASE_EVALUATORS",
    "MAX_CASE_INPUT_BYTES",
    "MAX_CASE_METADATA_KEYS",
    "MAX_MAX_CONCURRENCY",
    "MAX_REGEX_PATTERN_CHARS",
    "MAX_RUBRIC_BYTES",
    "MAX_SUITE_CASES",
    "MAX_SUITE_EVALUATORS",
    "MaxCost",
    "MaxDuration",
    "Regex",
    "SchemaValid",
    "field_path_value",
]
