"""Milestone 2, task 4 (worker half): the run executes inside `eap.run`, a child of the API request
span whose traceparent is stored on the run, so model requests and tool calls share its trace; log
records written during the run carry the trace id.

The session's in-process worker was launched with telemetry off; this module switches it on with
in-memory exporters (the workflow resolves the provider at run time) and off again afterwards.
"""

from __future__ import annotations

import uuid

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import eap_observability as obs
from eap_observability.agents import instrument_pydantic_ai, uninstrument_pydantic_ai
from worker_tests.conftest import APP_VERSION, enqueue_run, ticket_spec, wait_terminal


@pytest.fixture
def telemetry(worker, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs.shutdown_telemetry()
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    provider = obs.setup_telemetry("eap-worker", exporter=spans)
    obs.setup_logging("eap-worker", exporter=logs)
    assert instrument_pydantic_ai() is True

    def finished():
        provider.force_flush()
        obs._state.logger_provider.force_flush()
        return spans.get_finished_spans(), [r.log_record for r in logs.get_finished_logs()]

    yield provider, finished
    uninstrument_pydantic_ai()
    obs.shutdown_telemetry()


def _api_request_trace(provider) -> tuple[str, str, int, int]:
    """What the API stores: the traceparent of its request span (ended long before the worker runs)."""
    with provider.get_tracer("api").start_as_current_span("POST /api/v1/agents/{agent_id}/runs") as span:
        trace_id, traceparent = obs.current_trace_context()
        ctx = span.get_span_context()
    return trace_id, traceparent, ctx.trace_id, ctx.span_id


def _descends_from(span, ancestor_id: int, by_id: dict[int, object]) -> bool:
    cur = span
    while cur is not None and cur.parent is not None:
        if cur.parent.span_id == ancestor_id:
            return True
        cur = by_id.get(cur.parent.span_id)
    return False


def test_run_joins_the_api_request_trace(worker, client, tenant, telemetry):
    provider, finished = telemetry
    trace_id, traceparent, trace_int, api_span_id = _api_request_trace(provider)
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    worker.run_store.create_run(tenant, agent_id, 1, "My printer is on fire", run_id,
                                trace_id=trace_id, traceparent=traceparent)
    enqueue_run(client, worker.run_store, tenant_id=tenant, spec=ticket_spec(), input_text="My printer is on fire",
                app_version=APP_VERSION, run_id=run_id, agent_id=agent_id, create_row=False)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]

    spans, logs = finished()
    by_id = {s.context.span_id: s for s in spans}
    runs = [s for s in spans if s.name == "eap.run" and s.attributes["eap.run_id"] == str(run_id)]
    assert len(runs) == 1
    run_span = runs[0]
    assert run_span.context.trace_id == trace_int
    assert run_span.parent.span_id == api_span_id
    assert run_span.attributes["eap.tenant_id"] == tenant
    assert run_span.attributes["eap.agent_id"] == str(agent_id)
    assert run_span.attributes["eap.agent_version"] == 1
    assert run_span.attributes["eap.run_status"] == "completed"

    in_trace = [s for s in spans if s.context.trace_id == trace_int]
    model_requests = [s for s in in_trace if s.name.startswith("chat ")]
    tool_calls = [s for s in in_trace if s.name.startswith("execute_tool ")]
    assert len(model_requests) == 2, [s.name for s in in_trace]  # tool call, then the final answer
    assert len(tool_calls) == 1, [s.name for s in in_trace]
    for s in model_requests + tool_calls:
        assert _descends_from(s, run_span.context.span_id, by_id), s.name
    # Content stays out of spans (include_content=False).
    for s in in_trace:
        assert "printer is on fire" not in repr(dict(s.attributes or {})), s.name

    run_logs = [r for r in logs if isinstance(r.body, str) and str(run_id) in r.body]
    assert run_logs, [r.body for r in logs]
    assert all(r.trace_id == trace_int for r in run_logs)


def test_run_without_stored_traceparent_still_completes(worker, client, tenant, telemetry):
    _, finished = telemetry
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=ticket_spec(), input_text="hi",
                         app_version=APP_VERSION)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    spans, _ = finished()
    (run_span,) = [s for s in spans if s.name == "eap.run" and s.attributes["eap.run_id"] == str(run_id)]
    assert run_span.parent is None  # its own trace (DBOS tracing is off in this process)


def test_dbos_config_enables_dbos_tracing_only_with_an_endpoint(worker_settings, monkeypatch):
    from eap_worker.app import dbos_config

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert "enable_otlp" not in dbos_config(worker_settings)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    config = dbos_config(worker_settings)
    assert config["enable_otlp"] is True
    # DBOS exports through our providers: no second exporter of its own.
    assert "otlp_traces_endpoints" not in config and "otlp_logs_endpoints" not in config


def test_worker_telemetry_setup_is_a_noop_without_an_endpoint(monkeypatch):
    from eap_worker.app import setup_worker_telemetry

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs.shutdown_telemetry()
    assert setup_worker_telemetry() is False
    assert obs.tracer_provider() is None
