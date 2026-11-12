"""eap_observability: setup is a no-op without an endpoint; run spans join a stored traceparent;
logs carry the trace id; Pydantic AI spans leave content out."""

from __future__ import annotations

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

import eap_observability as obs
from eap_observability.agents import instrument_pydantic_ai, uninstrument_pydantic_ai

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs.shutdown_telemetry()
    yield
    uninstrument_pydantic_ai()
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


# --------------------------------------------------------------------------- off by default


def test_without_endpoint_everything_is_a_noop():
    root_handlers = list(logging.getLogger().handlers)
    assert obs.setup_telemetry("eap-api") is None
    assert obs.tracer_provider() is None
    assert obs.setup_logging("eap-api") is False
    assert logging.getLogger().handlers == root_handlers
    assert instrument_pydantic_ai() is False
    app = FastAPI()
    obs.instrument_fastapi(app)
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
    assert obs.dbos_otel_config(None) == {}
    assert obs.dbos_trace_attributes(None) is None
    with obs.run_span("r", "t", "a", 1, traceparent=None) as span:
        assert not span.is_recording()


def test_blank_endpoint_counts_as_unset(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "  ")
    assert obs.otlp_endpoint_from_env() is None
    assert obs.setup_telemetry("eap-api") is None


def test_endpoint_from_env_enables_otlp_exporters(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    provider = obs.setup_telemetry("eap-api")
    assert provider is not None
    assert provider.resource.attributes["service.name"] == "eap-api"
    assert obs.setup_telemetry("eap-api") is provider  # idempotent


def test_dbos_config_reuses_our_providers():
    # No DBOS endpoints: DBOS exports through the global providers instead of a second exporter.
    assert obs.dbos_otel_config("http://127.0.0.1:4318") == {"enable_otlp": True}


def test_dbos_trace_attributes_carry_the_traceparent():
    tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    assert obs.dbos_trace_attributes(tp) == {"dbos.otelContext": {"traceparent": tp}}


# --------------------------------------------------------------------------- spans


def test_current_trace_context_outside_and_inside_a_span(spans):
    provider, _ = spans
    assert obs.current_trace_context() == (None, None)
    with provider.get_tracer("t").start_as_current_span("request") as span:
        trace_id, traceparent = obs.current_trace_context()
        ctx = span.get_span_context()
    assert trace_id == format(ctx.trace_id, "032x")
    m = TRACEPARENT.match(traceparent)
    assert m and m.group(1) == trace_id and m.group(2) == format(ctx.span_id, "016x")


def test_run_span_attributes(spans):
    _, finished = spans
    with obs.run_span("run-1", "tenant-1", "agent-1", 3):
        assert obs.current_trace_id() is not None
    (span,) = finished()
    assert span.name == "eap.run"
    assert dict(span.attributes) == {
        "eap.run_id": "run-1", "eap.tenant_id": "tenant-1", "eap.agent_id": "agent-1", "eap.agent_version": 3,
    }


def test_run_span_is_a_child_of_the_stored_traceparent(spans):
    provider, finished = spans
    with provider.get_tracer("t").start_as_current_span("api request"):
        trace_id, traceparent = obs.current_trace_context()
    # Later, elsewhere (the worker), with no ambient span:
    with obs.run_span("run-1", "t", "a", 1, traceparent=traceparent) as span:
        obs.set_run_outcome(span, "failed", "boom")
    run = next(s for s in finished() if s.name == "eap.run")
    assert format(run.context.trace_id, "032x") == trace_id
    assert format(run.parent.span_id, "016x") == TRACEPARENT.match(traceparent).group(2)
    assert run.attributes["eap.run_status"] == "failed"
    assert run.status.status_code == trace.StatusCode.ERROR


def test_unreadable_traceparent_falls_back_to_a_new_trace(spans):
    _, finished = spans
    with obs.run_span("run-1", "t", "a", 1, traceparent="garbage"):
        pass
    (run,) = finished()
    assert run.parent is None


def test_instrument_fastapi_creates_server_span(spans):
    provider, finished = spans
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"trace": obs.current_trace_context()[1]}

    obs.instrument_fastapi(app)
    traceparent = TestClient(app).get("/ping").json()["trace"]
    server = [s for s in finished() if s.kind == trace.SpanKind.SERVER]
    assert len(server) == 1
    # The handler sees the server span as current: that is what the API stores on a run.
    assert TRACEPARENT.match(traceparent).group(2) == format(server[0].context.span_id, "016x")


