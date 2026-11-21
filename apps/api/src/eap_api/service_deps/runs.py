"""Providers of the runs and observability services (runs, spans, traces, metrics, logs)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from eap_api.deps import (
    MetricsStoreDep,
    RunLauncherDep,
    SessionDep,
    SpanStoreDep,
    StoresDep,
    TraceFeedbackStoreDep,
)
from eap_api.ports import RunLauncher
from eap_api.services.logs import LogService
from eap_api.services.metrics import MetricsService
from eap_api.services.runs import RunService
from eap_api.services.spans import SpanService
from eap_api.services.traces import TraceService
from eap_run_store import EventLog, RunStore


def build_run_service(db: Session, run_store: RunStore, event_log: EventLog, launcher: RunLauncher) -> RunService:
    """A RunService without a Request (MCP control builds its collaborators from the app)."""
    return RunService(db, run_store, event_log, launcher)


def get_run_service(db: SessionDep, stores: StoresDep, launcher: RunLauncherDep) -> RunService:
    return build_run_service(db, stores[0], stores[1], launcher)


RunServiceDep = Annotated[RunService, Depends(get_run_service)]


def get_span_service(spans: SpanStoreDep, stores: StoresDep) -> SpanService:
    return SpanService(spans, stores[0])


SpanServiceDep = Annotated[SpanService, Depends(get_span_service)]


def get_trace_service(db: SessionDep, stores: StoresDep, feedback: TraceFeedbackStoreDep) -> TraceService:
    return TraceService(db, stores[0], feedback)


TraceServiceDep = Annotated[TraceService, Depends(get_trace_service)]


def get_metrics_service(store: MetricsStoreDep, db: SessionDep, launcher: RunLauncherDep) -> MetricsService:
    return MetricsService(store, db, launcher)


MetricsServiceDep = Annotated[MetricsService, Depends(get_metrics_service)]


def get_log_service(stores: StoresDep, db: SessionDep) -> LogService:
    return LogService(stores[0], db)


LogServiceDep = Annotated[LogService, Depends(get_log_service)]
