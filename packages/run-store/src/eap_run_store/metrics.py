"""Tenant-scoped run and usage metrics over Postgres (P0.6): summary, time series, breakdowns.

Two sources, never mixed in one aggregate:

- **Run counts and latency** come from `runs`: top-level runs only (`parent_run_id IS NULL`; a
  workflow's agent steps are counted through their workflow run), by `created_at` in `[start, end)`.
  Latency is `finished_at - created_at` of runs that have finished (`percentile_cont`, linear
  interpolation, in milliseconds). Served by `ix_runs_tenant_created` / `ix_runs_tenant_kind_created`.
- **Tokens and cost** come from `run_model_requests`, by the request's own `created_at` in
  `[start, end)`. A workflow run makes no model request itself, so a workflow and its child runs are
  never counted twice. Served by `ix_run_model_requests_tenant_created` and, with a model filter,
  `ix_run_model_requests_tenant_model_created`. `cost_usd` is the exact sum of the priced requests;
  unpriced requests (cost NULL) are counted in `unpriced_requests`, never guessed.

Filters:
- `agent_id`: run counts are that agent's top-level runs; usage is every request made by that
  agent's runs, including those it made as a workflow step (the cost is the agent's).
- `kind`: run counts by `runs.kind`; usage by the kind of the top-level run the request belongs to
  (a workflow step's requests count as `workflow`, an eval case run's as `eval`; AC.17).
- `model` (our id `provider:name`) / `provider`: usage by the request's model; a run matches when its
  recorded `model_id` matches or it (or, for a workflow run, one of its child runs) made a request
  with that model.
- `operation` (RM.19): what a request was for, one of OPERATIONS, which partition the usage rows:
  `embedding` (`run_model_requests.operation = 'embedding'`: knowledge base ingest, retrieve, the
  search tester, semantic recall and indexing), and among chat requests `moderation` (request key
  `moderation:`), `observer` (`memory:observe:`), `reflector` (`memory:reflect:`) and `chat` (every
  other model request). The filter also takes `memory`: observer, reflector and the semantic recall
  and index embeddings (`embed:recall:`, `embed:index:`). Usage by the request's operation; a run
  matches when it (or one of its child runs) made such a request (its recorded `model_id` alone
  does not count). AC.17 adds `judge` (`judge:`, an eval run's LLM-judge requests; operation column
  `chat`), so `chat` no longer counts them. Failed fallback attempts (`model:{n}:failed:{k}`) stay
  `chat`, attributed to the model that failed; every usage row's `model_id` is the model that
  answered (or failed), so the model breakdown is usage by answering model.

Fallback (AC.17, `summary`'s `fallback`; definition changed in the AC.17 review 2026-11-15): from
`run_model_requests` joined to its run by primary key, and from `runs` (never `execution_events`,
whose `model_message` rows carry the whole response and are too heavy to scan per tenant).
`chat_requests`: the agents' chat requests (`model:{n}` keys; agent runs, workflow steps and eval
cases included). `fell_back`: those answered by a model other than the run's effective model
(`runs.model_id`, recorded at start for child runs created without one; a per-turn override
disables fallback, so it never counts). `billed_failed_attempts`:
`model:{n}:failed:{k}` rows (failed attempts the provider billed; an attempt that raised before a
response has none). `exhausted_runs`: failed agent runs created in the window whose `runs.error`
is FALLBACK_EXHAUSTED_RUN_ERROR (every model of the chain failed; such a request writes no usage
row). `fallback_rate` = (fell_back + exhausted_runs) / (chat_requests + exhausted_runs), None at 0:
the share of agent chat requests NOT answered by the primary model. The chain-only rate (out of
requests on a fallback chain) is not derivable without a migration: a chain request the primary
answered leaves a row identical to one of an agent without fallbacks. Filters: agent and kind as
for usage; model / provider on the run's model (the primary); an operation other than `chat`
gives zeros.

Every query is filtered on the tenant first. Bounds: a range of at most MAX_RANGE, at most
MAX_BUCKETS time buckets, at most MAX_GROUPS breakdown groups, and a per-transaction
`statement_timeout` (MetricsQueryTimeout when it fires). All buckets are UTC.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Float, and_, case, cast, false, func, literal, literal_column, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased, sessionmaker
from sqlalchemy.orm import join as orm_join

from eap_run_store.store import ExecutionEventModel, RunModel, RunModelRequestModel

MAX_RANGE = timedelta(days=90)
MAX_BUCKETS = 400
MAX_GROUPS = 50
DEFAULT_STATEMENT_TIMEOUT_MS = 5000
# runs.error carries no code; the run's durable `run_failed` event does (P0.17's block codes).
BLOCKED_ERROR_CODES = ("input_blocked", "output_blocked")

Interval = Literal["hour", "day"]
INTERVALS: dict[str, timedelta] = {"hour": timedelta(hours=1), "day": timedelta(days=1)}
BreakdownBy = Literal["agent", "model", "operation"]
# What a model request was for (RM.19): a partition of run_model_requests (see the module docstring).
OPERATIONS = ("chat", "moderation", "observer", "reflector", "embedding", "judge")
# Values the `operation` filter takes: every operation plus `memory` (observer, reflector, recall
# and index embeddings), which overlaps the others and is never a breakdown group.
OPERATION_FILTERS = (*OPERATIONS, "memory")
BreakdownSort = Literal["runs", "failed", "requests", "tokens", "cost"]

RUN_METRICS = (
    "runs", "completed", "skipped", "failed", "needs_attention", "in_progress", "blocked", "error_rate",
    "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
)
USAGE_METRICS = (
    "requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd",
    "unpriced_requests", "unmeasured_requests",
)
METRICS = RUN_METRICS + USAGE_METRICS

_COST_QUANTUM = Decimal("1e-10")  # Numeric(20, 10), same text form as the run API's cost_usd


class MetricsRangeError(ValueError):
    """The requested range, interval or group count is out of bounds (the API answers 422)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class MetricsQueryTimeout(Exception):
    """A metrics query hit the statement timeout (the API answers 503)."""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        super().__init__(timeout_ms)

    def __str__(self) -> str:
        return f"metrics query exceeded the {self.timeout_ms} ms statement timeout"


