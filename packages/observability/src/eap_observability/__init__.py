"""
EAP Observability package - OpenTelemetry integration.
"""

import os
from contextlib import contextmanager
from typing import Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource


# Module-level reference to the first provider set (for idempotent setup)
_global_provider: Optional[TracerProvider] = None


def setup_telemetry(
    service_name: str, *, otlp_endpoint: str | None = None, exporter=None
) -> TracerProvider:
    """
    Set up telemetry with the given service name.
    
    If neither endpoint nor exporter is provided, uses a no-op provider.
    
    This function is idempotent: the first call sets the global provider,
    subsequent calls return the same provider without modification.
    
    Args:
        service_name: Name of the service for the resource.
        otlp_endpoint: OTLP endpoint for the exporter (if exporter not provided).
        exporter: Custom exporter to use.
    
    Returns:
        The TracerProvider that was set (or already set).
    """
    global _global_provider
    
    # If already set, return the existing provider
    if _global_provider is not None:
        return _global_provider
    
    # Set PYDANTIC_AI_NO_BANNER once
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    
    if otlp_endpoint is None and exporter is None:
        otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    
    if otlp_endpoint is None and exporter is None:
        # No-op provider - no telemetry exported
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        _global_provider = provider
        return provider
    
    if exporter is None:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
    
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    # Use BatchSpanProcessor for async export (not blocking request path)
    processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)
    
    trace.set_tracer_provider(provider)
    _global_provider = provider
    return provider


def instrument_pydantic_ai(
    include_content: bool = False, tracer_provider: TracerProvider | None = None
) -> None:
    """
    Instrument Pydantic AI agents to create spans.
    
    Safe to call multiple times.
    
    Args:
        include_content: Whether to include prompts and completions in spans.
        tracer_provider: Optional tracer provider to use. If not provided,
            the global provider is used.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings
    
    settings = InstrumentationSettings(
        tracer_provider=tracer_provider,
        include_content=include_content,
    )
    
    Agent.instrument_all(settings)


def instrument_fastapi(
    app, tracer_provider: TracerProvider | None = None
) -> None:
    """
    Instrument a FastAPI app to create server spans.
    
    Safe to call multiple times.
    
    Args:
        app: The FastAPI app to instrument.
        tracer_provider: Optional tracer provider to use. If not provided,
            the global provider is used.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    
    if not getattr(app, "_is_instrumented_by_opentelemetry", False):
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=tracer_provider
        )


def dbos_otel_config(otlp_endpoint: str | None) -> dict:
    """
    Get DBOSConfig keys for OTLP configuration.
    
    Returns empty dict when no endpoint is provided.
    
    Args:
        otlp_endpoint: OTLP endpoint URL.
    
    Returns:
        Dict with DBOS OTLP config keys, or empty dict if no endpoint.
    """
    if otlp_endpoint is None:
        return {}
    
    return {
        "enable_otlp": True,
        "otlp_traces_endpoints": otlp_endpoint,
    }


def current_trace_id() -> str | None:
    """
    Get the current trace ID from the active span.
    
    Returns:
        32-hex trace ID if there is an active span, None otherwise.
    """
    span = trace.get_current_span()
    span_context = span.get_span_context()
    
    if span_context.is_valid:
        return format(span_context.trace_id, "032x")
    
    return None


@contextmanager
def run_span(
    run_id: str,
    tenant_id: str,
    agent_id: str,
    version: str,
    tracer_provider: TracerProvider | None = None,
):
    """
    Context manager for a run span.
    
    Sets the four attributes: eap.run_id, eap.tenant_id, eap.agent_id, eap.agent_version.
    
    Args:
        run_id: The run identifier.
        tenant_id: The tenant identifier.
        agent_id: The agent identifier.
        version: The agent version.
        tracer_provider: Optional tracer provider to use. If not provided,
            the global provider is used.
    
    Yields:
        The active span.
    """
    if tracer_provider is None:
        tracer_provider = trace.get_tracer_provider()
    
    tracer = tracer_provider.get_tracer("eap_observability")
    
    with tracer.start_as_current_span("eap.run") as span:
        span.set_attribute("eap.run_id", run_id)
        span.set_attribute("eap.tenant_id", tenant_id)
        span.set_attribute("eap.agent_id", agent_id)
        span.set_attribute("eap.agent_version", version)
        
        yield span
