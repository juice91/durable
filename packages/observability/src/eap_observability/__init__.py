"""EAP observability: the only package that imports OpenTelemetry (enforced by lint-imports).

One trace per run: the API request span's W3C traceparent is stored on the run row; the worker
reads it back and opens the `eap.run` span as its child, so the API request, the DBOS workflow, each
model request and each tool call share one trace id. Python logs go to the same OTLP endpoint and
carry the active trace and span ids.

Everything is a no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set (or a test passes an exporter):
`setup_telemetry` returns None, `setup_logging` returns False, nothing global is touched, and
`run_span` yields a non-recording span.

Nothing exported carries exception text, prompts or credentials: both pipelines pass through the
redacting processor/exporter in `eap_observability.redaction`, and handled failures are logged with
`log_failure` (error code + exception type + ids). See that module.

Pydantic AI instrumentation lives in `eap_observability.agents`, so importing this module never
imports pydantic_ai (the control plane must not).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from eap_observability.logcontext import (
    RUN_LOG_ATTRIBUTE,
    TENANT_LOG_ATTRIBUTE,
    LogContextProcessor,
    bind_log_context,
    current_log_context,
    log_context,
    reset_log_context,
)
from eap_observability.http import install_access_logging, server_request_hook
from eap_observability.redaction import (
    LOCAL_ONLY_LOG_ATTRIBUTE,
    RedactingLogProcessor,
    RedactingSpanExporter,
    exception_type,
    log_failure,
    redact,
)

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
# DBOS 3.5 reads a W3C carrier from this workflow attribute and parents the workflow span on it
# (dbos._context.OTEL_CARRIER_ATTRIBUTE; durable, so recovery lands on the same trace).
DBOS_OTEL_CARRIER_ATTRIBUTE = "dbos.otelContext"

_propagator = TraceContextTextMapPropagator()


@dataclass
class _State:
    tracer_provider: TracerProvider | None = None
    logger_provider: LoggerProvider | None = None
    log_handler: logging.Handler | None = None


_state = _State()


def otlp_endpoint_from_env() -> str | None:
    """OTEL_EXPORTER_OTLP_ENDPOINT (the OTLP/HTTP base URL), or None when unset or blank."""
    value = os.environ.get(ENDPOINT_ENV, "").strip()
    return value or None


def _signal_url(base: str, signal: str) -> str:
    base = base.rstrip("/")
    suffix = f"/v1/{signal}"
    return base if base.endswith(suffix) else base + suffix


def setup_telemetry(
    service_name: str, *, otlp_endpoint: str | None = None, exporter: SpanExporter | None = None
) -> TracerProvider | None:
    """Install the process's tracer provider (service.name = `service_name`) exporting over OTLP/HTTP.

    The endpoint defaults to OTEL_EXPORTER_OTLP_ENDPOINT; `exporter` replaces the OTLP exporter
    (tests). Returns None and changes nothing when there is neither. Idempotent: a second call
    returns the provider already installed.
    """
    if _state.tracer_provider is not None:
        return _state.tracer_provider
    endpoint = otlp_endpoint or otlp_endpoint_from_env()
    if exporter is None and endpoint is None:
        return None
    if exporter is None:
        assert endpoint is not None
        exporter = OTLPSpanExporter(endpoint=_signal_url(endpoint, "traces"))
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(RedactingSpanExporter(exporter)))
    # Global, so DBOS (which uses trace.get_tracer) exports through this provider too. OpenTelemetry
    # allows setting it once per process; later calls (tests) keep using the explicit provider.
    trace.set_tracer_provider(provider)
    _state.tracer_provider = provider
    return provider


def tracer_provider() -> TracerProvider | None:
    """The provider installed by setup_telemetry, or None when telemetry is off."""
    return _state.tracer_provider


def setup_logging(
    service_name: str,
    *,
    otlp_endpoint: str | None = None,
    exporter: LogRecordExporter | None = None,
    level: int = logging.INFO,
) -> bool:
    """Export Python logging records over OTLP (to Loki) with the active trace and span ids.

    Adds one OpenTelemetry handler to the root logger and makes sure INFO records reach it. Returns
    False and changes nothing when there is no endpoint (OTEL_EXPORTER_OTLP_ENDPOINT) and no
    `exporter`. Idempotent.
    """
    if _state.logger_provider is not None:
        return True
    endpoint = otlp_endpoint or otlp_endpoint_from_env()
    if exporter is None and endpoint is None:
        return False
    if exporter is None:
        assert endpoint is not None
        exporter = OTLPLogExporter(endpoint=_signal_url(endpoint, "logs"))
    provider = LoggerProvider(resource=Resource.create({"service.name": service_name}))
    # Every record on this provider (ours, and other libraries' handlers such as DBOS's) gets the
    # current tenant and run ids (eap.tenant_id, eap.run_id; see log_context) and is cleaned before
    # export: no exception text, tracebacks, credentials or local-only records.
    provider.add_log_record_processor(LogContextProcessor(RedactingLogProcessor(BatchLogRecordProcessor(exporter))))
    # Global, so DBOS's own log handler (enable_otlp) exports through it instead of warning.
    set_logger_provider(provider)
    handler = LoggingHandler(level=level, logger_provider=provider)
    root = logging.getLogger()
    if not root.handlers:
        # Adding a handler disables logging's last-resort stderr output; keep warnings visible.
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        root.addHandler(console)
    root.addHandler(handler)
    if root.getEffectiveLevel() > level:
        root.setLevel(level)
    _state.logger_provider = provider
    _state.log_handler = handler
    return True


def shutdown_telemetry() -> None:
    """Flush and drop what setup_telemetry/setup_logging installed (process exit, tests)."""
    if _state.log_handler is not None:
        logging.getLogger().removeHandler(_state.log_handler)
    if _state.logger_provider is not None:
        _state.logger_provider.shutdown()
    if _state.tracer_provider is not None:
        _state.tracer_provider.shutdown()
    _state.tracer_provider = None
    _state.logger_provider = None
    _state.log_handler = None


def instrument_fastapi(app: Any, tracer_provider: TracerProvider | None = None) -> None:
    """Server span per request. No-op when telemetry is off or the app is already instrumented."""
    provider = tracer_provider or _state.tracer_provider
    if provider is None or getattr(app, "_is_instrumented_by_opentelemetry", False):
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # url.path / http.target become the route template (no raw resource ids; P1.C2).
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, server_request_hook=server_request_hook)


def dbos_otel_config(otlp_endpoint: str | None) -> dict[str, Any]:
    """DBOSConfig keys for DBOS's own workflow and step spans: {} when there is no endpoint.

    Only `enable_otlp`: no DBOS endpoints, so DBOS exports through the global providers installed
    by setup_telemetry/setup_logging (call those first) instead of adding a second exporter.
    """
    if not otlp_endpoint:
        return {}
    return {"enable_otlp": True}


def dbos_trace_attributes(traceparent: str | None) -> dict[str, Any] | None:
    """Workflow attributes that make DBOS parent the workflow span on `traceparent`, or None."""
    if not traceparent:
        return None
    return {DBOS_OTEL_CARRIER_ATTRIBUTE: {"traceparent": traceparent}}


def current_trace_id() -> str | None:
    """The active span's 32-hex trace id, or None outside a (recording or propagated) span."""
    return current_trace_context()[0]


