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
from eap_observability.context import current_trace_context, current_trace_id  # noqa: F401 - re-exported
from eap_observability.http import install_access_logging, server_request_hook
from eap_observability.redaction import (
    LOCAL_ONLY_LOG_ATTRIBUTE,
    RedactingLogProcessor,
    RedactingSpanExporter,
    exception_type,
    log_failure,
    redact,
    redact_attribute,
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


def _tracer(tracer_provider: TracerProvider | None) -> trace.Tracer:
    provider = tracer_provider or _state.tracer_provider
    return provider.get_tracer("eap_observability") if provider is not None else trace.NoOpTracer()


def _parent_context(traceparent: str | None) -> Any:
    if not traceparent:
        return None
    extracted = _propagator.extract({"traceparent": traceparent})
    return extracted if trace.get_current_span(extracted).get_span_context().is_valid else None


def _require_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


ITERATION_SPAN_NAME = "eap.loop_iteration"
# The only attributes an iteration span ever carries: ids and counts, never the loop's item, state,
# conditions, step outputs or error text (loops LP.9).
ITERATION_SPAN_ATTRIBUTES = (
    "eap.run_id", "eap.tenant_id", "eap.workflow_step_id", "eap.loop_kind", "eap.loop_scope",
    "eap.loop_depth", "eap.iteration_index", "eap.iteration_status", "eap.error_code",
    "eap.body_steps", "eap.child_runs",
)


@contextmanager
def iteration_span(
    run_id: str,
    tenant_id: str,
    loop_id: str,
    index: int,
    *,
    scope: str = "",
    loop_kind: str = "loop",
    traceparent: str | None = None,
    tracer_provider: TracerProvider | None = None,
) -> Iterator[Span]:
    """The `eap.loop_iteration` span of iteration `index` of loop step `loop_id`, current for the
    block (loops LP.9). `scope` is the iteration's scope string (`each.3`, `outer.2/inner.0`,
    eap_schemas.workflow_scope.scope_string) and `loop_kind` is `loop` or `foreach`.

    Ids and counts only (ITERATION_SPAN_ATTRIBUTES): never the item, state, condition values,
    outputs or error text. With `traceparent` (the run's) it is that span's child, else the current
    context's (the DBOS iteration workflow's span). Non-recording when telemetry is off. Log
    records inside the block carry the run's ids (log_context).
    """
    attributes = {
        "eap.run_id": str(run_id),
        "eap.tenant_id": tenant_id,
        "eap.workflow_step_id": str(loop_id),
        "eap.loop_kind": "foreach" if loop_kind == "foreach" else "loop",
        "eap.loop_scope": str(scope),
        "eap.loop_depth": str(scope).count("/") + 1 if scope else 1,
        "eap.iteration_index": _require_int("index", index),
    }
    with (
        log_context(tenant_id=tenant_id, run_id=str(run_id)),
        _tracer(tracer_provider).start_as_current_span(
            ITERATION_SPAN_NAME,
            context=_parent_context(traceparent),
            attributes=attributes,
            # The SDK defaults would add an `exception` event (message and stack trace) and set
            # the status description to "<Type>: <message>": error text. Keep ids and codes only.
            record_exception=False,
            set_status_on_exception=False,
        ) as span,
    ):
        try:
            yield span
        except BaseException as exc:
            _mark_span_raised(span, exc)
            raise


def _mark_span_raised(span: Span, exc: BaseException) -> None:
    """An exception left the span's block: mark it an error with the exception's class name only
    (never its message), keeping an error code set_iteration_outcome already recorded."""
    status = getattr(span, "status", None)
    if status is not None and status.status_code == StatusCode.ERROR:
        return
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


def set_iteration_outcome(
    span: Span,
    status: str,
    error_code: str | None = None,
    *,
    body_steps: int | None = None,
    child_runs: int | None = None,
) -> None:
    """Record an iteration's status (`completed`, `failed`, `needs_attention`), its stable error
    code (never text) and how many body steps and child agent runs it executed. Anything but
    `completed` marks the span an error."""
    span.set_attribute("eap.iteration_status", status)
    if error_code:
        span.set_attribute("eap.error_code", error_code)
    if body_steps is not None:
        span.set_attribute("eap.body_steps", _require_int("body_steps", body_steps))
    if child_runs is not None:
        span.set_attribute("eap.child_runs", _require_int("child_runs", child_runs))
    if status == "completed":
        span.set_status(Status(StatusCode.OK))
    else:
        span.set_status(Status(StatusCode.ERROR, error_code or status))


def set_run_outcome(span: Span, status: str, error_code: str | None = None) -> None:
    """Record the run's terminal status on its span; anything but `completed` marks it an error.

    `error_code` is a stable code (never exception text); it becomes `eap.error_code` and the status
    description. A `skipped` run (a "nothing to do" exit) is a success too."""
    span.set_attribute("eap.run_status", status)
    if error_code:
        span.set_attribute("eap.error_code", error_code)
    if status in ("completed", "skipped"):
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
    "redact_attribute",
    "current_trace_context",
    "current_trace_id",
    "dbos_otel_config",
    "dbos_trace_attributes",
    "install_access_logging",
    "instrument_fastapi",
    "ITERATION_SPAN_ATTRIBUTES",
    "ITERATION_SPAN_NAME",
    "iteration_span",
    "set_iteration_outcome",
    "otlp_endpoint_from_env",
    "run_span",
    "set_run_outcome",
    "setup_logging",
    "setup_telemetry",
    "shutdown_telemetry",
    "tracer_provider",
]
