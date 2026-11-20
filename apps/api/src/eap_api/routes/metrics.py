"""Metrics API (P0.6): `GET /metrics/summary`, `/metrics/timeseries`, `/metrics/breakdown`.

Postgres aggregates over `runs` and `run_model_requests` (eap_run_store.metrics), always for the
principal's tenant. Run counts and latency are over top-level runs created in `[from, to)`; tokens
and cost are over model requests made in `[from, to)` (so a workflow and its child runs are never
counted twice). See eap_run_store.metrics for the exact definitions of every filter and metric.

Common query parameters: `from`, `to` (ISO 8601; a value without an offset is UTC; default the last
7 days ending now), `agent_id`, `model` (our id `provider:name`), `provider`, `kind`
(`agent` | `tool` | `workflow` | `kb_ingest` | `eval`; AC.17 adds `eval`), `operation` (RM.19: `chat` |
`moderation` | `observer` | `reflector` | `embedding` | `judge` (AC.17: an eval run's LLM-judge
requests, `judge:` keys), a partition of the model requests, or `memory` = observer, reflector and
the semantic recall and index embeddings). Bounds: `from < to`, a range of at most 90 days, at most 400 time
buckets, at most 50 breakdown groups; all 422 with a fixed message. A query that exceeds the 5 s
statement timeout answers 503. `cost_usd` is a decimal string (same form as the run API); latency
is in milliseconds (float, null when no run in the period has finished); `error_rate` is
`failed / (completed + failed + needs_attention)` (skipped runs are counted apart and left out), null when no run has finished.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_run_store import METRICS, MetricsQuery, MetricsQueryTimeout, MetricsRangeError, MetricsStore
from eap_run_store.metrics import MAX_GROUPS, default_interval

DEFAULT_RANGE = timedelta(days=7)
TIMEOUT_DETAIL = "Metrics query timed out; narrow the time range or the filters"

PrincipalDep = Annotated[Principal, Depends(current_principal)]
SessionDep = Annotated[Session, Depends(get_session)]
MetricName = Literal[METRICS]  # type: ignore[valid-type]


def get_metrics_store(request: Request) -> MetricsStore:
    """The app's MetricsStore (created on first use; tests may set `app.state.metrics_store`)."""
    store = getattr(request.app.state, "metrics_store", None)
    if store is None:
        store = MetricsStore(request.app.state.database_engine)
        request.app.state.metrics_store = store
    return store


StoreDep = Annotated[MetricsStore, Depends(get_metrics_store)]

router = APIRouter(
    prefix="/metrics",
    tags=["metrics"],
    responses={422: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
BEFORE_EPOCH = "from and to must not be before 1970-01-01"


def _before_epoch(value: datetime | None) -> bool:
    """Whether `value` (naive = UTC) is before 1970-01-01; a value so early that converting it
    overflows counts as before."""
    if value is None:
        return False
    try:
        return _utc(value) < EPOCH
    except OverflowError:
        return True


def resolve_range(from_: datetime | None, to: datetime | None) -> tuple[datetime, datetime]:
    """`[from, to)` in UTC: `to` defaults to now, `from` to 7 days before `to`. Dates before 1970
    are rejected (MetricsRangeError) before any arithmetic, which could overflow near year 1."""
    if _before_epoch(from_) or _before_epoch(to):
        raise MetricsRangeError(BEFORE_EPOCH)
    end = _utc(to) if to is not None else datetime.now(UTC)
    start = _utc(from_) if from_ is not None else end - DEFAULT_RANGE
    return start, end


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def metrics_query(
    principal: PrincipalDep,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    agent_id: uuid.UUID | None = None,
    model: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    provider: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    kind: Literal["agent", "tool", "workflow", "kb_ingest", "eval", "kb_source_sync"] | None = None,
    operation: Literal["chat", "moderation", "observer", "reflector", "embedding", "judge", "memory"] | None = None,
) -> MetricsQuery:
    try:
        start, end = resolve_range(from_, to)
        return MetricsQuery(
            tenant_id=principal.tenant_id,
            start=start,
            end=end,
            agent_id=str(agent_id) if agent_id is not None else None,
            model=model,
            provider=provider,
            kind=kind,
            operation=operation,
        )
    except MetricsRangeError as exc:
        raise _unprocessable(exc.message)


QueryDep = Annotated[MetricsQuery, Depends(metrics_query)]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _metrics_json(values: dict[str, Any], only: list[str] | None = None) -> dict[str, Any]:
    out = {
        k: (format(v, "f") if k == "cost_usd" else {op: _metrics_json(u) for op, u in v.items()} if k == "operations" else v)
        for k, v in values.items()
        if k != "start" and k != "key"
    }
    if only:
        out = {k: v for k, v in out.items() if k in only}
    return out


def _filters_json(q: MetricsQuery) -> dict[str, Any]:
    return {"agent_id": q.agent_id, "model": q.model, "provider": q.provider, "kind": q.kind, "operation": q.operation}


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except MetricsRangeError as exc:
        raise _unprocessable(exc.message)
    except MetricsQueryTimeout:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=TIMEOUT_DETAIL)


def _recovered_runs(store: MetricsStore, launcher: RunLauncher, q: MetricsQuery) -> int:
    lookup = getattr(launcher, "recovered_run_ids", None)
    ids = lookup(since=q.start, until=q.end) if lookup is not None else []
    return _call(store.count_recovered_runs, q, ids) if ids else 0


@router.get("/summary")
def metrics_summary(
    q: QueryDep,
    store: StoreDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)],
    compare: Literal["previous"] | None = None,
) -> JSONResponse:
    """Totals for `[from, to)` and, with `compare=previous`, for the period of equal length just
    before it (`previous_from`, `previous_to`; `previous` is null without `compare`). Each period
    also has `operations` (RM.19): the usage metrics (requests, tokens, cost_usd, unpriced and
    unmeasured requests) of each operation (`chat`, `moderation`, `observer`, `reflector`,
    `embedding`, `judge`; they sum to the totals) and of `memory`. AC.17 (review 2026-11-15): `fallback`
    is `{chat_requests, fell_back, billed_failed_attempts, exhausted_runs, fallback_rate}`, the share
    of agent chat requests not answered by the primary model, exhausted runs counted (see
    eap_run_store.metrics; `fallback_rate` null without agent chat requests or exhausted runs).
    Each period also has `recovered_runs`: the tenant's top-level runs that DBOS resumed at least once
    (`recovery_attempts > 0`, read from the DBOS system database) whose last update falls in the period."""
    result = _call(store.summary, q, compare_previous=compare == "previous")
    prev = q.previous()
    result["current"]["recovered_runs"] = _recovered_runs(store, launcher, q)
    if result["previous"] is not None:
        result["previous"]["recovered_runs"] = _recovered_runs(store, launcher, prev)
    return JSONResponse(content={
        "from": _iso(q.start),
        "to": _iso(q.end),
        "filters": _filters_json(q),
        "current": _metrics_json(result["current"]),
        "previous": _metrics_json(result["previous"]) if result["previous"] is not None else None,
        "previous_from": _iso(prev.start) if compare else None,
        "previous_to": _iso(prev.end) if compare else None,
    })