def test_pydantic_ai_spans_exclude_content(spans):
    _, finished = spans
    assert instrument_pydantic_ai() is True
    Agent(TestModel(), output_type=str).run_sync("my secret is hunter2")
    exported = finished()
    names = [s.name for s in exported]
    assert any(n.startswith("chat ") for n in names), names
    for s in exported:
        assert "hunter2" not in repr(dict(s.attributes or {})), s.name


# --------------------------------------------------------------------------- logs


def test_logs_carry_trace_and_span_ids(spans):
    exporter = InMemoryLogRecordExporter()
    assert obs.setup_logging("test-service", exporter=exporter) is True
    log = logging.getLogger("eap_test.logs")
    with obs.run_span("run-1", "t", "a", 1) as span:
        log.info("inside the run")
    log.info("outside")
    obs._state.logger_provider.force_flush()
    records = {r.log_record.body: r.log_record for r in exporter.get_finished_logs()}
    inside = records["inside the run"]
    assert inside.trace_id == span.get_span_context().trace_id
    assert inside.span_id == span.get_span_context().span_id
    assert records["outside"].trace_id == 0
    obs.shutdown_telemetry()
    assert all(not type(h).__name__ == "LoggingHandler" for h in logging.getLogger().handlers)


# --------------------------------------------------------------------------- redaction (no prompts, no secrets)

PROMPT = "SENTINEL-PROMPT my card is 4111"
KEY = "sk-SENTINEL0123456789abcdef"
BEARER = "Bearer SENTINELtoken0123456789"
URL_PASSWORD = "SENTINELpassw0rd"
SENTINELS = ("SENTINEL",)  # every prompt and secret sentinel contains it


def _leaks(text: str) -> list[str]:
    return [s for s in SENTINELS if s in text]


def test_redact_scrubs_tokens_keys_and_url_credentials():
    text = (
        f"Authorization: {BEARER} failed; api_key={KEY}; also sk-proj-SENTINELabcdefghijkl "
        f"via postgresql://eap:{URL_PASSWORD}@db:5432/x and https://h/v1?token=SENTINELq&x=1 "
        f'{{"api_key": "SENTINELjson", "password": "SENTINELpw"}} x-api-key: SENTINELhdr'
    )
    out = obs.redact(text)
    assert not _leaks(out), out
    assert "[REDACTED]" in out and "postgresql://" in out and "db:5432" in out
    assert obs.redact("plain words stay") == "plain words stay"
    assert len(obs.redact("x" * 10_000, max_length=200)) <= 200 + len("…[truncated]")


def test_exception_type_name_is_bounded_and_safe():
    assert obs.exception_type(ValueError("SENTINEL")) == "ValueError"
    weird = type("Bad name; SENTINEL", (Exception,), {})
    assert obs.exception_type(weird()) == "Exception"


def _exported_text(record) -> str:
    return repr(record.body) + repr(dict(record.attributes or {}))


@pytest.fixture
def logs():
    exporter = InMemoryLogRecordExporter()
    assert obs.setup_logging("test-service", exporter=exporter) is True

    def finished():
        obs._state.logger_provider.force_flush()
        return [r.log_record for r in exporter.get_finished_logs()]

    return finished