@dataclass(frozen=True)
class MetricsQuery:
    """One tenant's metrics over `[start, end)` (timezone-aware) with optional filters."""

    tenant_id: str
    start: datetime
    end: datetime
    agent_id: str | None = None
    model: str | None = None
    provider: str | None = None
    kind: str | None = None
    operation: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id is required")
        if self.operation is not None and self.operation not in OPERATION_FILTERS:
            raise MetricsRangeError(f"operation must be one of {', '.join(OPERATION_FILTERS)}")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if self.start >= self.end:
            raise MetricsRangeError("from must be before to")
        if self.end - self.start > MAX_RANGE:
            raise MetricsRangeError(f"the range must be at most {MAX_RANGE.days} days")

    def previous(self) -> MetricsQuery:
        """The period of equal length that ends where this one starts."""
        length = self.end - self.start
        return replace(self, start=self.start - length, end=self.start)


# ------------------------------------------------------------------------------- SQL building


def _model_match(column: Any, model: str | None, provider: str | None) -> Any:
    conds = []
    if model is not None:
        conds.append(column == model)
    if provider is not None:
        conds.append(column.startswith(f"{provider}:", autoescape=True))
    return and_(*conds) if conds else literal(True)


def _starts(column: Any, prefix: str) -> Any:
    return column.startswith(prefix, autoescape=True)


def operation_key(req: Any) -> Any:
    """SQL: the request's operation (one of OPERATIONS) from its `operation` column and request key."""
    return case(
        (req.operation == "embedding", literal("embedding")),
        (_starts(req.request_key, "moderation:"), literal("moderation")),
        (_starts(req.request_key, "memory:observe:"), literal("observer")),
        (_starts(req.request_key, "memory:reflect:"), literal("reflector")),
        (_starts(req.request_key, "judge:"), literal("judge")),
        else_=literal("chat"),
    )


