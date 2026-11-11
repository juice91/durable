import os
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry import trace as trace_api
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from eap_observability import (
    setup_telemetry,
    instrument_pydantic_ai,
    instrument_fastapi,
    dbos_otel_config,
    current_trace_id,
    run_span,
)


@pytest.fixture
def custom_tracer_provider():
    """Create a custom tracer provider with in-memory exporter for each test."""
    # Reset the global provider to ensure each test gets its own provider
    import eap_observability
    eap_observability._global_provider = None
    
    exporter = InMemorySpanExporter()
    provider = setup_telemetry("test-service", exporter=exporter)
    return provider, exporter


def test_setup_telemetry_idempotent_returns_same_provider(custom_tracer_provider):
    """Calling setup_telemetry twice returns the same provider without replacing global."""
    provider1, exporter = custom_tracer_provider
    provider2 = setup_telemetry("test-service", exporter=exporter)
    
    assert provider1 is provider2


def test_no_endpoint_no_exporter_creates_noop_provider():
    """When no endpoint and no exporter, telemetry is a no-op provider with no spans exported."""
    # Reset global provider for this test
    import eap_observability
    eap_observability._global_provider = None
    
    exporter = InMemorySpanExporter()
    
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    
    # No endpoint and no exporter -> no-op provider
    provider = setup_telemetry("test-service")
    
    # The no-op provider has no span processors, so spans aren't exported
    # We verify by checking the provider has no span processors
    assert len(list(provider._active_span_processor._span_processors)) == 0
    
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("test-span"):
        pass
    
    # Exporter should have no spans because no span processor was attached
    spans = exporter.get_finished_spans()
    assert len(spans) == 0


def test_instrument_pydantic_ai_creates_spans(custom_tracer_provider):
    """After instrument_pydantic_ai, agent runs produce spans."""
    provider, exporter = custom_tracer_provider
    
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    instrument_pydantic_ai(tracer_provider=provider)
    
    agent = Agent(TestModel(), output_type=str)
    
    # Run with TestModel
    result = agent.run_sync("Hello")
    
    # Flush spans to ensure batch export completes
    provider.force_flush()
    
    spans = exporter.get_finished_spans()
    assert len(spans) > 0
    
    # Find spans related to agent run
    agent_spans = [s for s in spans if "agent" in s.name.lower()]
    assert len(agent_spans) > 0


def test_instrument_pydantic_ai_excludes_content_by_default(custom_tracer_provider):
    """By default, prompt content should not be in span attributes."""
    provider, exporter = custom_tracer_provider
    
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    instrument_pydantic_ai(tracer_provider=provider)
    
    agent = Agent(TestModel(), output_type=str)
    result = agent.run_sync("Hello with sensitive data")
    
    spans = exporter.get_finished_spans()
    # Find model request spans
    model_spans = [s for s in spans if "model" in s.name.lower()]
    
    for span in model_spans:
        # Prompt text should not be in attributes by default
        attributes = dict(span.attributes or {})
        # When include_content=False (default), we expect no input_messages or similar
        assert "llm.input_messages" not in attributes or attributes["llm.input_messages"] == "[]"


def test_instrument_fastapi_creates_server_span(custom_tracer_provider):
    """After instrument_fastapi, FastAPI requests produce server spans."""
    provider, exporter = custom_tracer_provider
    
    os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    
    app = FastAPI()
    
    @app.get("/test")
    def test_endpoint():
        return {"status": "ok"}
    
    instrument_fastapi(app, tracer_provider=provider)
    
    client = TestClient(app)
    response = client.get("/test")
    assert response.status_code == 200
    
    # Flush spans to ensure batch export completes
    provider.force_flush()
    
    spans = exporter.get_finished_spans()
    # Should have at least one HTTP span
    assert len(spans) >= 1
    # Verify we have HTTP-related spans
    http_spans = [s for s in spans if "http" in s.name.lower()]
    assert len(http_spans) >= 1


def test_run_span_sets_attributes_and_current_trace_id(custom_tracer_provider):
    """run_span sets the four attributes and current_trace_id returns the span's trace id."""
    provider, exporter = custom_tracer_provider
    
    with run_span(run_id="run-123", tenant_id="tenant-456", agent_id="agent-789", version="v1", tracer_provider=provider):
        trace_id = current_trace_id()
        assert trace_id is not None
        assert len(trace_id) == 32  # 32 hex characters
    
    # Flush spans to ensure batch export completes
    provider.force_flush()
    
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    
    span = spans[0]
    attributes = dict(span.attributes or {})
    assert attributes["eap.run_id"] == "run-123"
    assert attributes["eap.tenant_id"] == "tenant-456"
    assert attributes["eap.agent_id"] == "agent-789"
    assert attributes["eap.agent_version"] == "v1"


def test_dbos_otel_config_none_returns_empty_dict():
    """dbos_otel_config(None) should return empty dict."""
    result = dbos_otel_config(None)
    assert result == {}


def test_dbos_otel_config_with_endpoint():
    """dbos_otel_config with endpoint contains enable_otlp True."""
    result = dbos_otel_config("http://localhost:4317")
    assert result.get("enable_otlp") is True
    assert "otlp_traces_endpoints" in result
    assert result["otlp_traces_endpoints"] == "http://localhost:4317"
