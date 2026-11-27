"""Span-store reads: the runs histogram, the flat span list and the external (OTLP) traces."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.run_filters import plain_refusals
from eap_api.services.errors import InvalidInput, NotFound, Unavailable
from eap_api.services.run_filters import (
    BAD_CURSOR,
    FROM_AFTER_TO,
    RUN_LIST_TIMEOUT_DETAIL,
    list_param,
    metadata_filter,
    status_filter,
    text_query,
    time_window,
    to_utc,
)
from eap_run_store import MetricsQueryTimeout, RunStore
from eap_run_store.spans import InvalidSpanCursor, SpanCursor, SpanStore
from eap_run_store.trace import SPAN_KINDS  # noqa: F401 - re-exported for the routes' query types

HISTOGRAM_DEFAULT_RANGE = timedelta(days=7)
HISTOGRAM_HOURLY_UNDER = timedelta(hours=72)
HISTOGRAM_MAX_RANGE = timedelta(days=366)
RUN_ID_ATTRIBUTE = "pydanticos.run_id"
TRACE_NOT_FOUND = "Trace not found"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _cursor(before: str | None) -> SpanCursor | None:
    if before is None:
        return None
    try:
        return SpanCursor.decode(before)
    except InvalidSpanCursor:
        raise InvalidInput(BAD_CURSOR) from None


@plain_refusals
class SpanService(AccessBound):
    """Spans, the runs histogram and external traces. Spans and the histogram are limited to the runs of the agents and
    workflows the caller may read (two engine calls, whatever the page size); external (OTLP) traces are tenant data:
    `read` on the tenant."""

    def __init__(self, spans: SpanStore, run_store: RunStore, access: Access | None = None) -> None:
        self.spans = spans
        self.run_store = run_store
        self._access = access  # None: no caller to check

    def _visible(self, agent_id: uuid.UUID | None, workflow_id: uuid.UUID | None) -> dict[str, Any]:
        """The per-resource restriction as filters; an agent or workflow the caller names must be readable."""
        if agent_id is not None:
            acl.require(self._access, "read", "agent", str(agent_id))
        if workflow_id is not None:
            acl.require(self._access, "read", "workflow", str(workflow_id))
        return {
            "visible_agent_ids": acl.sql_ids(acl.visible(self._access, "agent")),
            "visible_workflow_ids": acl.sql_ids(acl.visible(self._access, "workflow")),
        }

    def runs_histogram(
        self,
        tenant_id: str,
        *,
        agent_id: uuid.UUID | None,
        run_status: list[str] | None,
        kind: str | None,
        tool_id: str | None,
        workflow_id: uuid.UUID | None,
        knowledge_base_id: uuid.UUID | None,
        parent_run_id: uuid.UUID | None,
        created_from: datetime | None,
        created_to: datetime | None,
        model: str | None,
        provider: str | None,
        tags: list[str] | None,
        metadata: list[str] | None,
        q: str | None,
    ) -> dict[str, Any]:
        """Runs per bucket over the `GET /runs` filters, zero-filled (see GET /runs/histogram)."""
        now = datetime.now(UTC)
        end = to_utc(created_to) if created_to is not None else now
        start = to_utc(created_from) if created_from is not None else end - HISTOGRAM_DEFAULT_RANGE
        if start >= end:
            raise InvalidInput(FROM_AFTER_TO)
        if end - start > HISTOGRAM_MAX_RANGE:
            raise InvalidInput("the range is at most 366 days")
        interval = "hour" if end - start < HISTOGRAM_HOURLY_UNDER else "day"
        filters = {
            "agent_id": agent_id, "status": status_filter(run_status), "kind": kind, "tool_id": tool_id,
            "workflow_id": workflow_id, "knowledge_base_id": knowledge_base_id, "parent_run_id": parent_run_id,
            "model": model, "provider": provider, "tags": list_param(tags, "tags", split=True),
            "metadata": metadata_filter(metadata), "text_query": text_query(q),
            **self._visible(agent_id, workflow_id),
        }
        try:
            buckets = self.spans.runs_histogram(
                tenant_id, start, end, interval, {k: v for k, v in filters.items() if v is not None}
            )
        except MetricsQueryTimeout:
            raise Unavailable(RUN_LIST_TIMEOUT_DETAIL) from None
        return {"interval": interval, "buckets": [{**b, "start": b["start"].isoformat()} for b in buckets]}

    def list_spans(
        self,
        tenant_id: str,
        *,
        limit: int,
        before: str | None,
        span_kind: str | None,
        source: str | None,
        agent_id: uuid.UUID | None,
        run_status: list[str] | None,
        kind: str | None,
        tool_id: str | None,
        workflow_id: uuid.UUID | None,
        created_from: datetime | None,
        created_to: datetime | None,
        model: str | None,
        provider: str | None,
        tags: list[str] | None,
        metadata: list[str] | None,
        q: str | None,
    ) -> dict[str, Any]:
        """The flat span list, newest first, as `{items, next_before}` (see GET /spans)."""
        start, end = time_window(created_from, created_to)
        cursor = _cursor(before)
        filters = {
            "from": start, "to": end, "span_kind": span_kind, "source": source, "q": text_query(q),
            "agent_id": agent_id, "status": status_filter(run_status), "kind": kind, "tool_id": tool_id,
            "workflow_id": workflow_id, "model": model, "provider": provider,
            "tags": list_param(tags, "tags", split=True), "metadata": metadata_filter(metadata),
            **self._visible(agent_id, workflow_id),
        }
        try:
            items, next_before = self.spans.list(
                tenant_id, {k: v for k, v in filters.items() if v is not None}, limit, cursor
            )
        except MetricsQueryTimeout:
            raise Unavailable(RUN_LIST_TIMEOUT_DETAIL) from None
        return {
            "items": [{**i, "run_id": str(i["run_id"]), "start": _iso(i["start"]), "end": _iso(i["end"])} for i in items],
            "next_before": next_before,
        }

    def list_external_traces(
        self,
        tenant_id: str,
        *,
        limit: int,
        before: str | None,
        service: str | None,
        q: str | None,
        trace_status: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> dict[str, Any]:
        """The tenant's external traces (sent over OTLP), newest first (see GET /traces)."""
        acl.require(self._access, "read", "tenant", None)
        start, end = time_window(created_from, created_to)
        cursor = _cursor(before)
        filters = {"service": service, "q": text_query(q), "status": trace_status, "from": start, "to": end}
        items, next_before = self.spans.list_traces(
            tenant_id, {k: v for k, v in filters.items() if v is not None}, limit, cursor
        )
        return {
            "items": [
                {"trace_id": i["trace_id"], "source": "external", "name": i["name"], "service_name": i["service_name"],
                 "start": _iso(i["start"]), "end": _iso(i["end"]), "span_count": i["span_count"], "status": i["status"]}
                for i in items
            ],
            "next_before": next_before,
        }

    def read_external_trace(self, tenant_id: str, trace_id: str) -> dict[str, Any]:
        """One external trace with the platform run its spans name (when that run is the tenant's)."""
        acl.require(self._access, "read", "tenant", None)
        trace = self.spans.read_external_trace(tenant_id, trace_id)
        if trace is None:
            raise NotFound(TRACE_NOT_FOUND)
        run_id = None
        for span in trace["spans"]:
            candidate = span["attributes"].get(RUN_ID_ATTRIBUTE)
            try:
                parsed = uuid.UUID(candidate) if isinstance(candidate, str) else None
            except ValueError:
                parsed = None
            if parsed is not None and self.run_store.get_run(tenant_id, parsed) is not None:
                run_id = str(parsed)
                break
        return {
            "trace_id": trace["trace_id"], "source": "external", "service_name": trace["service_name"],
            "resource_attributes": trace["resource_attributes"],
            "spans": [{**{k: v for k, v in s.items() if k != "service_name"}, "start": _iso(s["start"]), "end": _iso(s["end"])}
                      for s in trace["spans"]],
            "run_id": run_id, "truncated": trace["truncated"],
        }
