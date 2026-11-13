"""Exported span status descriptions keep only names the span itself recorded (task 3.5 review):
a library may put a server-controlled body (an MCP tool's error text) into the status."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import eap_observability as obs


@pytest.fixture
def finished(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs.shutdown_telemetry()
    exporter = InMemorySpanExporter()
    provider = obs.setup_telemetry("test-service", exporter=exporter)

    def get():
        provider.force_flush()
        return {s.name: s for s in exporter.get_finished_spans()}

    yield provider.get_tracer("t"), get
    obs.shutdown_telemetry()


@pytest.mark.parametrize("body", ["Abcd1234token", "Token: invalid", "sntl.secret.value", "RuntimeError"])
def test_free_text_status_becomes_error(finished, body):
    tracer, get = finished
    with tracer.start_as_current_span("tools/call x") as span:
        span.set_status(trace.Status(trace.StatusCode.ERROR, body))
    assert get()["tools/call x"].status.description == "error"


def test_recorded_exception_type_is_kept(finished):
    tracer, get = finished
    with pytest.raises(ValueError):
        with tracer.start_as_current_span("op"):
            raise ValueError("Abcd1234token")
    exported = get()["op"]
    assert exported.status.description == "ValueError"
    assert "Abcd1234token" not in repr(exported.status)


def test_run_outcome_code_is_kept(finished):
    tracer, get = finished
    with tracer.start_as_current_span("eap.run") as span:
        obs.set_run_outcome(span, "failed", "mcp_tool_unavailable")
    assert get()["eap.run"].status.description == "mcp_tool_unavailable"
