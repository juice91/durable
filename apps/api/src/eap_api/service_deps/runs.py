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
from eap_api.service_deps.iam import AccessDep
from eap_api.services.access import Access
from eap_api.services.logs import LogService
from eap_api.services.metrics import MetricsService
from eap_api.services.runs import RunService
from eap_api.services.spans import SpanService
from eap_api.services.traces import TraceService
from eap_run_store import EventLog, RunStore


def build_run_service(
    db: Session, run_store: RunStore, event_log: EventLog, launcher: RunLauncher, access: Access | None = None
) -> RunService:
    """A RunService without a Request (MCP control builds its collaborators from the app)."""
    return RunService(db, run_store, event_log, launcher, access)


def get_run_service(db: SessionDep, stores: StoresDep, launcher: RunLauncherDep, access: AccessDep) -> RunService:
    return build_run_service(db, stores[0], stores[1], launcher, access)


RunServiceDep = Annotated[RunService, Depends(get_run_service)]


def get_span_service(spans: SpanStoreDep, stores: StoresDep, access: AccessDep) -> SpanService:
    return SpanService(spans, stores[0], access)


SpanServiceDep = Annotated[SpanService, Depends(get_span_service)]


def get_trace_service(
    db: SessionDep, stores: StoresDep, feedback: TraceFeedbackStoreDep, access: AccessDep
) -> TraceService:
    return TraceService(db, stores[0], feedback, access)


TraceServiceDep = Annotated[TraceService, Depends(get_trace_service)]


def get_metrics_service(
    store: MetricsStoreDep, db: SessionDep, launcher: RunLauncherDep, access: AccessDep
) -> MetricsService:
    return MetricsService(store, db, launcher, access)


MetricsServiceDep = Annotated[MetricsService, Depends(get_metrics_service)]


def get_log_service(stores: StoresDep, db: SessionDep, access: AccessDep) -> LogService:
    return LogService(stores[0], db, access)


LogServiceDep = Annotated[LogService, Depends(get_log_service)]