def operation_match(req: Any, operation: str | None) -> Any:
    """SQL: the request `req` (a RunModelRequestModel alias) is of `operation` (OPERATION_FILTERS)."""
    if operation is None:
        return literal(True)
    if operation == "memory":
        return or_(
            and_(req.operation == "chat", or_(_starts(req.request_key, "memory:observe:"), _starts(req.request_key, "memory:reflect:"))),
            and_(req.operation == "embedding", or_(_starts(req.request_key, "embed:recall:"), _starts(req.request_key, "embed:index:"))),
        )
    if operation == "embedding":
        return req.operation == "embedding"
    return and_(req.operation == "chat", operation_key(req) == operation)


def _request_match(req: Any, model: str | None, provider: str | None, operation: str | None) -> Any:
    return and_(_model_match(req.model_id, model, provider), operation_match(req, operation))


def run_model_condition(
    run: Any, tenant_id: str, model: str | None, provider: str | None, operation: str | None = None
) -> Any:
    """SQL: the top-level run `run` (a RunModel or alias) used the model / provider (/ operation).

    True when it made a matching model request, or one of its child runs (workflow agent steps)
    did, or (without an operation) its recorded `model_id` matches. Each EXISTS is an index lookup:
    the request table by its primary key `(run_id, …)`, the child runs by `ix_runs_parent_run`."""
    if model is None and provider is None and operation is None:
        return literal(True)
    req = aliased(RunModelRequestModel)
    child = aliased(RunModel)
    child_req = aliased(RunModelRequestModel)
    own = select(literal(1)).where(
        req.run_id == run.id, req.tenant_id == tenant_id, _request_match(req, model, provider, operation)
    ).exists()
    via_child = (
        select(literal(1))
        .select_from(child)
        .join(child_req, and_(child_req.run_id == child.id, child_req.tenant_id == child.tenant_id))
        .where(
            child.parent_run_id == run.id,
            child.tenant_id == tenant_id,
            _request_match(child_req, model, provider, operation),
        )
        .exists()
    )
    if operation is not None:
        return or_(own, via_child)
    return or_(and_(run.model_id.is_not(None), _model_match(run.model_id, model, provider)), own, via_child)


def _run_where(q: MetricsQuery, r: Any) -> list[Any]:
    conds = [
        r.tenant_id == q.tenant_id,
        r.parent_run_id.is_(None),
        r.created_at >= q.start,
        r.created_at < q.end,
    ]
    if q.agent_id is not None:
        conds.append(r.agent_id == q.agent_id)
    if q.kind is not None:
        conds.append(r.kind == q.kind)
    if q.model is not None or q.provider is not None or q.operation is not None:
        conds.append(run_model_condition(r, q.tenant_id, q.model, q.provider, q.operation))
    return conds


def _usage_from(q: MetricsQuery) -> tuple[Any, Any, list[Any]]:
    """(request alias, its run alias, WHERE conditions) for the usage side. The conditions name
    the run alias only when `_needs_run(q)`; select from `_usage_source(...)`, never `req` alone."""
    req = aliased(RunModelRequestModel)
    r = aliased(RunModel)
    conds = [req.tenant_id == q.tenant_id, req.created_at >= q.start, req.created_at < q.end]
    if q.model is not None or q.provider is not None:
        conds.append(_model_match(req.model_id, q.model, q.provider))
    if q.operation is not None:
        conds.append(operation_match(req, q.operation))
    if q.agent_id is not None:
        conds.append(r.agent_id == q.agent_id)
    if q.kind is not None:
        conds.append(_top_kind_is(q, r))
    return req, r, conds


def _needs_run(q: MetricsQuery) -> bool:
    """Whether the usage side's filters read the request's run (agent or kind)."""
    return q.agent_id is not None or q.kind is not None