def test_exported_logs_never_carry_exception_text_or_secrets(logs):
    log = logging.getLogger("eap_test.redaction")
    try:
        raise RuntimeError(f"provider rejected {PROMPT!r} with {BEARER} key={KEY}")
    except RuntimeError:
        log.warning("model request failed", exc_info=True)
    try:
        raise RuntimeError(f"connect to postgresql://u:{URL_PASSWORD}@db failed: {BEARER} key={KEY}")
    except RuntimeError as exc:
        # Free text is scrubbed of credentials. (It cannot be scrubbed of prompt text, which is why
        # our code never formats exception text into a message: see log_failure.)
        log.exception("again: %s", exc)
    log.error("calling https://u:%s@h/x with api_key=%s", URL_PASSWORD, KEY)

    # A third-party handler on the same provider (what DBOS's enable_otlp installs) is covered too.
    from opentelemetry.instrumentation.logging.handler import LoggingHandler

    other = logging.getLogger("eap_test.thirdparty")
    other.propagate = False
    handler = LoggingHandler(logger_provider=obs._state.logger_provider)
    other.addHandler(handler)
    try:
        try:
            raise ValueError(f"{PROMPT} {KEY}")
        except ValueError:
            other.warning("Step being automatically retried", exc_info=True)
    finally:
        other.removeHandler(handler)

    records = logs()
    assert len(records) == 4, [r.body for r in records]
    for r in records:
        assert not _leaks(_exported_text(r)), _exported_text(r)
        assert "exception.message" not in (r.attributes or {})
        assert "exception.stacktrace" not in (r.attributes or {})
    types = [(r.attributes or {}).get("exception.type") for r in records]
    assert types.count("RuntimeError") == 2 and "ValueError" in types


def test_log_failure_logs_a_bounded_description_and_keeps_detail_local(logs, caplog):
    log = logging.getLogger("eap_test.failure")
    exc = RuntimeError(f"{PROMPT} {BEARER}")
    with caplog.at_level(logging.DEBUG, logger="eap_test.failure"):
        obs.log_failure(log, "run failed", exc, code="model_http_error", run_id="r-1", tenant_id="t-1")
    (record,) = logs()
    assert not _leaks(_exported_text(record)), _exported_text(record)
    assert "r-1" in record.body and "model_http_error" in record.body and "RuntimeError" in record.body
    attrs = dict(record.attributes or {})
    assert attrs["eap.error_code"] == "model_http_error"
    assert attrs["exception.type"] == "RuntimeError"
    assert attrs["eap.run_id"] == "r-1" and attrs["eap.tenant_id"] == "t-1"
    # Full detail exists only as a local (never exported) debug record.
    local = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert local and local[0].exc_info is not None and getattr(local[0], obs.LOCAL_ONLY_LOG_ATTRIBUTE) is True


def test_exported_spans_never_carry_exception_text_or_secrets(spans):
    provider, finished = spans
    tracer = provider.get_tracer("eap_test")
    with pytest.raises(RuntimeError):
        with tracer.start_as_current_span("model request") as span:
            span.set_attribute("http.url", f"https://user:{URL_PASSWORD}@llm.local/v1?api_key={KEY}")
            span.set_attribute("eap.run_id", "r-1")
            span.add_event("Step attempt 0 failed", {"error": f"{PROMPT} {BEARER}", "retryIntervalSeconds": 1.0})
            raise RuntimeError(f"rejected {PROMPT} {KEY}")
    with tracer.start_as_current_span("custom") as span:
        span.set_status(trace.Status(trace.StatusCode.ERROR, f"provider said {PROMPT}"))
    exported = {s.name: s for s in finished()}
    for s in exported.values():
        text = repr(dict(s.attributes or {})) + repr([(e.name, dict(e.attributes or {})) for e in s.events])
        text += repr(s.status.description)
        assert not _leaks(text), text
    failed = exported["model request"]
    assert failed.status.status_code == trace.StatusCode.ERROR
    assert failed.status.description == "RuntimeError"
    assert failed.attributes["eap.run_id"] == "r-1"
    (exc_event,) = [e for e in failed.events if e.name == "exception"]
    assert exc_event.attributes["exception.type"] == "RuntimeError"
    (retry,) = [e for e in failed.events if e.name.startswith("Step attempt")]
    assert retry.attributes["retryIntervalSeconds"] == 1.0 and "error" not in retry.attributes
    assert exported["custom"].status.description == "error"


def test_pydantic_ai_provider_failure_spans_carry_no_content(spans):
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.models.function import FunctionModel

    _, finished = spans
    assert instrument_pydantic_ai() is True

    def fail(messages, info):
        raise ModelHTTPError(401, "m", body={"error": f"{BEARER} {KEY} echo: {PROMPT}"})

    with pytest.raises(ModelHTTPError):
        Agent(FunctionModel(fail), output_type=str).run_sync(PROMPT)
    exported = finished()
    assert exported
    for s in exported:
        text = repr(dict(s.attributes or {})) + repr([dict(e.attributes or {}) for e in s.events])
        assert not _leaks(text + repr(s.status.description)), (s.name, text)
