"""AC.17 acceptance (tester): real runs on the in-process worker (scripted models), read back through
TraceReader and MetricsStore: fallback attempts, approval waits, output_retry, eval traces, the
metrics operations/kind/fallback numbers, golden totals of runs without these features, tenants."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from eap_run_store.metrics import MetricsQuery, MetricsStore
from eap_run_store.trace import TraceReader
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.eval_run_helpers import JUDGE, cleanup_evals, create_suite, start_eval
from worker_tests.test_limits_fallback import fallbacks, run, spec_with
from worker_tests.test_structured_output import structured
from worker_tests.test_tool_approval import decide, events, gated_spec, prompt, start, wait_waiting
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent


def trace(worker, tenant, run_id):
    return TraceReader(worker.engine).read(tenant, run_id)


def spans(tr, kind):
    return [s for s in tr["spans"] if s["kind"] == kind]


def mq(tenant, **kw):
    now = datetime.now(timezone.utc)
    return MetricsQuery(tenant_id=tenant, start=now - timedelta(hours=1), end=now + timedelta(hours=1), **kw)


def db_sum(engine, run_id, col="input_tokens"):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COALESCE(SUM({col}),0) FROM run_model_requests WHERE run_id=:r"),
                            {"r": str(run_id)}).scalar_one()


# --------------------------------------------------------------------------- fallback in the trace


def _fallback_run(worker, client, tenant):
    run_id, row = run(worker, client, tenant, spec_with(model="fail-503", fallback_models=fallbacks("priced")), "hi")
    assert row["status"] == "completed", row
    return run_id


def test_fallback_run_shows_the_failed_attempt_span_and_the_answering_model(worker, client, tenant):
    run_id = _fallback_run(worker, client, tenant)
    tr = trace(worker, tenant, run_id)
    [req] = spans(tr, "model_request")
    [att] = spans(tr, "fallback_attempt")
    a = req["attributes"]
    assert (a["answering_model"], a["requested_model_id"], a["fallback_index"]) == ("test:priced", "test:fail-503", 1)
    assert a["failed_attempts"] == [{"model_id": "test:fail-503", "outcome": "error",
                                     "error_type": "ModelHTTPError", "http_status": 503}]
    assert att["parent_id"] == req["id"] and att["id"] == f"{run_id}:model:0:failed:0" and att["status"] == "error"
    assert att["attributes"]["timed"] is True and att["attributes"]["http_status"] == 503
    start, end = datetime.fromisoformat(att["start"]), datetime.fromisoformat(att["end"])
    assert end >= start and datetime.fromisoformat(req["start"]) <= start <= datetime.fromisoformat(req["end"])
    assert tr["totals"]["input_tokens"] == db_sum(worker.engine, run_id) == 1000  # chat row only
    assert att["attributes"]["usage"] is None  # a 503 reports no usage and records no row


def test_failed_attempt_usage_is_on_its_span_and_in_the_totals(worker, client, tenant):
    run_id = _fallback_run(worker, client, tenant)
    with worker.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at, operation)"
            " VALUES (:t, :r, 'model:0:failed:0', 'test:fail-503', 40, 0, 0, 0, true, 0.0004, 'override', now(), 'chat')"),
            {"t": tenant, "r": str(run_id)})
    tr = trace(worker, tenant, run_id)
    [att] = spans(tr, "fallback_attempt")
    assert att["attributes"]["usage"]["input_tokens"] == 40 and Decimal(att["attributes"]["cost_usd"]) == Decimal("0.0004")
    assert tr["totals"]["input_tokens"] == 1040 == db_sum(worker.engine, run_id)
    assert Decimal(tr["totals"]["cost_usd"]) == Decimal("0.0204")


def test_exhausted_chain_fails_the_run_span_and_records_no_answered_request(worker, client, tenant):
    """No model_message is stored when every model fails; since the AC.17 review fix, run_failed
    carries the attempts, so the trace shows the exhausted request (error) with an attempt span per
    model, while the totals count no answered (or billed) request."""
    run_id, row = run(worker, client, tenant, spec_with(model="fail-503", fallback_models=fallbacks("fail-400")), "hi")
    assert row["status"] == "failed", row
    tr = trace(worker, tenant, run_id)
    assert tr["run"]["status"] == "failed" and tr["spans"][0]["status"] == "error"
    [model] = spans(tr, "model_request")
    assert model["status"] == "error" and model["attributes"]["exhausted"] is True
    assert len(spans(tr, "fallback_attempt")) >= 2 and tr["totals"]["model_requests"] == 0
    end = [d for _s, _k, t, d in event_rows(worker.engine, run_id) if t == "run_failed"][0]
    assert end["error_code"] == "fallback_exhausted"


# --------------------------------------------------------------------------- approval waits


def _approval(tr):
    [ap] = spans(tr, "tool_approval")
    return ap


def test_approval_open_decided_denied_and_timed_out_spans(worker, client, tenant, monkeypatch):
    run_id = start(worker, client, tenant, gated_spec(), prompt())
    [w] = wait_waiting(worker, run_id)
    call = w["tool_call_id"]
    tr = trace(worker, tenant, run_id)  # still open: waiting
    ap = _approval(tr)
    assert ap["status"] == "waiting" and ap["end"] is None and ap["parent_id"] == f"{run_id}:tool:{call}"
    assert tr["run"]["display_status"] == "waiting_approval" and tr["spans"][0]["status"] == "waiting"
    assert "args" not in json.dumps(ap["attributes"]) and "Laptop" not in json.dumps(ap["attributes"])
    decide(worker, client, tenant, run_id, call, approve=True, comment="go")
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    ap = _approval(trace(worker, tenant, run_id))
    assert ap["status"] == "ok" and ap["attributes"]["decision"]["type"] == "tool_approved"
    assert ap["attributes"]["decision"]["decided_by"] == "alice" and ap["attributes"]["waited"] is True
    assert datetime.fromisoformat(ap["end"]) >= datetime.fromisoformat(ap["start"])
    # Denied: the approval span is ok, the tool_call span is ok with result_status denied.
    d_id = start(worker, client, tenant, gated_spec(), prompt())
    [dw] = wait_waiting(worker, d_id)
    decide(worker, client, tenant, d_id, dw["tool_call_id"], approve=False)
    wait_terminal(worker.run_store, tenant, d_id)
    tr = trace(worker, tenant, d_id)
    assert _approval(tr)["status"] == "ok" and _approval(tr)["attributes"]["decision"]["type"] == "tool_denied"
    [tc] = spans(tr, "tool_call")
    assert tc["status"] == "ok" and tc["attributes"]["result_status"] == "denied"
    # Timed out: error.
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "1")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.3")
    t_id = start(worker, client, tenant, gated_spec(timeout_hours=1), prompt())
    assert wait_terminal(worker.run_store, tenant, t_id)["status"] == "completed"
    ap = _approval(trace(worker, tenant, t_id))
    assert ap["status"] == "error" and ap["attributes"]["decision"]["type"] == "tool_approval_timed_out"


def test_undecided_approval_of_an_ended_run_is_incomplete(worker, client, tenant):
    run_id, row = run(worker, client, tenant, spec_with(), "hi")
    assert row["status"] == "completed"
    seq = max(e[0] for e in event_rows(worker.engine, run_id)) + 1
    with worker.engine.begin() as conn:
        for k, t, d in (("tool:c9:call", "tool_call", {"tool_call_id": "c9", "tool_name": "send"}),
                        ("tool:c9:approval:waiting", "tool_approval_waiting", {"tool_call_id": "c9", "tool_name": "send"})):
            conn.execute(text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                              " VALUES (:t, :r, :s, :k, :ty, CAST(:d AS jsonb), now())"),
                         {"t": tenant, "r": str(run_id), "s": seq, "k": k, "ty": t, "d": json.dumps(d)})
            seq += 1
    assert _approval(trace(worker, tenant, run_id))["status"] == "incomplete"


# --------------------------------------------------------------------------- output_retry, golden


def test_output_retry_marker_and_candidate_flag(worker, client, tenant):
    run_id, row = run(worker, client, tenant, structured(mode="tool"), json.dumps({"bad_first": True}))
    assert row["status"] == "completed", row
    tr = trace(worker, tenant, run_id)
    [rt] = spans(tr, "output_retry")
    assert rt["id"] == f"{run_id}:output:0:retry" and rt["status"] == "ok" and rt["start"] == rt["end"]
    assert rt["attributes"]["error_count"] == len(rt["attributes"]["errors"]) >= 1
    assert all(isinstance(e, str) for e in rt["attributes"]["errors"])
    reqs = spans(tr, "model_request")
    assert len(reqs) == 2 and reqs[0]["attributes"].get("output_candidate") is True
    assert tr["totals"]["input_tokens"] == db_sum(worker.engine, run_id)


def test_plain_run_golden_totals_shape_and_no_capability_spans(worker, client, tenant):
    run_id, row = run(worker, client, tenant, spec_with(model="priced"), "hi")
    tr = trace(worker, tenant, run_id)
    t = tr["totals"]
    assert (t["input_tokens"], t["output_tokens"], Decimal(t["cost_usd"])) == (1000, 500, Decimal("0.02"))
    assert (t["chat_requests"], t["judge_requests"], t["judge_input_tokens"], t["model_requests"]) == (1, 0, 0, 1)
    assert t["input_tokens"] == db_sum(worker.engine, run_id) == row["input_tokens"]
    assert {s["kind"] for s in tr["spans"]} == {"run", "model_request"}
    attrs = spans(tr, "model_request")[0]["attributes"]
    assert not {"failed_attempts", "requested_model_id", "fallback_index", "output_candidate"} & set(attrs)
    assert tr["run"].get("display_status") in (None, "completed")


# --------------------------------------------------------------------------- metrics of real runs


def test_metrics_fallback_block_and_answering_model_from_real_runs(worker, client, tenant):
    _fallback_run(worker, client, tenant)
    run(worker, client, tenant, spec_with(model="priced"), "hi")  # no chain
    run(worker, client, tenant, spec_with(model="fail-503", fallback_models=fallbacks("fail-400")), "hi")
    store = MetricsStore(worker.engine)
    cur = store.summary(mq(tenant))["current"]
    # Review 2026-11-15 definition: out of every agent chat request (the fallen-back one and the
    # no-chain run's), with the exhausted run counted; the 503 attempts were never billed.
    assert cur["fallback"] == {"chat_requests": 2, "fell_back": 1, "billed_failed_attempts": 0, "exhausted_runs": 1,
                               "fallback_rate": 2 / 3}
    assert cur["requests"] == 2  # the failed 503 attempts record no usage; two answered requests
    items = {i["key"]: i for i in store.breakdown(mq(tenant), "model", sort="requests")}
    assert items["test:priced"]["requests"] == 2 and "test:fail-503" not in items
    assert store.summary(mq(tenant, operation="moderation"))["current"]["fallback"]["chat_requests"] == 0
    by_primary = store.summary(mq(tenant, model="test:fail-503"))["current"]["fallback"]
    assert (by_primary["chat_requests"], by_primary["fell_back"], by_primary["exhausted_runs"]) == (1, 1, 1)
    other = store.summary(mq(tenant + "x"))["current"]
    assert other["requests"] == 0 and other["fallback"]["chat_requests"] == 0 and other["fallback"]["exhausted_runs"] == 0
    assert other["fallback"]["fallback_rate"] is None


def test_metrics_totals_of_plain_runs_equal_the_rows_and_carry_a_zero_fallback_block(worker, client, tenant):
    ids = [run(worker, client, tenant, spec_with(model="priced"), "hi")[0] for _ in range(2)]
    cur = MetricsStore(worker.engine).summary(mq(tenant))["current"]
    assert (cur["runs"], cur["requests"], cur["input_tokens"], cur["output_tokens"]) == (2, 2, 2000, 1000)
    assert cur["input_tokens"] == sum(db_sum(worker.engine, i) for i in ids)
    assert Decimal(str(cur["cost_usd"])) == Decimal("0.04") and cur["operations"]["chat"]["requests"] == 2
    assert cur["operations"]["judge"]["requests"] == 0
    assert cur["fallback"] == {"chat_requests": 2, "fell_back": 0, "billed_failed_attempts": 0, "exhausted_runs": 0,
                               "fallback_rate": 0.0}


# --------------------------------------------------------------------------- eval runs


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_evals(worker.engine, tenant)
    cleanup_definitions(worker.engine, tenant)


def _eval(worker, client, tenant, model="priced"):
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model))
    cases = [{"name": f"c{i}", "input": f"word{i}"} for i in range(2)]
    ev = [{"kind": "contains", "value": "Echo"}, {"kind": "llm_judge", "rubric": "MUST_CONTAIN:word", "model": JUDGE,
                                                  "name": "judge"}]
    suite_id = create_suite(worker.engine, tenant, agent_id, cases, ev)
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION)
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=120)["status"] == "completed"
    return run_id


def test_eval_trace_nests_case_runs_and_judge_requests(worker, client, tenant, defs):
    run_id = _eval(worker, client, tenant)
    tr = trace(worker, tenant, run_id)
    cases, judges, kids = spans(tr, "eval_case"), spans(tr, "judge"), spans(tr, "child_run")
    assert len(cases) == len(judges) == len(kids) == 2
    by_case = {c["id"]: c for c in cases}
    for c in cases:
        assert c["parent_id"] == f"run:{run_id}" and c["status"] == "ok" and c["attributes"]["case_status"] == "passed"
        assert datetime.fromisoformat(c["end"]) >= datetime.fromisoformat(c["start"])
    assert {j["parent_id"] for j in judges} == set(by_case) and {k["parent_id"] for k in kids} == set(by_case)
    for j in judges:
        assert j["start"] == j["end"] and j["attributes"]["model_id"] == "test:judge"
        assert j["attributes"]["usage"]["input_tokens"] == 200 and Decimal(j["attributes"]["cost_usd"]) == Decimal("0.0004")
    t = tr["totals"]
    assert (t["judge_requests"], t["judge_input_tokens"], t["judge_output_tokens"]) == (2, 400, 100)
    assert t["chat_requests"] == 2 and Decimal(t["judge_cost_usd"]) == Decimal("0.0008")
    assert t["input_tokens"] == t["chat_input_tokens"] + t["judge_input_tokens"] + t["memory_input_tokens"] \
        + t["moderation_input_tokens"] + t["embedding_input_tokens"]
    assert tr["spans"][0]["attributes"]["judge_requests"] == 2
    assert TraceReader(worker.engine).read(tenant + "x", run_id) is None


def test_eval_metrics_judge_operation_eval_kind_and_case_run_attribution(worker, client, tenant, defs):
    _eval(worker, client, tenant)
    run(worker, client, tenant, spec_with(model="priced"), "hi")  # a plain agent run
    store = MetricsStore(worker.engine)
    everything = store.summary(mq(tenant))["current"]
    ops = everything["operations"]
    assert ops["judge"]["requests"] == 2 and ops["judge"]["input_tokens"] == 400
    assert ops["chat"]["requests"] == 3  # 2 case runs + the plain run; judge rows are not chat
    assert everything["requests"] == 5
    ev = store.summary(mq(tenant, kind="eval"))["current"]
    assert ev["runs"] == 1 and ev["requests"] == 4  # case-run requests and judge rows
    assert store.summary(mq(tenant, kind="workflow"))["current"]["requests"] == 0
    assert store.summary(mq(tenant, kind="agent"))["current"]["requests"] == 1
    assert store.summary(mq(tenant, operation="judge"))["current"]["requests"] == 2
    assert store.summary(mq(tenant, operation="chat", kind="eval"))["current"]["requests"] == 2
    items = {i["key"]: i for i in store.breakdown(mq(tenant), "operation", sort="requests")}
    assert items["judge"]["requests"] == 2
    assert store.summary(mq(tenant + "x", kind="eval"))["current"]["requests"] == 0