def current_trace_context() -> tuple[str | None, str | None]:
    """(trace_id, W3C traceparent) of the active span, or (None, None) when there is none."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    trace_id = format(ctx.trace_id, "032x")
    return trace_id, f"00-{trace_id}-{format(ctx.span_id, '016x')}-{int(ctx.trace_flags):02x}"


@contextmanager
def run_span(
    run_id: str,
    tenant_id: str,
    agent_id: str,
    version: int | str,
    *,
    traceparent: str | None = None,
    tracer_provider: TracerProvider | None = None,
) -> Iterator[Span]:
    """The `eap.run` span, current for the block, with run/tenant/agent/version attributes.

    With `traceparent` (the API request span stored on the run) it is that span's child, so the
    run joins the request's trace; an unreadable traceparent falls back to the current context.
    Non-recording when telemetry is off. Log records emitted inside the block carry the run's
    `eap.tenant_id` and `eap.run_id` (log_context), whether or not telemetry is on.
    """
    provider = tracer_provider or _state.tracer_provider
    tracer = provider.get_tracer("eap_observability") if provider is not None else trace.NoOpTracer()
    parent = None
    if traceparent:
        extracted = _propagator.extract({"traceparent": traceparent})
        if trace.get_current_span(extracted).get_span_context().is_valid:
            parent = extracted
    attributes = {
        "eap.run_id": str(run_id),
        "eap.tenant_id": tenant_id,
        "eap.agent_id": str(agent_id),
        "eap.agent_version": version,
    }
    with (
        log_context(tenant_id=tenant_id, run_id=str(run_id)),
        tracer.start_as_current_span("eap.run", context=parent, attributes=attributes) as span,
    ):
        yield span


def set_run_outcome(span: Span, status: str, error_code: str | None = None) -> None:
    """Record the run's terminal status on its span; anything but `completed` marks it an error.

    `error_code` is a stable code (never exception text); it becomes `eap.error_code` and the status
    description."""
    span.set_attribute("eap.run_status", status)
    if error_code:
        span.set_attribute("eap.error_code", error_code)
    if status == "completed":
        span.set_status(Status(StatusCode.OK))
    else:
        span.set_status(Status(StatusCode.ERROR, error_code or status))


__all__ = [
    "DBOS_OTEL_CARRIER_ATTRIBUTE",
    "RUN_LOG_ATTRIBUTE",
    "TENANT_LOG_ATTRIBUTE",
    "LogContextProcessor",
    "bind_log_context",
    "current_log_context",
    "log_context",
    "reset_log_context",
    "LOCAL_ONLY_LOG_ATTRIBUTE",
    "exception_type",
    "log_failure",
    "redact",
    "current_trace_context",
    "current_trace_id",
    "dbos_otel_config",
    "dbos_trace_attributes",
    "instrument_fastapi",
    "install_access_logging",
    "otlp_endpoint_from_env",
    "run_span",
    "set_run_outcome",
    "setup_logging",
    "setup_telemetry",
    "shutdown_telemetry",
    "tracer_provider",
]
