"""Test suites (evals) store (0015_evals): suites, cases, per-run case results, aggregates, compare.

- **Suites and cases** are an agent's editable working data. Every write is validated with the
  `eap_schemas.evals` contracts (evaluator union, case rules) and the caps: at most
  `MAX_SUITE_CASES` (500) cases per suite, enforced under the suite row lock; evaluator names are
  unique across the suite's evaluators plus each case's.
- **`snapshot`** freezes a suite into the `EvalSuiteSnapshot` an eval run executes (`runs.input`,
  created with `RunStore.create_eval_run`), so later edits never change a running or past run.
- **`record_case_result`** writes one case's result exactly once per `(run_id, case_id)`: the
  `eval_case_results` row, the case's judge usage rows (`request_key = judge:{case_id}:{i}`) and the
  `eval_case_result` event (key `case:{case_id}`; status, counts and cost, no text) in ONE
  transaction under the eval run's row lock. A repeat (a replayed step) writes nothing and returns
  the first result.
- **`aggregates`** and **`compare`** read the recorded results.

Stored result shapes (what the worker writes, what the API returns):
- `assertions`: `{name: {"value": bool, "reason": str | None}}`;
- `scores`: `{name: {"value": number, "reason": str | None}}`;
- `labels`: `{name: {"value": str, "reason": str | None}}`;
- `evaluator_failures`: `[{"name": str, "error": str, ...}]` (one per evaluator that raised).

Every query is tenant-scoped; another tenant's rows read as not found. SQLAlchemy only (no
pydantic_ai, dbos or pydantic_evals).
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Mapping, Sequence
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    literal,
    null,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_schemas.evals import (
    DEFAULT_MAX_CONCURRENCY,
    MAX_NAME_CHARS,
    MAX_SUITE_CASES,
    MAX_SUITE_EVALUATORS,
    EvalCase,
    EvalCaseSnapshot,
    EvalSuiteInvalid,
    EvalSuiteSnapshot,
    EvaluatorSpec,
)
from eap_run_store.cursor import RunCursor
from eap_run_store.store import (
    Base,
    RunModel,
    RunModelRequestModel,
    RunNotFound,
    _run_dict,
    append_event_in_session,
    insert_usage_in_session,
)

CaseStatus = Literal["passed", "failed", "error"]
CASE_STATUSES = ("passed", "failed", "error")
MAX_SUITE_DESCRIPTION_CHARS = 2000
MAX_LIST_RUNS = 100
MAX_LIST_RESULTS = 500
EVAL_CASE_RESULT_EVENT = "eval_case_result"

# Same texts as migration 0015_evals.
CASE_INPUT_CHECK = "octet_length(input) <= 32768"
CASE_POSITION_CHECK = "position >= 0"
CASE_METADATA_CHECK = "jsonb_typeof(metadata) = 'object'"
SUITE_EVALUATORS_CHECK = "jsonb_typeof(evaluators) = 'array'"
CASE_EVALUATORS_CHECK = "jsonb_typeof(evaluators) = 'array'"
RESULT_STATUS_CHECK = "status IN ('passed', 'failed', 'error')"
RESULT_ERROR_CHECK = "(status = 'error') = (error_code IS NOT NULL)"
RESULT_OBJECTS_CHECK = (
    "jsonb_typeof(assertions) = 'object' AND jsonb_typeof(scores) = 'object' AND jsonb_typeof(labels) = 'object'"
    " AND jsonb_typeof(evaluator_failures) = 'array'"
)

_EVALUATORS = TypeAdapter(tuple[EvaluatorSpec, ...])


def case_event_key(case_id: UUID | str) -> str:
    """The `eval_case_result` event's key: `case:{case_id}`."""
    return f"case:{case_id}"


def judge_request_key(case_id: UUID | str, evaluator_index: int) -> str:
    """A judge call's usage row key on the eval run: `judge:{case_id}:{evaluator_index}`."""
    return f"judge:{case_id}:{evaluator_index}"


# ---------------------------------------------------------------------------- models