def _usage_join(q: MetricsQuery, req: Any, r: Any) -> Any:
    """Request -> its run. `r.created_at < q.end` bounds the run side to the window's end (a run is
    always created before its requests), so a large tenant's later history is never joined."""
    return and_(r.tenant_id == req.tenant_id, r.id == req.run_id, r.created_at < q.end)


def _usage_source(q: MetricsQuery, req: Any, r: Any, *, need_run: bool = False) -> Any:
    """The usage side's FROM: the request table alone unless a filter or the grouping reads the
    run, so an unfiltered summary or time series scans only `run_model_requests`."""
    return orm_join(req, r, _usage_join(q, req, r)) if need_run or _needs_run(q) else req


def _run_columns(q: MetricsQuery, r: Any) -> list[Any]:
    latency_ms = cast(func.extract("epoch", r.finished_at - r.created_at), Float) * 1000.0
    blocked = (
        select(literal(1))
        .where(
            ExecutionEventModel.tenant_id == q.tenant_id,
            ExecutionEventModel.run_id == r.id,
            ExecutionEventModel.type == "run_failed",
            ExecutionEventModel.data["error_code"].astext.in_(BLOCKED_ERROR_CODES),
        )
        .exists()
    )
    return [
        func.count().label("runs"),
        func.count().filter(r.status == "completed").label("completed"),
        func.count().filter(r.status == "skipped").label("skipped"),
        func.count().filter(r.status == "failed").label("failed"),
        func.count().filter(r.status == "needs_attention").label("needs_attention"),
        func.count().filter(and_(r.status == "failed", blocked)).label("blocked"),
        func.percentile_cont(0.5).within_group(latency_ms).filter(r.status != "skipped").label("latency_p50_ms"),
        func.percentile_cont(0.95).within_group(latency_ms).filter(r.status != "skipped").label("latency_p95_ms"),
        func.percentile_cont(0.99).within_group(latency_ms).filter(r.status != "skipped").label("latency_p99_ms"),
    ]


