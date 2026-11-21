"""Metrics: the summary, time series and breakdown of the tenant's runs and model requests.

Postgres aggregates (eap_run_store.metrics) for one tenant; this service validates the range, turns the store's
refusals into `ServiceError`s and shapes the JSON the routes answer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.ports import RunLauncher
from eap_api.services.errors import InvalidInput, Unavailable
from eap_run_store import METRICS, MetricsQuery, MetricsQueryTimeout, MetricsRangeError, MetricsStore
from eap_run_store.metrics import MAX_GROUPS, default_interval

DEFAULT_RANGE = timedelta(days=7)
TIMEOUT_DETAIL = "Metrics query timed out; narrow the time range or the filters"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
BEFORE_EPOCH = "from and to must not be before 1970-01-01"

MetricName = Literal[METRICS]  # type: ignore[valid-type]  # re-exported for the route's query type
__all__ = ["METRICS", "MetricName", "MetricsQuery", "MetricsService", "resolve_range"]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
        raise InvalidInput(exc.message) from None
    except MetricsQueryTimeout:
        raise Unavailable(TIMEOUT_DETAIL) from None


class MetricsService:
    def __init__(self, store: MetricsStore, db: Session, launcher: RunLauncher) -> None:
        self.store = store
        self.db = db
        self.launcher = launcher

    def build_query(
        self,
        tenant_id: str,
        from_: datetime | None,
        to: datetime | None,
        *,
        agent_id: str | None,
        model: str | None,
        provider: str | None,
        kind: str | None,
        operation: str | None,
    ) -> MetricsQuery:
        try:
            start, end = resolve_range(from_, to)
            return MetricsQuery(
                tenant_id=tenant_id, start=start, end=end, agent_id=agent_id, model=model, provider=provider,
                kind=kind, operation=operation,
            )
        except MetricsRangeError as exc:
            raise InvalidInput(exc.message) from None

    def _recovered_runs(self, q: MetricsQuery) -> int:
        lookup = getattr(self.launcher, "recovered_run_ids", None)
        ids = lookup(since=q.start, until=q.end) if lookup is not None else []
        return _call(self.store.count_recovered_runs, q, ids) if ids else 0

    def summary(self, q: MetricsQuery, compare: str | None) -> dict[str, Any]:
        """Totals for `[from, to)` and, with `compare=previous`, for the period of equal length just
    before it (`previous_from`, `previous_to`; `previous` is null without `compare`). Each period
    also has `operations` (RM.19): the usage metrics (requests, tokens, cost_usd, unpriced and
    unmeasured requests) of each operation (`chat`, `moderation`, `observer`, `reflector`,
    `embedding`, `judge`; they sum to the totals) and of `memory`. AC.17 (review 2026-11-15): `fallback`
    is `{chat_requests, fell_back, billed_failed_attempts, exhausted_runs, fallback_rate}`, the share
    of agent chat requests not answered by the primary model, exhausted runs counted (see
    eap_run_store.metrics; `fallback_rate` null without agent chat requests or exhausted runs).
    Each period also has `recovered_runs`: the tenant's top-level runs that DBOS resumed at least once
    (`recovery_attempts > 1`, read from the DBOS system database) whose last update falls in the period."""
        result = _call(self.store.summary, q, compare_previous=compare == "previous")
        prev = q.previous()
        result["current"]["recovered_runs"] = self._recovered_runs(q)
        if result["previous"] is not None:
            result["previous"]["recovered_runs"] = self._recovered_runs(prev)
        return {
            "from": _iso(q.start),
            "to": _iso(q.end),
            "filters": _filters_json(q),
            "current": _metrics_json(result["current"]),
            "previous": _metrics_json(result["previous"]) if result["previous"] is not None else None,
            "previous_from": _iso(prev.start) if compare else None,
            "previous_to": _iso(prev.end) if compare else None,
        }

    def timeseries(self, q: MetricsQuery, interval: str | None, metric: list[str] | None) -> dict[str, Any]:
        chosen = interval or default_interval(q.start, q.end)
        buckets = _call(self.store.timeseries, q, chosen)
        only = list(dict.fromkeys(metric)) if metric else None
        return {
            "from": _iso(q.start),
            "to": _iso(q.end),
            "interval": chosen,
            "filters": _filters_json(q),
            "buckets": [{"start": _iso(b["start"]), **_metrics_json(b, only)} for b in buckets],
        }

    def breakdown(self, q: MetricsQuery, by: str, sort: str, limit: int, compare: str | None) -> dict[str, Any]:
        items = _call(self.store.breakdown, q, by, sort=sort, limit=limit)
        previous: dict[str, dict[str, Any]] = {}
        if compare == "previous" and items:
            wanted = {i["key"] for i in items}
            before = _call(self.store.breakdown, q.previous(), by, sort=sort, limit=MAX_GROUPS)
            previous = {i["key"]: i for i in before if i["key"] in wanted}
        labels: dict[str, str] = {}
        if by == "agent" and items:
            rows = (
                self.db.query(AgentModel.id, AgentModel.name)
                .filter(AgentModel.tenant_id == q.tenant_id, AgentModel.id.in_([i["key"] for i in items]))
                .all()
            )
            labels = {str(row.id): row.name for row in rows}
        return {
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
        }