class EvalSuiteModel(Base):
    __tablename__ = "eval_suites"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    # Exactly one of agent_id and workflow_id (ck_eval_suites_one_target, 0022_parity_round_2).
    agent_id: Mapped[str | None] = mapped_column("agent_id", String, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column("workflow_id", String, nullable=True)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    description: Mapped[str | None] = mapped_column("description", Text, nullable=True)
    evaluators: Mapped[list] = mapped_column(
        "evaluators", JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    archived_at: Mapped[datetime | None] = mapped_column("archived_at", DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_eval_suites_tenant_id_id"),
        UniqueConstraint("tenant_id", "agent_id", "name", name="uq_eval_suites_tenant_agent_name"),
        UniqueConstraint("tenant_id", "workflow_id", "name", name="uq_eval_suites_tenant_workflow_name"),
        CheckConstraint("(agent_id IS NULL) <> (workflow_id IS NULL)", name="ck_eval_suites_one_target"),
        CheckConstraint(SUITE_EVALUATORS_CHECK, name="ck_eval_suites_evaluators"),
    )


class EvalCaseModel(Base):
    __tablename__ = "eval_cases"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    suite_id: Mapped[str] = mapped_column("suite_id", PG_UUID(as_uuid=False), nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    input: Mapped[str] = mapped_column("input", Text, nullable=False)
    expected_output: Mapped[Any] = mapped_column("expected_output", JSONB, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    evaluators: Mapped[list] = mapped_column(
        "evaluators", JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    position: Mapped[int] = mapped_column("position", Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("suite_id", "name", name="uq_eval_cases_suite_name"),
        ForeignKeyConstraint(
            ["tenant_id", "suite_id"],
            ["eval_suites.tenant_id", "eval_suites.id"],
            name="fk_eval_cases_tenant_suite",
            ondelete="CASCADE",
        ),
        CheckConstraint(CASE_INPUT_CHECK, name="ck_eval_cases_input_size"),
        CheckConstraint(CASE_POSITION_CHECK, name="ck_eval_cases_position"),
        CheckConstraint(CASE_METADATA_CHECK, name="ck_eval_cases_metadata"),
        CheckConstraint(CASE_EVALUATORS_CHECK, name="ck_eval_cases_evaluators"),
        Index("ix_eval_cases_tenant_suite_position", "tenant_id", "suite_id", "position"),
    )


class EvalCaseResultModel(Base):
    __tablename__ = "eval_case_results"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    run_id: Mapped[str] = mapped_column("run_id", String, nullable=False)
    case_id: Mapped[str] = mapped_column("case_id", PG_UUID(as_uuid=False), nullable=False)
    case_name: Mapped[str] = mapped_column("case_name", String, nullable=False)
    position: Mapped[int] = mapped_column("position", Integer, nullable=False)
    child_run_id: Mapped[str | None] = mapped_column("child_run_id", String, nullable=True)
    status: Mapped[str] = mapped_column("status", String, nullable=False)
    output: Mapped[Any] = mapped_column("output", JSONB, nullable=True)
    assertions: Mapped[dict] = mapped_column("assertions", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    scores: Mapped[dict] = mapped_column("scores", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    labels: Mapped[dict] = mapped_column("labels", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    evaluator_failures: Mapped[list] = mapped_column(
        "evaluator_failures", JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    duration_ms: Mapped[int | None] = mapped_column("duration_ms", BigInteger, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column("cost_usd", Numeric(20, 10), nullable=True)
    error_code: Mapped[str | None] = mapped_column("error_code", String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "run_id", "case_id", name="eval_case_results_pkey"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_eval_case_results_tenant_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(RESULT_STATUS_CHECK, name="ck_eval_case_results_status"),
        CheckConstraint(RESULT_ERROR_CHECK, name="ck_eval_case_results_error"),
        CheckConstraint(RESULT_OBJECTS_CHECK, name="ck_eval_case_results_json"),
    )


# ---------------------------------------------------------------------------- errors (pickle-safe)


class EvalSuiteNotFound(LookupError):
    """No suite with this id belongs to the tenant (and, when given, the agent)."""

    def __init__(self, tenant_id: str, suite_id: UUID | str):
        self.tenant_id = tenant_id
        self.suite_id = str(suite_id)
        super().__init__(tenant_id, str(suite_id))

    def __str__(self) -> str:
        return f"Test suite not found: {self.suite_id}"


class EvalCaseNotFound(LookupError):
    """No case with this id belongs to the tenant (and, when given, the suite)."""

    def __init__(self, tenant_id: str, case_id: UUID | str):
        self.tenant_id = tenant_id
        self.case_id = str(case_id)
        super().__init__(tenant_id, str(case_id))

    def __str__(self) -> str:
        return f"Test case not found: {self.case_id}"


class EvalRunNotFound(LookupError):
    """No eval run with this id belongs to the tenant (and, when given, the agent)."""

    def __init__(self, tenant_id: str, run_id: UUID | str):
        self.tenant_id = tenant_id
        self.run_id = str(run_id)
        super().__init__(tenant_id, str(run_id))

    def __str__(self) -> str:
        return f"Eval run not found: {self.run_id}"


class EvalSuiteNameConflict(ValueError):
    """The target (an agent or a workflow) already has a suite with this name (archived suites
    keep theirs). `agent_id` is the target's id whichever its kind (`kind`)."""

    def __init__(self, agent_id: str, name: str, kind: str = "agent"):
        self.agent_id = agent_id
        self.name = name
        self.kind = kind
        super().__init__(agent_id, name)

    def __str__(self) -> str:
        return f"A test suite named {self.name!r} already exists for this {self.kind}"


class EvalCaseNameConflict(ValueError):
    """The suite already has a case with this name."""

    def __init__(self, suite_id: str, name: str):
        self.suite_id = suite_id
        self.name = name
        super().__init__(suite_id, name)

    def __str__(self) -> str:
        return f"A test case named {self.name!r} already exists in this suite"


class EvalCaseLimitExceeded(ValueError):
    """The write would leave the suite with more than `limit` cases."""

    def __init__(self, suite_id: str, limit: int):
        self.suite_id = suite_id
        self.limit = limit
        super().__init__(suite_id, limit)

    def __str__(self) -> str:
        return f"A test suite can have at most {self.limit} cases"


class EvalSuiteArchived(ValueError):
    """The suite is archived: it cannot be edited or run until it is restored."""

    def __init__(self, suite_id: str):
        self.suite_id = suite_id
        super().__init__(suite_id)

    def __str__(self) -> str:
        return "The test suite is archived; restore it to edit or run it"


# ---------------------------------------------------------------------------- records


@dataclass(frozen=True)
class EvalSuiteRecord:
    id: str
    tenant_id: str
    agent_id: str | None  # exactly one of agent_id and workflow_id is set
    name: str
    description: str | None
    evaluators: tuple[Any, ...]  # eap_schemas.evals evaluator models
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    case_count: int
    workflow_id: str | None = None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    @property
    def target_kind(self) -> str:
        return "workflow" if self.workflow_id is not None else "agent"

    @property
    def target_id(self) -> str:
        return str(self.workflow_id if self.workflow_id is not None else self.agent_id)


@dataclass(frozen=True)
class EvalCaseRecord:
    id: str
    tenant_id: str
    suite_id: str
    name: str
    input: str
    expected_output: Any
    metadata: dict[str, Any]
    evaluators: tuple[Any, ...]
    position: int
    created_at: datetime
    updated_at: datetime

    def to_case(self) -> EvalCase:
        return EvalCase(
            name=self.name,
            input=self.input,
            expected_output=self.expected_output,
            metadata=self.metadata,
            evaluators=self.evaluators,
        )


@dataclass(frozen=True)
class CaseResultWrite:
    """One case's result as the worker records it (see the module docstring for the shapes)."""

    case_id: UUID | str
    case_name: str
    position: int
    status: CaseStatus
    child_run_id: UUID | str | None = None
    output: Any = None
    assertions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    scores: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    labels: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    evaluator_failures: Sequence[Mapping[str, Any]] = ()
    duration_ms: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class EvalCaseResult:
    tenant_id: str
    run_id: str
    case_id: str
    case_name: str
    position: int
    child_run_id: str | None
    status: str
    output: Any
    assertions: dict[str, Any]
    scores: dict[str, Any]
    labels: dict[str, Any]
    evaluator_failures: list[Any]
    duration_ms: int | None
    cost_usd: Decimal | None
    error_code: str | None
    created_at: datetime


@dataclass(frozen=True)
class ImportResult:
    created: int
    updated: int


_UNSET: Any = object()


# ---------------------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(timezone.utc)


COST_QUANTUM = Decimal("1E-10")  # the scale of every cost column (Numeric(20, 10))


def _decimal_str(value: Decimal | None) -> str | None:
    """A cost as the API and events carry it: decimal text at the stored scale (10 places), so the
    event, the stored row and the aggregates always print the same text; or None."""
    return None if value is None else format(Decimal(value).quantize(COST_QUANTUM), "f")


def _evaluators(value: Any, where: str) -> tuple[Any, ...]:
    try:
        return _EVALUATORS.validate_python(tuple(value or ()))
    except ValidationError as exc:
        raise EvalSuiteInvalid(_first_error(exc), where) from None


def _suite_evaluators(value: Any) -> tuple[Any, ...]:
    """A suite's evaluator list: valid specs, at most MAX_SUITE_EVALUATORS, unique names."""
    specs = _evaluators(value, "evaluators")
    if len(specs) > MAX_SUITE_EVALUATORS:
        raise EvalSuiteInvalid(f"a suite can have at most {MAX_SUITE_EVALUATORS} evaluators", "evaluators")
    _check_names(specs, (), "evaluators")
    return specs


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    return f"{loc}: {err['msg']}" if loc else err["msg"]


def _dump_evaluator(evaluator: Any) -> dict[str, Any]:
    """An evaluator as stored: optional fields left out while None (the stored shape every suite
    without nulls already has), but a REQUIRED field is always kept, even when it is null
    (`field_equals.value: null` compares with null). `exclude_unset` is not used: it would drop
    the defaulted `kind` discriminator."""
    out = evaluator.model_dump(mode="json", exclude_none=True)
    full = evaluator.model_dump(mode="json")
    for name, info in type(evaluator).model_fields.items():
        if info.is_required() and name not in out:
            out[name] = full[name]
    return out


def dump_evaluators(evaluators: Sequence[Any]) -> list[dict[str, Any]]:
    """Evaluator models as stored and as the API returns them (see `_dump_evaluator`)."""
    return [_dump_evaluator(e) for e in evaluators]


_dump_evaluators = dump_evaluators


def _validate_case(case: EvalCase | Mapping[str, Any]) -> EvalCase:
    if isinstance(case, EvalCase):
        return case
    try:
        return EvalCase.model_validate(dict(case))
    except ValidationError as exc:
        raise EvalSuiteInvalid(_first_error(exc), "case") from None


def _validate_suite_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS:
        raise EvalSuiteInvalid(f"name must be 1 to {MAX_NAME_CHARS} characters and not blank", "name")
    return name


def _validate_description(description: str | None) -> str | None:
    if description is not None and len(description) > MAX_SUITE_DESCRIPTION_CHARS:
        raise EvalSuiteInvalid(f"description must be at most {MAX_SUITE_DESCRIPTION_CHARS} characters", "description")
    return description


def _check_names(suite_evaluators: Sequence[Any], case_evaluators: Sequence[Any], where: str) -> None:
    """Evaluator names are unique across the suite's evaluators plus one case's."""
    seen: set[str] = set()
    for evaluator in (*suite_evaluators, *case_evaluators):
        if evaluator.name is None:
            continue
        if evaluator.name in seen:
            raise EvalSuiteInvalid(f"duplicate evaluator name {evaluator.name!r}", where)
        seen.add(evaluator.name)


NAME_CONFLICT_CONSTRAINTS = ("uq_eval_suites_tenant_agent_name", "uq_eval_suites_tenant_workflow_name")


def _constraint(exc: IntegrityError) -> str | None:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None)


def _suite_record(row: EvalSuiteModel, case_count: int) -> EvalSuiteRecord:
    return EvalSuiteRecord(
        id=str(row.id),
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        workflow_id=row.workflow_id,
        name=row.name,
        description=row.description,
        evaluators=_evaluators(row.evaluators, "evaluators"),
        created_at=row.created_at,
        updated_at=row.updated_at,
        archived_at=row.archived_at,
        case_count=case_count,
    )


def _case_record(row: EvalCaseModel) -> EvalCaseRecord:
    return EvalCaseRecord(
        id=str(row.id),
        tenant_id=row.tenant_id,
        suite_id=str(row.suite_id),
        name=row.name,
        input=row.input,
        expected_output=row.expected_output,
        metadata=dict(row.metadata_ or {}),
        evaluators=_evaluators(row.evaluators, f"cases.{row.name}.evaluators"),
        position=row.position,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_record(row: EvalCaseResultModel) -> EvalCaseResult:
    return EvalCaseResult(
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        case_id=str(row.case_id),
        case_name=row.case_name,
        position=row.position,
        child_run_id=row.child_run_id,
        status=row.status,
        output=row.output,
        assertions=dict(row.assertions or {}),
        scores=dict(row.scores or {}),
        labels=dict(row.labels or {}),
        evaluator_failures=list(row.evaluator_failures or []),
        duration_ms=row.duration_ms,
        cost_usd=row.cost_usd,
        error_code=row.error_code,
        created_at=row.created_at,
    )


def _check_outcomes(values: Mapping[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    """`{name: {"value": ..., "reason": str | None}}` with the value type of `kind`."""
    if not isinstance(values, Mapping):
        raise ValueError(f"{kind} must be a mapping of name to outcome")
    out: dict[str, dict[str, Any]] = {}
    for name, outcome in values.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{kind} names must be non-empty strings")
        if not isinstance(outcome, Mapping) or "value" not in outcome:
            raise ValueError(f"{kind}[{name!r}] must be an object with a 'value'")
        value = outcome["value"]
        if kind == "assertions" and not isinstance(value, bool):
            raise ValueError(f"assertions[{name!r}].value must be a boolean")
        if kind == "scores" and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        ):
            raise ValueError(f"scores[{name!r}].value must be a finite number")
        if kind == "labels" and not isinstance(value, str):
            raise ValueError(f"labels[{name!r}].value must be a string")
        reason = outcome.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError(f"{kind}[{name!r}].reason must be a string or null")
        out[name] = dict(outcome)
    return out


def _validate_result(result: CaseResultWrite) -> dict[str, Any]:
    """The row values of a valid result (ValueError otherwise)."""
    if result.status not in CASE_STATUSES:
        raise ValueError(f"status must be one of {CASE_STATUSES}")
    if result.error_code is not None and (not isinstance(result.error_code, str) or not result.error_code):
        raise ValueError("error_code must be a non-empty string or None")
    if (result.status == "error") != (result.error_code is not None):
        raise ValueError("error_code is required for status 'error' and only then")
    if not isinstance(result.position, int) or isinstance(result.position, bool) or result.position < 0:
        raise ValueError("position must be a non-negative integer")
    if result.duration_ms is not None and (
        not isinstance(result.duration_ms, int) or isinstance(result.duration_ms, bool) or result.duration_ms < 0
    ):
        raise ValueError("duration_ms must be a non-negative integer or None")
    if not result.case_name:
        raise ValueError("case_name is required")
    failures = [dict(f) for f in result.evaluator_failures]
    if not all(isinstance(f, dict) for f in result.evaluator_failures):
        raise ValueError("evaluator_failures must be a list of objects")
    try:
        json.dumps(result.output, allow_nan=False)
        json.dumps(failures, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("output and evaluator_failures must be finite JSON") from None
    return {
        "case_id": str(UUID(str(result.case_id))),
        "case_name": result.case_name,
        "position": result.position,
        "child_run_id": None if result.child_run_id is None else str(result.child_run_id),
        "status": result.status,
        "output": null() if result.output is None else result.output,
        "assertions": _check_outcomes(result.assertions, "assertions"),
        "scores": _check_outcomes(result.scores, "scores"),
        "labels": _check_outcomes(result.labels, "labels"),
        "evaluator_failures": failures,
        "duration_ms": result.duration_ms,
        "error_code": result.error_code,
    }


def _median_int(values: list[int]) -> int | None:
    if not values:
        return None
    return int(Decimal(str(statistics.median(values))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _round(value: float) -> float:
    return round(value, 6)


def aggregate_results(results: Sequence[EvalCaseResult]) -> dict[str, Any]:
    """`{cases, passed, failed, errors, pass_rate, scores_avg, duration_ms_p50, cost_usd}` of a run's
    results (JSON-safe):
    - `pass_rate` = passed / cases (errors count as not passed), rounded to 6 places; None without cases;
    - `scores_avg`: per score name, the mean over the cases that have it;
    - `duration_ms_p50`: the median of the recorded durations (half-up to an integer); None without any;
    - `cost_usd`: the exact sum of the case costs as decimal text; None when any case is unpriced."""
    n = len(results)
    passed = sum(1 for r in results if r.status == "passed")
    failed = sum(1 for r in results if r.status == "failed")
    errors = sum(1 for r in results if r.status == "error")
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in results:
        for name, outcome in r.scores.items():
            sums[name] = sums.get(name, 0.0) + float(outcome["value"])
            counts[name] = counts.get(name, 0) + 1
    cost: Decimal | None = Decimal(0)
    for r in results:
        if r.cost_usd is None:
            cost = None
            break
        cost += r.cost_usd
    return {
        "cases": n,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "pass_rate": _round(passed / n) if n else None,
        "scores_avg": {name: _round(sums[name] / counts[name]) for name in sorted(sums)},
        "duration_ms_p50": _median_int([r.duration_ms for r in results if r.duration_ms is not None]),
        "cost_usd": _decimal_str(cost),
    }


def _delta_num(base: float | int | None, target: float | int | None) -> float | int | None:
    if base is None or target is None:
        return None
    delta = target - base
    return _round(delta) if isinstance(delta, float) else delta


def _delta_cost(base: Decimal | str | None, target: Decimal | str | None) -> str | None:
    if base is None or target is None:
        return None
    return _decimal_str(Decimal(str(target)) - Decimal(str(base)))


def _change(base: EvalCaseResult | None, target: EvalCaseResult | None) -> str:
    if base is None:
        return "added"
    if target is None:
        return "removed"
    if base.status != "passed" and target.status == "passed":
        return "fixed"
    if base.status == "passed" and target.status != "passed":
        return "regressed"
    return "unchanged"


# ---------------------------------------------------------------------------- the store


class EvalStore:
    """Suites, cases, results, aggregates and compare. Every method is tenant-scoped."""

    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    # ------------------------------------------------------------------ suites

    def create_suite(
        self,
        tenant_id: str,
        agent_id: UUID | str,
        name: str,
        *,
        description: str | None = None,
        evaluators: Sequence[Any] = (),
        suite_id: UUID | str | None = None,
    ) -> EvalSuiteRecord:
        """Create a suite for `agent_id` (the caller has checked the agent is the tenant's).
        Raises EvalSuiteInvalid (bad name, description or evaluators, at most 10) or
        EvalSuiteNameConflict."""
        return self._create_suite(
            tenant_id, ("agent", str(agent_id)), name, description, evaluators, suite_id
        )

    def create_workflow_suite(
        self,
        tenant_id: str,
        workflow_id: UUID | str,
        name: str,
        *,
        description: str | None = None,
        evaluators: Sequence[Any] = (),
        suite_id: UUID | str | None = None,
    ) -> EvalSuiteRecord:
        """As `create_suite`, for a suite that targets `workflow_id` (the caller has checked the
        workflow is the tenant's)."""
        return self._create_suite(
            tenant_id, ("workflow", str(workflow_id)), name, description, evaluators, suite_id
        )

    def _create_suite(
        self,
        tenant_id: str,
        target: tuple[str, str],
        name: str,
        description: str | None,
        evaluators: Sequence[Any],
        suite_id: UUID | str | None,
    ) -> EvalSuiteRecord:
        _require_tenant(tenant_id)
        name = _validate_suite_name(name)
        description = _validate_description(description)
        specs = _suite_evaluators(evaluators)
        kind, target_id = target
        with self.Session() as session:
            row = EvalSuiteModel(
                id=str(suite_id or uuid4()),
                tenant_id=tenant_id,
                agent_id=target_id if kind == "agent" else None,
                workflow_id=target_id if kind == "workflow" else None,
                name=name,
                description=description,
                evaluators=_dump_evaluators(specs),
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                if _constraint(exc) in NAME_CONFLICT_CONSTRAINTS:
                    raise EvalSuiteNameConflict(target_id, name, kind) from None
                raise
            session.commit()
            session.refresh(row)
            return _suite_record(row, 0)

    def get_suite(
        self,
        tenant_id: str,
        suite_id: UUID | str,
        *,
        agent_id: UUID | str | None = None,
        workflow_id: UUID | str | None = None,
    ) -> EvalSuiteRecord:
        """The suite, or EvalSuiteNotFound (unknown, another tenant's, or another target's)."""
        with self.Session() as session:
            row = self._suite(session, tenant_id, suite_id, agent_id, workflow_id=workflow_id)
            return _suite_record(row, self._case_count(session, tenant_id, row.id))

    def list_suites(
        self, tenant_id: str, agent_id: UUID | str, *, include_archived: bool = False
    ) -> list[EvalSuiteRecord]:
        """The agent's suites by name, with their case counts."""
        return self._list_suites(tenant_id, EvalSuiteModel.agent_id == str(agent_id), include_archived)

    def list_workflow_suites(
        self, tenant_id: str, workflow_id: UUID | str, *, include_archived: bool = False
    ) -> list[EvalSuiteRecord]:
        """The workflow's suites by name, with their case counts."""
        return self._list_suites(tenant_id, EvalSuiteModel.workflow_id == str(workflow_id), include_archived)

    def _list_suites(self, tenant_id: str, target_filter, include_archived: bool) -> list[EvalSuiteRecord]:
        counts = (
            select(EvalCaseModel.suite_id, func.count().label("n"))
            .where(EvalCaseModel.tenant_id == tenant_id)
            .group_by(EvalCaseModel.suite_id)
            .subquery()
        )
        stmt = (
            select(EvalSuiteModel, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.suite_id == EvalSuiteModel.id)
            .where(EvalSuiteModel.tenant_id == tenant_id, target_filter)
            .order_by(EvalSuiteModel.name, EvalSuiteModel.id)
        )
        if not include_archived:
            stmt = stmt.where(EvalSuiteModel.archived_at.is_(None))
        with self.Session() as session:
            return [_suite_record(row, int(n)) for row, n in session.execute(stmt).all()]

    def update_suite(
        self,
        tenant_id: str,
        suite_id: UUID | str,
        *,
        agent_id: UUID | str | None = None,
        name: str = _UNSET,
        description: str | None = _UNSET,
        evaluators: Sequence[Any] = _UNSET,
        archived: bool | None = None,
    ) -> EvalSuiteRecord:
        """Change the given fields. `archived=True` archives, `False` restores. An archived suite
        accepts only `archived=False` (EvalSuiteArchived otherwise). New suite evaluators are
        checked for name clashes with every case's (EvalSuiteInvalid)."""
        with self.Session() as session:
            row = self._suite(session, tenant_id, suite_id, agent_id, lock=True)
            sid = str(row.id)
            edits = name is not _UNSET or description is not _UNSET or evaluators is not _UNSET
            if row.archived_at is not None and archived is not False and (edits or archived is None):
                raise EvalSuiteArchived(str(row.id))
            values: dict[str, Any] = {}
            if name is not _UNSET:
                values["name"] = _validate_suite_name(name)
            if description is not _UNSET:
                values["description"] = _validate_description(description)
            if evaluators is not _UNSET:
                specs = _suite_evaluators(evaluators)
                for case in session.execute(
                    select(EvalCaseModel).where(EvalCaseModel.tenant_id == tenant_id, EvalCaseModel.suite_id == row.id)
                ).scalars():
                    _check_names(specs, _evaluators(case.evaluators, "case"), f"cases.{case.name}.evaluators")
                values["evaluators"] = _dump_evaluators(specs)
            if archived is not None:
                values["archived_at"] = (row.archived_at or _now()) if archived else None
            if values:
                values["updated_at"] = _now()
                try:
                    session.execute(
                        update(EvalSuiteModel)
                        .where(EvalSuiteModel.tenant_id == tenant_id, EvalSuiteModel.id == sid)
                        .values(**values)
                    )
                    session.flush()
                except IntegrityError as exc:
                    session.rollback()
                    if _constraint(exc) in NAME_CONFLICT_CONSTRAINTS:
                        raise EvalSuiteNameConflict(
                            row.workflow_id if row.workflow_id is not None else row.agent_id,
                            values["name"],
                            "workflow" if row.workflow_id is not None else "agent",
                        ) from None
                    raise
            session.commit()
        return self.get_suite(tenant_id, sid)

    def delete_suite(self, tenant_id: str, suite_id: UUID | str, *, agent_id: UUID | str | None = None) -> None:
        """Delete the suite and its cases (EvalSuiteNotFound if not the tenant's). Eval runs and
        their results stay (they hold their own snapshot)."""
        with self.Session() as session:
            row = self._suite(session, tenant_id, suite_id, agent_id, lock=True)
            session.execute(
                delete(EvalSuiteModel).where(EvalSuiteModel.tenant_id == tenant_id, EvalSuiteModel.id == row.id)
            )
            session.commit()

    # ------------------------------------------------------------------ cases

    def add_case(
        self,
        tenant_id: str,
        suite_id: UUID | str,
        case: EvalCase | Mapping[str, Any],
        *,
        position: int | None = None,
        case_id: UUID | str | None = None,
        agent_id: UUID | str | None = None,
    ) -> EvalCaseRecord:
        """Add a case (default position: after the last). Raises EvalSuiteNotFound,
        EvalSuiteArchived, EvalSuiteInvalid, EvalCaseNameConflict or EvalCaseLimitExceeded (500)."""
        valid = _validate_case(case)
        if position is not None and position < 0:
            raise EvalSuiteInvalid("position must be >= 0", "position")
        with self.Session() as session:
            suite = self._writable_suite(session, tenant_id, suite_id, agent_id)
            _check_names(_evaluators(suite.evaluators, "evaluators"), valid.evaluators, f"cases.{valid.name}.evaluators")
            if self._case_count(session, tenant_id, suite.id) + 1 > MAX_SUITE_CASES:
                raise EvalCaseLimitExceeded(str(suite.id), MAX_SUITE_CASES)
            if position is None:
                position = self._next_position(session, tenant_id, suite.id)
            row = self._new_case_row(tenant_id, suite.id, valid, position, case_id)
            session.add(row)
            self._flush_case(session, suite.id, valid.name)
            self._touch_suite(session, tenant_id, suite.id)
            session.commit()
            session.refresh(row)
            return _case_record(row)

    def get_case(
        self,
        tenant_id: str,
        case_id: UUID | str,
        *,
        suite_id: UUID | str | None = None,
        agent_id: UUID | str | None = None,
    ) -> EvalCaseRecord:
        """The case, or EvalCaseNotFound (unknown, another tenant's, another suite's, or a suite of
        another agent)."""
        with self.Session() as session:
            return _case_record(self._case(session, tenant_id, case_id, suite_id, agent_id))

    def list_cases(self, tenant_id: str, suite_id: UUID | str, *, agent_id: UUID | str | None = None) -> list[EvalCaseRecord]:
        """The suite's cases in execution order (position, then id). EvalSuiteNotFound if not the tenant's."""
        with self.Session() as session:
            suite = self._suite(session, tenant_id, suite_id, agent_id)
            return [_case_record(row) for row in self._cases(session, tenant_id, suite.id)]

    def update_case(
        self,
        tenant_id: str,
        case_id: UUID | str,
        *,
        suite_id: UUID | str | None = None,
        agent_id: UUID | str | None = None,
        name: str = _UNSET,
        input: str = _UNSET,
        expected_output: Any = _UNSET,
        metadata: Mapping[str, Any] = _UNSET,
        evaluators: Sequence[Any] = _UNSET,
        position: int = _UNSET,
    ) -> EvalCaseRecord:
        """Change the given fields; the merged case is re-validated. Same errors as add_case, plus
        EvalCaseNotFound (also when the case is deleted concurrently). Locks the suite, then the case
        (the same order as every case write, so no deadlock)."""
        with self.Session() as session:
            existing = self._case(session, tenant_id, case_id, suite_id, agent_id)
            suite = self._writable_suite(session, tenant_id, existing.suite_id, None)
            row = self._lock_case(session, tenant_id, existing.id)
            merged = {
                "name": row.name if name is _UNSET else name,
                "input": row.input if input is _UNSET else input,
                "expected_output": row.expected_output if expected_output is _UNSET else expected_output,
                "metadata": dict(row.metadata_ or {}) if metadata is _UNSET else metadata,
                "evaluators": row.evaluators if evaluators is _UNSET else evaluators,
            }
            valid = _validate_case(merged)
            _check_names(_evaluators(suite.evaluators, "evaluators"), valid.evaluators, f"cases.{valid.name}.evaluators")
            if position is not _UNSET:
                if not isinstance(position, int) or position < 0:
                    raise EvalSuiteInvalid("position must be >= 0", "position")
                row.position = position
            self._apply_case(row, valid)
            self._flush_case(session, suite.id, valid.name)
            self._touch_suite(session, tenant_id, suite.id)
            session.commit()
            session.refresh(row)
            return _case_record(row)

    def delete_case(
        self,
        tenant_id: str,
        case_id: UUID | str,
        *,
        suite_id: UUID | str | None = None,
        agent_id: UUID | str | None = None,
    ) -> None:
        """Delete the case. EvalCaseNotFound if it is not the tenant's (suite's, agent's), or was
        deleted concurrently."""
        with self.Session() as session:
            existing = self._case(session, tenant_id, case_id, suite_id, agent_id)
            suite = self._writable_suite(session, tenant_id, existing.suite_id, None)
            gone = session.execute(
                delete(EvalCaseModel)
                .where(EvalCaseModel.tenant_id == tenant_id, EvalCaseModel.id == existing.id)
                .returning(EvalCaseModel.id)
            ).fetchone()
            if gone is None:
                session.rollback()
                raise EvalCaseNotFound(tenant_id, case_id)
            self._touch_suite(session, tenant_id, suite.id)
            session.commit()

    def upsert_cases(
        self,
        tenant_id: str,
        suite_id: UUID | str,
        cases: Sequence[EvalCase | Mapping[str, Any]],
        *,
        evaluators: Sequence[Any] = _UNSET,
        agent_id: UUID | str | None = None,
        keep_case_evaluators: bool = False,
    ) -> ImportResult:
        """Import: upsert `cases` by name in ONE transaction (all or nothing). An existing case keeps
        its id and position; new cases are appended in the given order. `evaluators`, when given,
        replaces the suite's. `keep_case_evaluators`: an existing case keeps its own evaluators (for
        a file format that has no place for them, CSV). Raises EvalSuiteInvalid (a bad case, duplicate names, name clashes),
        EvalCaseLimitExceeded, EvalSuiteArchived or EvalSuiteNotFound."""
        valid = [_validate_case(c) for c in cases]
        names = [c.name for c in valid]
        if len(set(names)) != len(names):
            dup = next(n for n in names if names.count(n) > 1)
            raise EvalSuiteInvalid(f"duplicate case name {dup!r}", "cases")
        with self.Session() as session:
            suite = self._writable_suite(session, tenant_id, suite_id, agent_id)
            existing = {row.name: row for row in self._cases(session, tenant_id, suite.id)}
            suite_specs = (
                _evaluators(suite.evaluators, "evaluators")
                if evaluators is _UNSET
                else _suite_evaluators(evaluators)
            )
            new = [c for c in valid if c.name not in existing]
            if len(existing) + len(new) > MAX_SUITE_CASES:
                raise EvalCaseLimitExceeded(str(suite.id), MAX_SUITE_CASES)
            imported = {c.name for c in valid}
            for case in valid:
                if keep_case_evaluators and case.name in existing:
                    continue  # its stored evaluators are checked below
                _check_names(suite_specs, case.evaluators, f"cases.{case.name}.evaluators")
            for name, row in existing.items():
                if name not in imported or keep_case_evaluators:
                    _check_names(suite_specs, _evaluators(row.evaluators, "case"), f"cases.{name}.evaluators")
            position = self._next_position(session, tenant_id, suite.id)
            for case in valid:
                if case.name in existing:
                    self._apply_case(existing[case.name], case, keep_evaluators=keep_case_evaluators)
                else:
                    session.add(self._new_case_row(tenant_id, suite.id, case, position, None))
                    position += 1
            if evaluators is not _UNSET:
                suite.evaluators = _dump_evaluators(suite_specs)
            self._flush_case(session, suite.id, "")
            self._touch_suite(session, tenant_id, suite.id)
            session.commit()
            return ImportResult(created=len(new), updated=len(valid) - len(new))

    # ------------------------------------------------------------------ snapshot

    def snapshot(
        self,
        tenant_id: str,
        suite_id: UUID | str,
        *,
        agent_id: UUID | str | None = None,
        workflow_id: UUID | str | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> EvalSuiteSnapshot:
        """The suite frozen for an eval run: its evaluators and its cases (with ids and positions).
        Store `snapshot.canonical_json()` as the run input; `snapshot.suite_hash()` is the hash.
        Raises EvalSuiteNotFound, EvalSuiteArchived, or EvalSuiteInvalid (no cases, or a
        max_concurrency outside 1..8)."""
        with self.Session() as session:
            # FOR SHARE: every case write locks the suite FOR UPDATE first, so the suite row and its
            # cases are read as one consistent state (no torn snapshot).
            suite = self._suite(session, tenant_id, suite_id, agent_id, lock="share", workflow_id=workflow_id)
            if suite.archived_at is not None:
                raise EvalSuiteArchived(str(suite.id))
            rows = self._cases(session, tenant_id, suite.id)
        if not rows:
            raise EvalSuiteInvalid("the suite has no cases", "cases")
        try:
            return EvalSuiteSnapshot(
                suite_id=UUID(str(suite.id)),
                name=suite.name,
                evaluators=_evaluators(suite.evaluators, "evaluators"),
                cases=tuple(
                    EvalCaseSnapshot(
                        id=UUID(str(row.id)),
                        position=row.position,
                        name=row.name,
                        input=row.input,
                        expected_output=row.expected_output,
                        metadata=dict(row.metadata_ or {}),
                        evaluators=_evaluators(row.evaluators, f"cases.{row.name}.evaluators"),
                    )
                    for row in rows
                ),
                max_concurrency=max_concurrency,
            )
        except ValidationError as exc:
            raise EvalSuiteInvalid(_first_error(exc)) from None

    # ------------------------------------------------------------------ results

    def record_case_result(
        self,
        tenant_id: str,
        run_id: UUID | str,
        result: CaseResultWrite,
        *,
        judge_usage: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[EvalCaseResult, bool]:
        """Record one case's result, exactly once per (run_id, case_id), in ONE transaction under
        the eval run's row lock: the judge usage rows (each a run_model_requests row as for
        EventLog.append's `usage`, with `request_key = judge:{case_id}:{i}`; first write wins), the
        `eval_case_results` row, and the `eval_case_result` event (key `case:{case_id}`, data
        `{case_id, position, status, assertions: {passed, total}, scores, labels, evaluator_failures
        (counts), duration_ms, cost_usd, error_code, child_run_id}`; no text).

        The case cost is computed here from durable rows: the child run's usage rows plus this
        case's judge rows on the eval run; None when any of them is unpriced, 0 without any.

        Returns (result, created). A repeat (the event exists) writes nothing and returns the first
        result. Raises RunNotFound (unknown run or another tenant's), EvalRunNotFound (not an eval
        run) or ValueError (a malformed result or judge key)."""
        _require_tenant(tenant_id)
        values = _validate_result(result)
        case_id = values["case_id"]
        prefix = f"judge:{case_id}:"
        usage_rows = [dict(u) for u in judge_usage]
        for row in usage_rows:
            key = row.get("request_key")
            if not isinstance(key, str) or not key.startswith(prefix) or not key[len(prefix):].isdecimal():
                raise ValueError(f"judge usage request_key must be {prefix}<evaluator index>")
            row.setdefault("operation", "chat")
        run_id = str(run_id)
        with self.Session() as session:
            kind = session.execute(
                text("SELECT kind FROM runs WHERE id = :r AND tenant_id = :t FOR UPDATE"),
                {"r": run_id, "t": tenant_id},
            ).scalar_one_or_none()
            if kind is None:
                session.rollback()
                raise RunNotFound(tenant_id, run_id)
            if kind != "eval":
                session.rollback()
                raise EvalRunNotFound(tenant_id, run_id)
            existing = self._result(session, tenant_id, run_id, case_id)
            if existing is not None:
                session.rollback()
                return _result_record(existing), False
            for usage in usage_rows:
                insert_usage_in_session(session, tenant_id, run_id, usage)
            cost = self._case_cost(session, tenant_id, run_id, values["child_run_id"], prefix)
            session.execute(
                pg_insert(EvalCaseResultModel.__table__)
                .values(tenant_id=tenant_id, run_id=run_id, cost_usd=cost, **values)
                .on_conflict_do_nothing(constraint="eval_case_results_pkey")
            )
            assertions = values["assertions"]
            append_event_in_session(
                session,
                tenant_id,
                run_id,
                case_event_key(case_id),
                EVAL_CASE_RESULT_EVENT,
                {
                    "case_id": case_id,
                    "position": values["position"],
                    "status": values["status"],
                    "assertions": {
                        "passed": sum(1 for a in assertions.values() if a["value"] is True),
                        "total": len(assertions),
                    },
                    "scores": len(values["scores"]),
                    "labels": len(values["labels"]),
                    "evaluator_failures": len(values["evaluator_failures"]),
                    "duration_ms": values["duration_ms"],
                    "cost_usd": _decimal_str(cost),
                    "error_code": values["error_code"],
                    "child_run_id": values["child_run_id"],
                },
            )
            session.commit()
            row = self._result(session, tenant_id, run_id, case_id)
            return _result_record(row), True

    def list_case_results(
        self,
        tenant_id: str,
        run_id: UUID | str,
        *,
        agent_id: UUID | str | None = None,
        after: tuple[int, str] | None = None,
        limit: int | None = None,
        workflow_id: UUID | str | None = None,
    ) -> list[EvalCaseResult]:
        """The eval run's results by position, then case id; with `after` = (position, case_id)
        only those past it, and at most `limit` (1..MAX_LIST_RESULTS) when given. EvalRunNotFound
        if not the tenant's (or the agent's) eval run."""
        with self.Session() as session:
            self._eval_run(session, tenant_id, run_id, agent_id, workflow_id)
            return [
                _result_record(r) for r in self._results(session, tenant_id, str(run_id), after=after, limit=limit)
            ]

    def aggregates_for_runs(self, tenant_id: str, run_ids: Sequence[UUID | str]) -> dict[str, dict[str, Any]]:
        """`aggregate_results` of each of `run_ids` (the tenant's only), in ONE query: run id ->
        aggregates (a run without results has the empty aggregates)."""
        ids = [str(r) for r in run_ids]
        if not ids:
            return {}
        by_run: dict[str, list[EvalCaseResult]] = {i: [] for i in ids}
        with self.Session() as session:
            rows = session.execute(
                select(EvalCaseResultModel)
                .where(EvalCaseResultModel.tenant_id == tenant_id, EvalCaseResultModel.run_id.in_(ids))
                .order_by(EvalCaseResultModel.run_id, EvalCaseResultModel.position, EvalCaseResultModel.case_id)
            ).scalars()
            for row in rows:
                by_run[row.run_id].append(_result_record(row))
        return {run_id: aggregate_results(results) for run_id, results in by_run.items()}

    def aggregates(
        self, tenant_id: str, run_id: UUID | str, *, agent_id: UUID | str | None = None,
        workflow_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """See `aggregate_results`. EvalRunNotFound if not the tenant's (or the agent's, or the
        workflow's) eval run."""
        return aggregate_results(self.list_case_results(tenant_id, run_id, agent_id=agent_id, workflow_id=workflow_id))

    def list_runs(
        self,
        tenant_id: str,
        agent_id: UUID | str,
        *,
        suite_id: UUID | str | None = None,
        limit: int = 50,
        before: RunCursor | None = None,
        workflow: bool = False,
    ) -> list[dict[str, Any]]:
        """The agent's eval runs (optionally one suite's), newest first by (created_at, id), as
        RunStore run dicts; with `before` (RunCursor.after(last row of the previous page)) only the
        runs past it. `workflow`: `agent_id` names a workflow, whose eval runs are listed."""
        limit = max(1, min(int(limit), MAX_LIST_RUNS))
        target = RunModel.workflow_id if workflow else RunModel.agent_id
        stmt = (
            select(RunModel)
            .where(RunModel.tenant_id == tenant_id, RunModel.kind == "eval", target == str(agent_id))
            .order_by(RunModel.created_at.desc(), RunModel.id.desc())
            .limit(limit)
        )
        if before is not None:
            stmt = stmt.where(tuple_(RunModel.created_at, RunModel.id) < tuple_(before.created_at, before.id))
        if suite_id is not None:
            stmt = stmt.where(RunModel.eval_suite_id == str(suite_id))
        with self.Session() as session:
            return [_run_dict(run) for run in session.execute(stmt).scalars()]

    def compare(
        self,
        tenant_id: str,
        base_run_id: UUID | str,
        target_run_id: UUID | str,
        *,
        agent_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """Compare two eval runs of the same agent (EvalRunNotFound for a run that is not the
        tenant's eval run, not `agent_id`'s when given, or of another agent than the base run).

        Cases are matched by case id, then the rest by name. Each case's `change` is `fixed`
        (target passed, base did not), `regressed` (base passed, target did not), `unchanged`,
        `added` (target only) or `removed` (base only). Deltas are target - base (None when a side
        has no value). `same_suite` compares the suite hashes recorded at run creation (`runs.eval_suite_hash`); `warnings` says when
        they differ. JSON-safe (costs as decimal text)."""
        with self.Session() as session:
            base = self._eval_run(session, tenant_id, base_run_id, agent_id)
            target = self._eval_run(session, tenant_id, target_run_id, agent_id or base.agent_id)
            base_results = [_result_record(r) for r in self._results(session, tenant_id, base.id)]
            target_results = [_result_record(r) for r in self._results(session, tenant_id, target.id)]
        base_hash, target_hash = base.eval_suite_hash, target.eval_suite_hash
        same_suite = base_hash is not None and base_hash == target_hash

        # Match by case id, then by name among the unmatched.
        pairs: list[tuple[EvalCaseResult | None, EvalCaseResult | None]] = []
        base_by_id = {r.case_id: r for r in base_results}
        matched_base: set[str] = set()
        unmatched_target: list[EvalCaseResult] = []
        for t in target_results:
            b = base_by_id.get(t.case_id)
            if b is not None:
                pairs.append((b, t))
                matched_base.add(b.case_id)
            else:
                unmatched_target.append(t)
        base_by_name = {r.case_name: r for r in base_results if r.case_id not in matched_base}
        for t in unmatched_target:
            b = base_by_name.pop(t.case_name, None)
            if b is not None:
                matched_base.add(b.case_id)
            pairs.append((b, t))
        pairs.sort(key=lambda p: (p[1].position, p[1].case_id))  # target order
        removed = sorted(
            (r for r in base_results if r.case_id not in matched_base), key=lambda r: (r.position, r.case_id)
        )
        pairs.extend((r, None) for r in removed)

        cases = [self._compare_case(b, t) for b, t in pairs]
        base_agg, target_agg = aggregate_results(base_results), aggregate_results(target_results)
        score_names = sorted(set(base_agg["scores_avg"]) | set(target_agg["scores_avg"]))
        counts = {c: sum(1 for x in cases if x["change"] == c) for c in ("fixed", "regressed", "unchanged", "added", "removed")}
        warnings = []
        if not same_suite:
            warnings.append("The runs used different test suites (their suite hashes differ).")
        return {
            "base": self._compare_side(base, base_hash, base_agg),
            "target": self._compare_side(target, target_hash, target_agg),
            "same_suite": same_suite,
            "warnings": warnings,
            "summary": {
                **counts,
                "pass_rate_delta": _delta_num(base_agg["pass_rate"], target_agg["pass_rate"]),
                "scores_avg_delta": {
                    n: _delta_num(base_agg["scores_avg"].get(n), target_agg["scores_avg"].get(n)) for n in score_names
                },
                "cost_usd_delta": _delta_cost(base_agg["cost_usd"], target_agg["cost_usd"]),
                "duration_ms_p50_delta": _delta_num(base_agg["duration_ms_p50"], target_agg["duration_ms_p50"]),
            },
            "cases": cases,
        }

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _compare_side(run: RunModel, suite_hash: str | None, agg: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run.id,
            "agent_id": run.agent_id,
            "agent_version": run.agent_version,
            "draft": run.agent_version is None,
            "suite_id": run.eval_suite_id,
            "suite_hash": suite_hash,
            "status": run.status,
            "aggregates": agg,
        }

    @staticmethod
    def _compare_case(b: EvalCaseResult | None, t: EvalCaseResult | None) -> dict[str, Any]:
        names = sorted(set(b.assertions if b else {}) | set(t.assertions if t else {}))
        score_names = sorted(set(b.scores if b else {}) | set(t.scores if t else {}))
        return {
            "name": (t or b).case_name,
            "base_case_id": b.case_id if b else None,
            "target_case_id": t.case_id if t else None,
            "base_status": b.status if b else None,
            "target_status": t.status if t else None,
            "change": _change(b, t),
            "assertions": {
                n: {
                    "base": (b.assertions.get(n) or {}).get("value") if b else None,
                    "target": (t.assertions.get(n) or {}).get("value") if t else None,
                }
                for n in names
            },
            "score_deltas": {
                n: _delta_num(
                    (b.scores.get(n) or {}).get("value") if b else None,
                    (t.scores.get(n) or {}).get("value") if t else None,
                )
                for n in score_names
            },
            "duration_ms_delta": _delta_num(b.duration_ms if b else None, t.duration_ms if t else None),
            "cost_usd_delta": _delta_cost(b.cost_usd if b else None, t.cost_usd if t else None),
        }

    @staticmethod
    def _suite(
        session, tenant_id: str, suite_id, agent_id, *, lock: bool | str = False, workflow_id=None
    ) -> EvalSuiteModel:
        try:
            sid = str(UUID(str(suite_id)))
        except ValueError:
            raise EvalSuiteNotFound(tenant_id, suite_id) from None
        stmt = select(EvalSuiteModel).where(EvalSuiteModel.tenant_id == tenant_id, EvalSuiteModel.id == sid)
        if agent_id is not None:
            stmt = stmt.where(EvalSuiteModel.agent_id == str(agent_id))
        if workflow_id is not None:
            stmt = stmt.where(EvalSuiteModel.workflow_id == str(workflow_id))
        if lock == "share":
            stmt = stmt.with_for_update(read=True)
        elif lock:
            stmt = stmt.with_for_update()
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise EvalSuiteNotFound(tenant_id, suite_id)
        return row

    def _writable_suite(self, session, tenant_id: str, suite_id, agent_id) -> EvalSuiteModel:
        """The suite, locked (serialises case writes, so the case cap holds), and not archived."""
        row = self._suite(session, tenant_id, suite_id, agent_id, lock=True)
        if row.archived_at is not None:
            raise EvalSuiteArchived(str(row.id))
        return row

    @staticmethod
    def _lock_case(session, tenant_id: str, case_id: str) -> EvalCaseModel:
        """The case row FOR UPDATE, re-read (not the identity map's copy); EvalCaseNotFound when a
        concurrent delete removed it."""
        row = session.execute(
            select(EvalCaseModel)
            .where(EvalCaseModel.tenant_id == tenant_id, EvalCaseModel.id == str(case_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None:
            session.rollback()
            raise EvalCaseNotFound(tenant_id, case_id)
        return row

    @staticmethod
    def _case(session, tenant_id: str, case_id, suite_id, agent_id=None) -> EvalCaseModel:
        try:
            cid = str(UUID(str(case_id)))
        except ValueError:
            raise EvalCaseNotFound(tenant_id, case_id) from None
        stmt = select(EvalCaseModel).where(EvalCaseModel.tenant_id == tenant_id, EvalCaseModel.id == cid)
        if agent_id is not None:
            stmt = stmt.join(
                EvalSuiteModel,
                (EvalSuiteModel.tenant_id == EvalCaseModel.tenant_id) & (EvalSuiteModel.id == EvalCaseModel.suite_id),
            ).where(EvalSuiteModel.agent_id == str(agent_id))
        if suite_id is not None:
            try:
                stmt = stmt.where(EvalCaseModel.suite_id == str(UUID(str(suite_id))))
            except ValueError:
                raise EvalCaseNotFound(tenant_id, case_id) from None
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            raise EvalCaseNotFound(tenant_id, case_id)
        return row

    @staticmethod
    def _cases(session, tenant_id: str, suite_id: str) -> list[EvalCaseModel]:
        return list(
            session.execute(
                select(EvalCaseModel)
                .where(EvalCaseModel.tenant_id == tenant_id, EvalCaseModel.suite_id == str(suite_id))
                .order_by(EvalCaseModel.position, EvalCaseModel.id)
            ).scalars()
        )

    @staticmethod
    def _case_count(session, tenant_id: str, suite_id: str) -> int:
        return int(
            session.execute(
                select(func.count()).where(EvalCaseModel.tenant_id == tenant_id, EvalCaseModel.suite_id == str(suite_id))
            ).scalar_one()
        )

    @staticmethod
    def _next_position(session, tenant_id: str, suite_id: str) -> int:
        top = session.execute(
            select(func.max(EvalCaseModel.position)).where(
                EvalCaseModel.tenant_id == tenant_id, EvalCaseModel.suite_id == str(suite_id)
            )
        ).scalar_one()
        return 0 if top is None else int(top) + 1

    @staticmethod
    def _new_case_row(tenant_id: str, suite_id: str, case: EvalCase, position: int, case_id) -> EvalCaseModel:
        row = EvalCaseModel(id=str(case_id or uuid4()), tenant_id=tenant_id, suite_id=str(suite_id), position=position)
        EvalStore._apply_case(row, case)
        return row

    @staticmethod
    def _apply_case(row: EvalCaseModel, case: EvalCase, keep_evaluators: bool = False) -> None:
        row.name = case.name
        row.input = case.input
        row.expected_output = null() if case.expected_output is None else case.expected_output
        row.metadata_ = dict(case.metadata)
        if not keep_evaluators:
            row.evaluators = _dump_evaluators(case.evaluators)
        row.updated_at = _now()

    @staticmethod
    def _flush_case(session, suite_id: str, name: str) -> None:
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            if _constraint(exc) == "uq_eval_cases_suite_name":
                raise EvalCaseNameConflict(str(suite_id), name) from None
            raise

    @staticmethod
    def _touch_suite(session, tenant_id: str, suite_id: str) -> None:
        session.execute(
            update(EvalSuiteModel)
            .where(EvalSuiteModel.tenant_id == tenant_id, EvalSuiteModel.id == str(suite_id))
            .values(updated_at=_now())
        )

    @staticmethod
    def _eval_run(session, tenant_id: str, run_id, agent_id, workflow_id=None) -> RunModel:
        stmt = select(RunModel).where(
            RunModel.tenant_id == tenant_id, RunModel.id == str(run_id), RunModel.kind == "eval"
        )
        if agent_id is not None:
            stmt = stmt.where(RunModel.agent_id == str(agent_id))
        if workflow_id is not None:
            stmt = stmt.where(RunModel.workflow_id == str(workflow_id))
        run = session.execute(stmt).scalar_one_or_none()
        if run is None:
            raise EvalRunNotFound(tenant_id, run_id)
        return run

    @staticmethod
    def _result(session, tenant_id: str, run_id: str, case_id: str) -> EvalCaseResultModel | None:
        return session.execute(
            select(EvalCaseResultModel).where(
                EvalCaseResultModel.tenant_id == tenant_id,
                EvalCaseResultModel.run_id == run_id,
                EvalCaseResultModel.case_id == case_id,
            )
        ).scalar_one_or_none()

    @staticmethod
    def _results(
        session, tenant_id: str, run_id: str, *, after: tuple[int, str] | None = None, limit: int | None = None
    ) -> list[EvalCaseResultModel]:
        stmt = (
            select(EvalCaseResultModel)
            .where(EvalCaseResultModel.tenant_id == tenant_id, EvalCaseResultModel.run_id == run_id)
            .order_by(EvalCaseResultModel.position, EvalCaseResultModel.case_id)
        )
        if after is not None:
            position, case_id = after
            stmt = stmt.where(
                tuple_(EvalCaseResultModel.position, EvalCaseResultModel.case_id)
                > tuple_(literal(int(position)), literal(str(case_id), type_=EvalCaseResultModel.__table__.c.case_id.type))
            )
        if limit is not None:
            stmt = stmt.limit(max(1, min(int(limit), MAX_LIST_RESULTS)))
        return list(session.execute(stmt).scalars())

    @staticmethod
    def _case_cost(session, tenant_id: str, run_id: str, child_run_id: str | None, judge_prefix: str) -> Decimal | None:
        """The child run's usage rows plus this case's judge rows: the exact sum, None when any is
        unpriced, 0 without rows."""
        q = RunModelRequestModel
        own = (q.run_id == run_id) & q.request_key.startswith(judge_prefix, autoescape=True)
        # A workflow case's child also has agent-step runs of its own (their rows count too).
        steps = select(RunModel.id).where(RunModel.tenant_id == tenant_id, RunModel.parent_run_id == child_run_id)
        cond = own if child_run_id is None else (own | (q.run_id == child_run_id) | q.run_id.in_(steps))
        n, priced, total = session.execute(
            select(func.count(), func.count(q.cost_usd), func.coalesce(func.sum(q.cost_usd), 0)).where(
                q.tenant_id == tenant_id, cond
            )
        ).one()
        return Decimal(total) if n == priced else None


def _require_tenant(tenant_id: str) -> None:
    if not tenant_id:
        raise ValueError("tenant_id is required")


__all__ = [
    "CASE_STATUSES",
    "CaseResultWrite",
    "EvalCaseLimitExceeded",
    "EvalCaseModel",
    "EvalCaseNameConflict",
    "EvalCaseNotFound",
    "EvalCaseRecord",
    "EvalCaseResult",
    "EvalCaseResultModel",
    "EvalRunNotFound",
    "EvalStore",
    "EvalSuiteArchived",
    "EvalSuiteModel",
    "EvalSuiteNameConflict",
    "EvalSuiteNotFound",
    "EvalSuiteRecord",
    "ImportResult",
    "MAX_LIST_RESULTS",
    "MAX_LIST_RUNS",
    "aggregate_results",
    "case_event_key",
    "dump_evaluators",
    "judge_request_key",
]
