"""LP.9 tester: iteration_span allowlist, traceparent parenting, index validation, status mapping."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import eap_observability as obs

SECRET = "SECRET-ITEM-STATE"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs.shutdown_telemetry()
    yield
    obs.shutdown_telemetry()


@pytest.fixture
def env():
    exporter = InMemorySpanExporter()
    provider = obs.setup_telemetry("svc", exporter=exporter)
    assert provider is not None

    def finished():
        provider.force_flush()
        return [s for s in exporter.get_finished_spans() if s.name == obs.ITERATION_SPAN_NAME]

    return provider, finished


def test_allowlist_is_exactly_the_documented_ids_and_counts():
    assert set(obs.ITERATION_SPAN_ATTRIBUTES) == {
        "eap.run_id", "eap.tenant_id", "eap.workflow_step_id", "eap.loop_kind", "eap.loop_scope",
        "eap.loop_depth", "eap.iteration_index", "eap.iteration_status", "eap.error_code",
        "eap.body_steps", "eap.child_runs",
    }
    assert len(obs.ITERATION_SPAN_ATTRIBUTES) == len(set(obs.ITERATION_SPAN_ATTRIBUTES))


def test_every_attribute_in_every_outcome_is_allowlisted(env):
    _, finished = env
    with obs.iteration_span("r", "t", "each", 0, scope="each.0", loop_kind="foreach") as s:
        obs.set_iteration_outcome(s, "failed", "agent_failed", body_steps=2, child_runs=1)
    with obs.iteration_span("r", "t", "lp", 3, scope="o.1/lp.3") as s:
        obs.set_iteration_outcome(s, "completed")
    with obs.iteration_span("r", "t", "lp", 4):
        pass  # no outcome set
    spans = finished()
    assert len(spans) == 3
    for sp in spans:
        assert set(sp.attributes) <= set(obs.ITERATION_SPAN_ATTRIBUTES)
        assert all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in sp.attributes.values())
        assert SECRET not in repr(sp.to_json())
    assert spans[1].attributes["eap.loop_depth"] == 2 and spans[1].attributes["eap.loop_kind"] == "loop"
    assert spans[2].attributes["eap.loop_depth"] == 1 and "eap.iteration_status" not in spans[2].attributes


def test_secret_in_error_code_arg_is_the_only_free_text_and_status_description_is_the_code(env):
    _, finished = env
    with obs.iteration_span("r", "t", "x", 0) as s:
        obs.set_iteration_outcome(s, "failed", "run_budget_exceeded")
    (sp,) = finished()
    assert sp.status.status_code == trace.StatusCode.ERROR and sp.status.description == "run_budget_exceeded"


@pytest.mark.parametrize("status,ok", [("completed", True), ("failed", False), ("needs_attention", False),
                                         ("cancelled", False)])
def test_status_mapping(env, status, ok):
    _, finished = env
    with obs.iteration_span("r", "t", "x", 0) as s:
        obs.set_iteration_outcome(s, status)
    (sp,) = finished()
    assert sp.attributes["eap.iteration_status"] == status
    assert (sp.status.status_code == trace.StatusCode.OK) is ok
    if not ok:
        # The exporter redaction reduces a description that is not a known error code to "error".
        assert sp.status.status_code == trace.StatusCode.ERROR and sp.status.description == "error"


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "1", None])
def test_bad_index_raises_value_error(env, bad):
    with pytest.raises(ValueError):
        with obs.iteration_span("r", "t", "x", bad):  # type: ignore[arg-type]
            pytest.fail("body must not run")


def test_bad_counts_raise(env):
    with obs.iteration_span("r", "t", "x", 0) as s:
        for kw in ({"body_steps": -1}, {"child_runs": True}, {"child_runs": 1.5}):
            with pytest.raises(ValueError):
                obs.set_iteration_outcome(s, "completed", **kw)  # type: ignore[arg-type]


def test_valid_traceparent_parents_and_invalid_falls_back_to_current(env):
    provider, finished = env
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("eap.run"):
        trace_id, tp = obs.current_trace_context()
    with obs.iteration_span("r", "t", "x", 0, traceparent=tp):
        pass
    for bad in ("garbage", "00-" + "0" * 32 + "-" + "0" * 16 + "-01", ""):
        with tracer.start_as_current_span("outer") as outer:
            with obs.iteration_span("r", "t", "x", 1, traceparent=bad):
                pass
            outer_tid = outer.get_span_context().trace_id
        last = finished()[-1]
        assert last.context.trace_id == outer_tid
    first = finished()[0]
    assert format(first.context.trace_id, "032x") == trace_id
    assert first.parent is not None and first.parent.is_valid


def test_without_traceparent_is_a_child_of_the_current_span(env):
    provider, finished = env
    with provider.get_tracer("t").start_as_current_span("wf") as wf:
        with obs.iteration_span("r", "t", "x", 0):
            pass
    (sp,) = finished()
    assert sp.parent.span_id == wf.get_span_context().span_id


def test_non_recording_when_telemetry_off_and_outcome_is_safe():
    with obs.iteration_span("r", "t", "x", 0, scope="x.0", traceparent="garbage") as s:
        assert not s.is_recording()
        obs.set_iteration_outcome(s, "failed", "code", body_steps=1, child_runs=0)
