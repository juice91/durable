"""Metrics API (P0.6): `GET /metrics/summary`, `/metrics/timeseries`, `/metrics/breakdown`.

Postgres aggregates over `runs` and `run_model_requests` (the run store's metrics module), always for the
principal's tenant. Run counts and latency are over top-level runs created in `[from, to)`; tokens
and cost are over model requests made in `[from, to)` (so a workflow and its child runs are never
counted twice). See the run store's metrics module for the exact definitions of every filter and metric.

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

import inspect
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.runs import MetricsServiceDep
from eap_api.services.metrics import MetricName, MetricsQuery, MetricsService

PrincipalDep = Annotated[Principal, Depends(current_principal)]

router = APIRouter(
    prefix="/metrics",
    tags=["metrics"],
    responses={422: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)


def metrics_query(
    principal: PrincipalDep,
    service: MetricsServiceDep,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    agent_id: uuid.UUID | None = None,
    model: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    provider: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    kind: Literal["agent", "tool", "workflow", "kb_ingest", "eval", "kb_source_sync"] | None = None,
    operation: Literal["chat", "moderation", "observer", "reflector", "embedding", "judge", "memory"] | None = None,
) -> MetricsQuery:
    return service.build_query(
        principal.tenant_id, from_, to, agent_id=str(agent_id) if agent_id is not None else None, model=model,
        provider=provider, kind=kind, operation=operation,
    )


QueryDep = Annotated[MetricsQuery, Depends(metrics_query)]


@router.get("/summary", description=inspect.cleandoc(MetricsService.summary.__doc__ or ""))
def metrics_summary(
    q: QueryDep,
    service: MetricsServiceDep,
    compare: Literal["previous"] | None = None,
) -> JSONResponse:
    return JSONResponse(content=service.summary(q, compare))


@router.get("/timeseries")
def metrics_timeseries(
    q: QueryDep,
    service: MetricsServiceDep,
    interval: Literal["hour", "day"] | None = None,
    metric: Annotated[list[MetricName] | None, Query()] = None,  # type: ignore[valid-type]
) -> JSONResponse:
    """Zero-filled UTC buckets (`interval` hour or day; default hour up to 2 days, else day). Each
    bucket is `{"start": <ISO>, ...metrics}`; repeat `metric=` to return only those metrics. The
    first bucket starts at `from` truncated to the interval."""
    return JSONResponse(content=service.timeseries(q, interval, metric))


@router.get("/breakdown")
def metrics_breakdown(
    q: QueryDep,
    service: MetricsServiceDep,
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
    return JSONResponse(content=service.breakdown(q, by, sort, limit, compare))
