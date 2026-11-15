"""LP.9: the `eap.loop_iteration` OTel span carries ids and counts only."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import eap_observability as obs

SECRET_STATE = "SECRET-LOOP-STATE"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs.shutdown_telemetry()
    yield
    obs.shutdown_telemetry()


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = obs.setup_telemetry("test-service", exporter=exporter)
    assert provider is not None

    def finished():
        provider.force_flush()
        return exporter.get_finished_spans()

    return provider, finished


def test_iteration_span_is_a_noop_without_telemetry():
    with obs.iteration_span("r", "t", "each", 0, scope="each.0") as span:
        assert not span.is_recording()


def test_iteration_span_carries_ids_and_counts_only(spans):
    _, finished = spans
    with obs.iteration_span("run-1", "tenant-1", "inner", 2, scope="outer.1/inner.2", loop_kind="loop") as span:
        obs.set_iteration_outcome(span, "completed", body_steps=3, child_runs=1)
    (span,) = finished()
    assert span.name == obs.ITERATION_SPAN_NAME == "eap.loop_iteration"
    assert dict(span.attributes) == {
        "eap.run_id": "run-1", "eap.tenant_id": "tenant-1", "eap.workflow_step_id": "inner",
        "eap.loop_kind": "loop", "eap.loop_scope": "outer.1/inner.2", "eap.loop_depth": 2,
        "eap.iteration_index": 2, "eap.iteration_status": "completed", "eap.body_steps": 3, "eap.child_runs": 1,
    }
    assert set(span.attributes) <= set(obs.ITERATION_SPAN_ATTRIBUTES)
    assert all(isinstance(v, (str, int)) for v in span.attributes.values())
    assert span.status.status_code == trace.StatusCode.OK


def test_failed_iteration_records_the_code_never_text(spans):
    _, finished = spans
    with obs.iteration_span("run-1", "t", "each", 0, scope="each.0", loop_kind="foreach") as span:
        obs.set_iteration_outcome(span, "failed", "agent_failed", body_steps=1)
    (span,) = finished()
    assert span.attributes["eap.loop_kind"] == "foreach"
    assert span.attributes["eap.error_code"] == "agent_failed"
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.status.description == "agent_failed"
    assert SECRET_STATE not in repr(span.to_json())


def test_iteration_span_is_a_child_of_the_traceparent(spans):
    provider, finished = spans
    with provider.get_tracer("t").start_as_current_span("eap.run"):
        trace_id, traceparent = obs.current_trace_context()
    with obs.iteration_span("run-1", "t", "each", 0, scope="each.0", traceparent=traceparent):
        assert obs.current_trace_id() == trace_id
    it = next(s for s in finished() if s.name == "eap.loop_iteration")
    assert format(it.context.trace_id, "032x") == trace_id


def test_iteration_span_refuses_non_count_values(spans):
    with pytest.raises(ValueError):
        with obs.iteration_span("r", "t", "each", -1):
            pass
    with obs.iteration_span("r", "t", "each", 0) as span:
        with pytest.raises(ValueError):
            obs.set_iteration_outcome(span, "completed", body_steps=True)  # type: ignore[arg-type]



@pytest.fixture
def raw_spans():
    """A provider WITHOUT the redacting exporter, so the span itself is what is checked."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter.get_finished_spans


def _no_text_anywhere(span, secret):
    assert list(span.events) == []
    assert all(secret not in str(v) for v in span.attributes.values())
    assert secret not in (span.status.description or "")
    assert secret not in span.to_json()


class _BodyError(RuntimeError):
    pass


def test_an_exception_leaving_the_span_records_only_its_class_name(raw_spans):
    provider, finished = raw_spans
    with pytest.raises(_BodyError):
        with obs.iteration_span("run-1", "t", "each", 0, scope="each.0", tracer_provider=provider):
            raise _BodyError(f"secret user prompt {SECRET_STATE}")
    (span,) = finished()
    _no_text_anywhere(span, SECRET_STATE)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.status.description == "_BodyError"
    assert set(span.attributes) <= set(obs.ITERATION_SPAN_ATTRIBUTES)


def test_an_exception_after_the_outcome_keeps_the_error_code(raw_spans):
    provider, finished = raw_spans
    with pytest.raises(_BodyError):
        with obs.iteration_span("run-1", "t", "each", 0, scope="each.0", tracer_provider=provider) as span:
            obs.set_iteration_outcome(span, "failed", "run_cancelled")
            raise _BodyError(SECRET_STATE)
    (span,) = finished()
    _no_text_anywhere(span, SECRET_STATE)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.status.description == "run_cancelled"


def test_a_base_exception_is_recorded_without_text(raw_spans):
    provider, finished = raw_spans
    with pytest.raises(KeyboardInterrupt):
        with obs.iteration_span("run-1", "t", "each", 0, tracer_provider=provider):
            raise KeyboardInterrupt(SECRET_STATE)
    (span,) = finished()
    _no_text_anywhere(span, SECRET_STATE)
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.status.description == "KeyboardInterrupt"


def test_an_exception_through_the_exporter_pipeline_carries_no_text(spans):
    _, finished = spans
    with pytest.raises(_BodyError):
        with obs.iteration_span("run-1", "t", "each", 0, scope="each.0"):
            raise _BodyError(SECRET_STATE)
    (span,) = finished()
    _no_text_anywhere(span, SECRET_STATE)
    assert span.status.status_code == trace.StatusCode.ERROR