def _usage_columns(req: Any) -> list[Any]:
    return [
        func.count().label("requests"),
        func.coalesce(func.sum(req.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(req.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(req.cache_read_tokens), 0).label("cache_read_tokens"),
        func.coalesce(func.sum(req.cache_write_tokens), 0).label("cache_write_tokens"),
        func.coalesce(func.sum(req.cost_usd), 0).label("cost_usd"),
        func.count().filter(req.cost_usd.is_(None)).label("unpriced_requests"),
        func.count().filter(req.usage_measured.is_(False)).label("unmeasured_requests"),
    ]


# ----------------------------------------------------------------------------- shaping results


def _cost(value: Any) -> Decimal:
    return Decimal(value or 0).quantize(_COST_QUANTUM)


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _error_rate(failed: int, terminal: int) -> float | None:
    return None if terminal == 0 else failed / terminal


def _empty_runs() -> dict[str, Any]:
    return {
        "runs": 0, "completed": 0, "skipped": 0, "failed": 0, "needs_attention": 0, "in_progress": 0, "blocked": 0,
        "error_rate": None, "latency_p50_ms": None, "latency_p95_ms": None, "latency_p99_ms": None,
    }


def _empty_usage() -> dict[str, Any]:
    return {
        "requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "cost_usd": _cost(0), "unpriced_requests": 0, "unmeasured_requests": 0,
    }


def _runs_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return _empty_runs()
    m = row._mapping
    finished = m["completed"] + m["failed"] + m["needs_attention"]  # skipped runs are neither successes nor failures
    terminal = finished + m["skipped"]
    return {
        "runs": m["runs"],
        "completed": m["completed"],
        "skipped": m["skipped"],
        "failed": m["failed"],
        "needs_attention": m["needs_attention"],
        "in_progress": m["runs"] - terminal,
        "blocked": m["blocked"],
        "error_rate": _error_rate(m["failed"], finished),
        "latency_p50_ms": _float(m["latency_p50_ms"]),
        "latency_p95_ms": _float(m["latency_p95_ms"]),
        "latency_p99_ms": _float(m["latency_p99_ms"]),
    }


def _usage_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return _empty_usage()
    m = row._mapping
    return {
        "requests": m["requests"],
        "input_tokens": int(m["input_tokens"]),
        "output_tokens": int(m["output_tokens"]),
        "cache_read_tokens": int(m["cache_read_tokens"]),
        "cache_write_tokens": int(m["cache_write_tokens"]),
        "cost_usd": _cost(m["cost_usd"]),
        "unpriced_requests": m["unpriced_requests"],
        "unmeasured_requests": m["unmeasured_requests"],
    }


def bucket_starts(start: datetime, end: datetime, interval: str) -> list[datetime]:
    """The UTC bucket starts covering `[start, end)`: the first is `start` truncated to the
    interval, so it may begin before `start` (only data at or after `start` is counted in it).
    MetricsRangeError for an unknown interval or more than MAX_BUCKETS buckets."""
    step = INTERVALS.get(interval)
    if step is None:
        raise MetricsRangeError("interval must be 'hour' or 'day'")
    first = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if interval == "day":
        first = first.replace(hour=0)
    count = math.ceil((end - first) / step)
    if count > MAX_BUCKETS:
        raise MetricsRangeError(
            f"the range has {count} {interval} buckets; at most {MAX_BUCKETS} are allowed (use a larger interval)"
        )
    return [first + i * step for i in range(count)]


def default_interval(start: datetime, end: datetime) -> str:
    """'hour' for ranges up to two days, else 'day'."""
    return "hour" if end - start <= timedelta(days=2) else "day"


# ------------------------------------------------------------------------------------- store


class MetricsStore:
    """Read-only, tenant-scoped aggregates. Each call is one read-only transaction with
    `statement_timeout` set to `statement_timeout_ms`."""

    def __init__(self, engine: Any, statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS) -> None:
        self.engine = engine
        self.statement_timeout_ms = int(statement_timeout_ms)
        self.Session = sessionmaker(bind=engine)

    @contextmanager
    def _session(self) -> Iterator[Any]:
        try:
            with self.Session() as session, session.begin():
                session.execute(text("SET TRANSACTION READ ONLY"))
                session.execute(text(f"SET LOCAL statement_timeout = {self.statement_timeout_ms}"))
                yield session
        except DBAPIError as exc:
            if getattr(getattr(exc, "orig", None), "sqlstate", None) == "57014":  # query_canceled
                raise MetricsQueryTimeout(self.statement_timeout_ms) from None
            raise

    # -- summary

    def _summary(self, session: Any, q: MetricsQuery) -> dict[str, Any]:
        r = aliased(RunModel)
        runs_row = session.execute(select(*_run_columns(q, r)).select_from(r).where(*_run_where(q, r))).one()
        req, rr, conds = _usage_from(q)
        usage_row = session.execute(
            select(*_usage_columns(req)).select_from(_usage_source(q, req, rr)).where(*conds)
        ).one()
        return {
            **_runs_dict(runs_row),
            **_usage_dict(usage_row),
            "operations": self._operations(session, q),
            "fallback": self._fallback(session, q),
        }

    def _fallback(self, session: Any, q: MetricsQuery) -> dict[str, Any]:
        """The fallback block (see the module docstring): run_model_requests joined to its run by
        primary key, and the window's runs; never execution_events."""
        if q.operation is not None and q.operation != "chat":
            return _fallback_dict(0, 0, 0, 0)
        req = aliased(RunModelRequestModel)
        r = aliased(RunModel)
        failed_row = req.request_key.like("model:%:failed:%")
        answered_row = ~failed_row
        conds = [
            req.tenant_id == q.tenant_id,
            req.created_at >= q.start,
            req.created_at < q.end,
            req.operation == "chat",
            _starts(req.request_key, "model:"),
            r.kind == "agent",
            *_event_run_filters(q, r),
        ]
        if q.model is not None or q.provider is not None:
            conds.append(_model_match(r.model_id, q.model, q.provider))
        row = session.execute(
            select(
                func.count().filter(answered_row).label("chat_requests"),
                func.count().filter(and_(answered_row, r.model_id.is_not(None), req.model_id != r.model_id)).label("fell_back"),
                func.count().filter(failed_row).label("billed_failed_attempts"),
            )
            .select_from(orm_join(req, r, _usage_join(q, req, r)))
            .where(*conds)
        ).one()
        fr = aliased(RunModel)
        fconds = [
            fr.tenant_id == q.tenant_id,
            fr.created_at >= q.start,
            fr.created_at < q.end,
            fr.kind == "agent",
            fr.status == "failed",
            fr.error == FALLBACK_EXHAUSTED_RUN_ERROR,
            *_event_run_filters(q, fr),
        ]
        if q.model is not None or q.provider is not None:
            fconds.append(_model_match(fr.model_id, q.model, q.provider))
        exhausted = session.execute(select(func.count()).select_from(fr).where(*fconds)).scalar_one()
        return _fallback_dict(row.chat_requests, row.fell_back, row.billed_failed_attempts, exhausted)

    def _operations(self, session: Any, q: MetricsQuery) -> dict[str, dict[str, Any]]:
        """Usage per operation (every one of OPERATIONS, zero-filled; they sum to the totals) plus
        `memory` (observer, reflector, recall and index embeddings), under the same filters."""
        req, rr, conds = _usage_from(q)
        key = operation_key(req).label("key")
        rows = session.execute(
            select(key, *_usage_columns(req))
            .select_from(_usage_source(q, req, rr))
            .where(*conds)
            .group_by(literal_column("1"))  # by position, as in _by_request
        ).all()
        out = {op: _empty_usage() for op in OPERATIONS}
        for row in rows:
            out[row.key] = _usage_dict(row)
        memory = session.execute(
            select(*_usage_columns(req))
            .select_from(_usage_source(q, req, rr))
            .where(*conds, operation_match(req, "memory"))
        ).one()
        out["memory"] = _usage_dict(memory)
        return out

    def summary(self, q: MetricsQuery, *, compare_previous: bool = False) -> dict[str, Any]:
        """`{"current": {...}, "previous": {...} | None}` with every metric in METRICS, plus
        `operations`: `{operation: usage metrics}` for OPERATIONS and `memory` (RM.19)."""
        with self._session() as session:
            current = self._summary(session, q)
            previous = self._summary(session, q.previous()) if compare_previous else None
        return {"current": current, "previous": previous}

    # -- time series

    def timeseries(self, q: MetricsQuery, interval: str) -> list[dict[str, Any]]:
        """One entry per bucket (zero-filled; latency and error rate None where there is no data):
        `{"start": <UTC datetime>, **metrics}`."""
        starts = bucket_starts(q.start, q.end, interval)
        r = aliased(RunModel)
        run_bucket = func.date_trunc(interval, func.timezone("UTC", r.created_at)).label("bucket")
        req, rr, conds = _usage_from(q)
        usage_bucket = func.date_trunc(interval, func.timezone("UTC", req.created_at)).label("bucket")
        with self._session() as session:
            run_rows = session.execute(
                select(run_bucket, *_run_columns(q, r)).select_from(r).where(*_run_where(q, r)).group_by(run_bucket)
            ).all()
            usage_rows = session.execute(
                select(usage_bucket, *_usage_columns(req))
                .select_from(_usage_source(q, req, rr))
                .where(*conds)
                .group_by(usage_bucket)
            ).all()
        by_run = {row.bucket.replace(tzinfo=UTC): row for row in run_rows}
        by_usage = {row.bucket.replace(tzinfo=UTC): row for row in usage_rows}
        return [
            {"start": s, **_runs_dict(by_run.get(s)), **_usage_dict(by_usage.get(s))} for s in starts
        ]

    # -- breakdowns

    def breakdown(
        self, q: MetricsQuery, by: str, *, sort: str = "runs", limit: int = 10
    ) -> list[dict[str, Any]]:
        """The top `limit` groups by `sort` (descending; ties by key): `{"key": ..., **metrics}`.

        `by="agent"`: run metrics of the agent's top-level runs and usage of all its requests
        (keys are agent ids; tool and workflow runs have no agent and are not listed).
        `by="model"`: usage per request model id; `runs`, `completed`, `failed` and
        `needs_attention` count the distinct top-level runs whose requests in the range used that
        model, by their status; latency is None.
        `by="operation"`: the same per request operation (OPERATIONS; keys are operation names)."""
        if not 1 <= limit <= MAX_GROUPS:
            raise MetricsRangeError(f"limit must be between 1 and {MAX_GROUPS}")
        if by == "agent":
            return self._by_agent(q, sort, limit)
        if by == "model":
            return self._by_request(q, sort, limit, lambda req: req.model_id)
        if by == "operation":
            return self._by_request(q, sort, limit, operation_key)
        raise MetricsRangeError("by must be 'agent', 'model' or 'operation'")

    def _by_agent(self, q: MetricsQuery, sort: str, limit: int) -> list[dict[str, Any]]:
        r = aliased(RunModel)
        run_key = r.agent_id.label("key")
        run_cols = _run_columns(q, r)
        req, rr, conds = _usage_from(q)
        usage_key = rr.agent_id.label("key")
        usage_cols = _usage_columns(req)

        def run_query(keys: list[str] | None = None) -> Any:
            stmt = (
                select(run_key, *run_cols)
                .select_from(r)
                .where(*_run_where(q, r), r.agent_id.is_not(None))
                .group_by(r.agent_id)
            )
            if keys is not None:
                stmt = stmt.where(r.agent_id.in_(keys) if keys else false())
            return stmt

        def usage_query(keys: list[str] | None = None) -> Any:
            stmt = (
                select(usage_key, *usage_cols)
                .select_from(_usage_source(q, req, rr, need_run=True))
                .where(*conds, rr.agent_id.is_not(None))
                .group_by(rr.agent_id)
            )
            if keys is not None:
                stmt = stmt.where(rr.agent_id.in_(keys) if keys else false())
            return stmt

        with self._session() as session:
            if sort in ("runs", "failed"):
                order = func.count() if sort == "runs" else func.count().filter(r.status == "failed")
                primary = session.execute(run_query().order_by(order.desc(), r.agent_id).limit(limit)).all()
                keys = [row.key for row in primary]
                runs_by = {row.key: row for row in primary}
                usage_by = {row.key: row for row in session.execute(usage_query(keys)).all()}
            else:
                order = _usage_order(req, sort)
                primary = session.execute(usage_query().order_by(order, rr.agent_id).limit(limit)).all()
                keys = [row.key for row in primary]
                usage_by = {row.key: row for row in primary}
                runs_by = {row.key: row for row in session.execute(run_query(keys)).all()}
        return [{"key": k, **_runs_dict(runs_by.get(k)), **_usage_dict(usage_by.get(k))} for k in keys]

    def _by_request(self, q: MetricsQuery, sort: str, limit: int, key_of: Any) -> list[dict[str, Any]]:
        """Groups of requests by `key_of(req)` (the model id or the operation)."""
        req, rr, conds = _usage_from(q)
        key = key_of(req)
        top = aliased(RunModel)
        top_id = top.id
        distinct_runs = func.count(func.distinct(top_id))
        key_label = key.label("key")
        cols = [
            key_label,
            *_usage_columns(req),
            distinct_runs.label("runs"),
            func.count(func.distinct(top_id)).filter(top.status == "completed").label("completed"),
            func.count(func.distinct(top_id)).filter(top.status == "skipped").label("skipped"),
            func.count(func.distinct(top_id)).filter(top.status == "failed").label("failed"),
            func.count(func.distinct(top_id)).filter(top.status == "needs_attention").label("needs_attention"),
        ]
        if sort == "runs":
            order = distinct_runs.desc()
        elif sort == "failed":
            order = func.count(func.distinct(top_id)).filter(top.status == "failed").desc()
        else:
            order = _usage_order(req, sort)
        stmt = (
            select(*cols)
            .select_from(_usage_source(q, req, rr, need_run=True))
            .join(
                top,
                and_(
                    top.tenant_id == req.tenant_id,
                    top.id == func.coalesce(rr.parent_run_id, rr.id),
                    top.created_at < q.end,  # a parent run exists before its child's requests
                ),
            )
            .where(*conds)
            # By position: a CASE key's bound parameters would not match between SELECT and GROUP BY.
            .group_by(literal_column("1"))
            .order_by(order, key_label)
            .limit(limit)
        )
        with self._session() as session:
            rows = session.execute(stmt).all()
        items = []
        for row in rows:
            m = row._mapping
            finished = m["completed"] + m["failed"] + m["needs_attention"]
            terminal = finished + m["skipped"]
            items.append({
                "key": m["key"],
                **_empty_runs(),
                "runs": m["runs"],
                "completed": m["completed"],
                "skipped": m["skipped"],
                "failed": m["failed"],
                "needs_attention": m["needs_attention"],
                "in_progress": m["runs"] - terminal,
                "blocked": None,
                "error_rate": _error_rate(m["failed"], finished),
                **_usage_dict(row),
            })
        return items


# runs.error of a run whose whole fallback chain failed: the worker's fixed RunFailure text
# (`FallbackExceptionGroup: ` + errors.FALLBACK_EXHAUSTED_MESSAGE; runs store no error code, only
# the run_failed event does). Pinned by worker_tests/test_ac17_fallback_exhausted.py.
FALLBACK_EXHAUSTED_RUN_ERROR = "FallbackExceptionGroup: every model of the agent's fallback chain failed"


def _event_run_filters(q: MetricsQuery, r: Any) -> list[Any]:
    """The agent and kind filters on a request's (or run's) run, as the usage side applies them to a request's
    run (a workflow step's child run counts as `workflow`)."""
    conds: list[Any] = []
    if q.agent_id is not None:
        conds.append(r.agent_id == q.agent_id)
    if q.kind is not None:
        conds.append(_top_kind_is(q, r))
    return conds


def _top_kind_is(q: MetricsQuery, r: Any) -> Any:
    """SQL: the top-level run of `r` (itself, or its parent) is of kind `q.kind`. A workflow's agent
    steps count as `workflow` and an eval run's case runs as `eval` (AC.17: before, every child run
    counted as `workflow`, eval cases included). The parent lookup is a primary-key probe."""
    parent = aliased(RunModel)
    via_parent = (
        select(literal(1))
        .where(parent.tenant_id == q.tenant_id, parent.id == r.parent_run_id, parent.kind == q.kind)
        .exists()
    )
    return or_(and_(r.parent_run_id.is_(None), r.kind == q.kind), and_(r.parent_run_id.is_not(None), via_parent))


def _fallback_dict(chat_requests: int, fell_back: int, billed_failed_attempts: int, exhausted_runs: int) -> dict[str, Any]:
    total = chat_requests + exhausted_runs
    return {
        "chat_requests": chat_requests,
        "fell_back": fell_back,
        "billed_failed_attempts": billed_failed_attempts,
        "exhausted_runs": exhausted_runs,
        "fallback_rate": None if total == 0 else (fell_back + exhausted_runs) / total,
    }


def _usage_order(req: Any, sort: str) -> Any:
    if sort == "requests":
        return func.count().desc()
    if sort == "tokens":
        return (func.coalesce(func.sum(req.input_tokens), 0) + func.coalesce(func.sum(req.output_tokens), 0)).desc()
    if sort == "cost":
        return func.coalesce(func.sum(req.cost_usd), 0).desc()
    raise MetricsRangeError("sort must be one of runs, failed, requests, tokens, cost")
