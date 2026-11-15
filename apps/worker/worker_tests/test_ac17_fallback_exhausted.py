"""AC.17 review fix: an exhausted fallback chain is visible in the trace.

run_failed (`fallback_exhausted`) carries `model_request` and `failed_attempts` (ids, error class
and HTTP status only, never provider text), built in workflow code from the recorded exception, so
the step layout is unchanged; the trace shows the exhausted request with one attempt span per model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic_ai.exceptions import FallbackExceptionGroup, ModelHTTPError
from pydantic_ai.messages import ModelRequestAttempt

from eap_run_store.metrics import FALLBACK_EXHAUSTED_RUN_ERROR
from eap_run_store.trace import TraceReader
from eap_worker.errors import classify, describe_fallback_exhausted
from worker_tests.test_conversation import step_names
from worker_tests.test_limits_fallback import events_by_key, fallbacks, run, spec_with

CHAIN = ("test:fail-503", "test:fail-400")


def test_describe_from_member_exceptions_keeps_no_text():
    group = FallbackExceptionGroup(
        "All models failed", [ModelHTTPError(503, "fail-503", {"secret": "BODY"}), ModelHTTPError(400, "fail-400", "BODY")]
    )
    describe_fallback_exhausted(group, CHAIN, 2)
    data = classify(group).data
    assert data == {
        "cause": "model_http_error",
        "model_request": 2,
        "failed_attempts": [
            {"model_id": "test:fail-503", "outcome": "error", "error_type": "ModelHTTPError", "http_status": 503},
            {"model_id": "test:fail-400", "outcome": "error", "error_type": "ModelHTTPError", "http_status": 400},
        ],
    }
    assert "BODY" not in repr(data) and "secret" not in repr(data)


def test_describe_from_attempts_carries_timing():
    t0 = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)
    group = FallbackExceptionGroup("All models failed", [ModelHTTPError(503, "fail-503", "BODY")])
    group.attempts = [
        ModelRequestAttempt(model_name="fail-503", outcome="error", error="ModelHTTPError: status_code: 503, body: BODY",
                            timestamp=t0, duration=timedelta(milliseconds=250)),
    ]
    describe_fallback_exhausted(group, CHAIN, 0)
    [attempt] = classify(group).data["failed_attempts"]
    assert attempt == {"model_id": "test:fail-503", "outcome": "error", "error_type": "ModelHTTPError",
                       "http_status": 503, "timestamp": t0.isoformat(), "duration_ms": 250}


def test_runs_error_text_is_the_metrics_constant():
    """Metrics count exhausted runs by runs.error (runs store no error code): the two must agree."""
    group = FallbackExceptionGroup("All models failed", [ModelHTTPError(503, "m", "BODY")])
    assert classify(group).error == FALLBACK_EXHAUSTED_RUN_ERROR


def test_single_model_or_other_errors_are_untouched():
    group = FallbackExceptionGroup("x", [ModelHTTPError(503, "m", "b")])
    describe_fallback_exhausted(group, ("test:m",), 0)
    assert classify(group).data == {"cause": "model_http_error"}
    plain = ModelHTTPError(503, "m", "b")
    describe_fallback_exhausted(plain, CHAIN, 0)
    assert not hasattr(plain, "eap_fallback")


def test_exhausted_run_records_attempts_and_trace_spans_with_unchanged_layout(worker, client, tenant):
    spec = spec_with(model="fail-503", fallback_models=fallbacks("fail-400"))
    run_id, row = run(worker, client, tenant, spec)
    assert row["status"] == "failed", row
    assert row["error"] == FALLBACK_EXHAUSTED_RUN_ERROR
    end = events_by_key(worker.engine, run_id)["end"][1]
    assert end["error_code"] == "fallback_exhausted" and end["cause"] == "model_http_error"
    assert end["model_request"] == 0
    models = [a["model_id"] for a in end["failed_attempts"]]
    assert models[0] == "test:fail-503" and "test:fail-400" in models
    assert all(set(a) <= {"model_id", "outcome", "error_type", "http_status", "timestamp", "duration_ms"}
               for a in end["failed_attempts"])
    assert "scripted" not in repr(end) and "body" not in repr(end).lower()
    # No new step: the plain failed-request layout.
    assert step_names(worker.settings.dbos_system_schema, run_id) == [
        "eap.start_run", "eap.append_event", "eap-agent__dynamic_toolset__eap-spec-tools.get_tools",
        "eap-agent__model.request_stream", "eap.append_event", "eap.finish_run",
    ]
    tr = TraceReader(worker.engine).read(tenant, run_id)
    [model] = [s for s in tr["spans"] if s["kind"] == "model_request"]
    assert model["status"] == "error" and model["attributes"]["exhausted"] is True
    attempts = [s for s in tr["spans"] if s["kind"] == "fallback_attempt"]
    assert [a["attributes"]["model_id"] for a in attempts] == models
    assert all(a["parent_id"] == model["id"] and a["status"] == "error" for a in attempts)
    # Nothing was billed: the totals still count recorded requests only.
    assert tr["totals"]["model_requests"] == 0


def test_a_run_created_without_a_model_records_the_spec_model_and_counts_its_fallback(worker, client, tenant):
    """The test helper creates rows without run options, like a workflow step's or an eval case's
    child run: eap.start_run records the spec's model, so the fallback answer is counted."""
    from eap_run_store.metrics import MetricsQuery, MetricsStore

    spec = spec_with(model="fail-503", fallback_models=fallbacks("priced"))
    run_id, row = run(worker, client, tenant, spec)
    assert row["status"] == "completed", row
    assert worker.run_store.get_run(tenant, run_id)["model_id"] == "test:fail-503"
    now = datetime.now(timezone.utc)
    fb = MetricsStore(worker.engine).summary(
        MetricsQuery(tenant_id=tenant, start=now - timedelta(hours=1), end=now + timedelta(minutes=1))
    )["current"]["fallback"]
    assert (fb["chat_requests"], fb["fell_back"]) == (1, 1)