@router.get("/timeseries")
def metrics_timeseries(
    q: QueryDep,
    store: StoreDep,
    interval: Literal["hour", "day"] | None = None,
    metric: Annotated[list[MetricName] | None, Query()] = None,  # type: ignore[valid-type]
) -> JSONResponse:
    """Zero-filled UTC buckets (`interval` hour or day; default hour up to 2 days, else day). Each
    bucket is `{"start": <ISO>, ...metrics}`; repeat `metric=` to return only those metrics. The
    first bucket starts at `from` truncated to the interval."""
    chosen = interval or default_interval(q.start, q.end)
    buckets = _call(store.timeseries, q, chosen)
    only = list(dict.fromkeys(metric)) if metric else None
    return JSONResponse(content={
        "from": _iso(q.start),
        "to": _iso(q.end),
        "interval": chosen,
        "filters": _filters_json(q),
        "buckets": [{"start": _iso(b["start"]), **_metrics_json(b, only)} for b in buckets],
    })


@router.get("/breakdown")
def metrics_breakdown(
    q: QueryDep,
    store: StoreDep,
    db: SessionDep,
    by: Literal["agent", "model", "operation"],
    sort: Literal["runs", "failed", "requests", "tokens", "cost"] = "runs",
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    compare: Literal["previous"] | None = None,
) -> JSONResponse:
    """The top `limit` agents, models or operations by `sort` (descending). Items are `{"key",
    "label", ...metrics}`: for an agent, `key` is its id and `label` its current name (null if it was
    deleted); for a model or an operation, both are the model id or the operation name. With
    `compare=previous` each item also has `previous`: the same metrics of that key over the period of
    equal length just before `[from, to)`, or null when the key had nothing then. The previous period
    is read as its own top-50 by `sort` (the store's cap), so with more than 50 groups a key that
    ranked below 50 then shows null, not its real previous values."""
    items = _call(store.breakdown, q, by, sort=sort, limit=limit)
    previous: dict[str, dict[str, Any]] = {}
    if compare == "previous" and items:
        wanted = {i["key"] for i in items}
        before = _call(store.breakdown, q.previous(), by, sort=sort, limit=MAX_GROUPS)
        previous = {i["key"]: i for i in before if i["key"] in wanted}
    labels: dict[str, str] = {}
    if by == "agent" and items:
        rows = (
            db.query(AgentModel.id, AgentModel.name)
            .filter(AgentModel.tenant_id == q.tenant_id, AgentModel.id.in_([i["key"] for i in items]))
            .all()
        )
        labels = {str(row.id): row.name for row in rows}
    return JSONResponse(content={
        "from": _iso(q.start),
        "to": _iso(q.end),
        "by": by,
        "sort": sort,
        "filters": _filters_json(q),
        "items": [
            {
                "key": i["key"], "label": labels.get(i["key"]) if by == "agent" else i["key"], **_metrics_json(i),
                **({"previous": _metrics_json(previous[i["key"]]) if i["key"] in previous else None} if compare else {}),
            }
            for i in items
        ],
    })
