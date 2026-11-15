"""AC.17: agent capability spans of the event-derived trace (pure: build_trace on in-memory events).

Fallback attempts and the answering model, tool approval waits, output_retry markers, eval cases
with their judge requests, the waiting run status and the judge totals split.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from eap_run_store.trace import SPAN_KINDS, RunData, TraceInput, build_trace, request_operation

T0 = datetime(2026, 11, 15, 9, 0, 0, tzinfo=timezone.utc)
SECRET = "SECRET-PROVIDER-ERROR"


def _run(**kw):
    base = {
        "id": "r1", "kind": "agent", "status": "completed", "display_status": "completed", "agent_id": "a1",
        "agent_version": 1, "created_at": T0, "started_at": T0, "finished_at": T0 + timedelta(seconds=60),
        "updated_at": T0 + timedelta(seconds=60), "input": "hi", "output": "done",
    }
    base.update(kw)
    return base


def _events(rows, start=T0):
    return [
        {"seq": i + 1, "event_key": k, "type": t, "data": d, "ts": start + timedelta(seconds=10 * (i + 1))}
        for i, (k, t, d) in enumerate(rows)
    ]


def _row(key, run_id="r1", model="test:m", inp=10, out=5, cost="0.001", op="chat"):
    return {
        "run_id": run_id, "request_key": key, "model_id": model, "provider_model_name": None, "input_tokens": inp,
        "output_tokens": out, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None if cost is None else Decimal(cost), "price_source": "override", "operation": op,
    }


def _spans(trace, kind):
    return [s for s in trace["spans"] if s["kind"] == kind]


def test_new_span_kinds_are_declared():
    for kind in ("fallback_attempt", "tool_approval", "output_retry", "eval_case", "judge"):
        assert kind in SPAN_KINDS


def test_fallback_attempts_nest_under_their_request_with_durations():
    attempt_at = T0 + timedelta(seconds=2)
    events = _events([
        ("start", "run_started", {"input": "hi"}),
        ("model:0:final", "model_message", {
            "model_request": 0, "text": "ok",
            "answering_model": "test:b", "requested_model_id": "test:a", "fallback_index": 1,
            "failed_attempts": [{"model_id": "test:a", "outcome": "error", "error_type": "ModelHTTPError",
                                 "http_status": 503}],
            "message": {"parts": [], "failed_attempts": [
                {"model_name": "a", "outcome": "error", "error": f"ModelHTTPError: {SECRET}",
                 "timestamp": attempt_at.isoformat(), "duration": "PT1.5S"},
            ]},
        }),
        ("end", "run_completed", {"output": "ok"}),
    ])
    rows = {"model:0": _row("model:0", model="test:b"), "model:0:failed:0": _row("model:0:failed:0", model="test:a", cost="0.0002")}
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=events, requests=rows)))
    model = _spans(trace, "model_request")[0]
    assert model["attributes"]["answering_model"] == "test:b"
    assert model["attributes"]["model_id"] == "test:b"
    assert model["attributes"]["fallback_index"] == 1
    assert model["attributes"]["requested_model_id"] == "test:a"
    assert model["attributes"]["failed_attempts"] == [
        {"model_id": "test:a", "outcome": "error", "error_type": "ModelHTTPError", "http_status": 503}
    ]
    [attempt] = _spans(trace, "fallback_attempt")
    assert attempt["id"] == "r1:model:0:failed:0"
    assert attempt["parent_id"] == model["id"]
    assert attempt["status"] == "error"
    assert attempt["start"] == attempt_at.isoformat()
    assert attempt["end"] == (attempt_at + timedelta(seconds=1.5)).isoformat()
    assert attempt["attributes"]["timed"] is True
    assert attempt["attributes"]["usage"]["input_tokens"] == 10
    assert Decimal(attempt["attributes"]["cost_usd"]) == Decimal("0.0002")
    assert SECRET not in repr(trace)
    # The failed attempt's billed row is counted in the totals like every usage row.
    assert trace["totals"]["model_requests"] == 2
    assert Decimal(trace["totals"]["cost_usd"]) == Decimal("0.0012")


def test_fallback_attempt_without_stored_message_is_zero_length_and_untimed():
    events = _events([
        ("start", "run_started", {"input": "hi"}),
        ("model:0:final", "model_message", {
            "model_request": 0, "text": "", "blocked": True, "requested_model_id": "test:a", "fallback_index": 1,
            "failed_attempts": [{"model_id": "test:a", "outcome": "error", "error_type": "ModelHTTPError"}],
        }),
        ("end", "run_failed", {"error_code": "output_blocked"}),
    ])
    trace = build_trace(TraceInput(root=RunData(run=_run(status="failed"), events=events, requests={})))
    [attempt] = _spans(trace, "fallback_attempt")
    model = _spans(trace, "model_request")[0]
    assert attempt["attributes"]["timed"] is False
    assert attempt["start"] == attempt["end"] == model["start"]
    assert attempt["attributes"]["usage"] is None


def test_request_without_fallback_has_no_attempts_and_old_shape():
    events = _events([
        ("start", "run_started", {"input": "hi"}),
        ("model:0:final", "model_message", {"model_request": 0, "text": "ok", "message": {"parts": []}}),
        ("end", "run_completed", {"output": "ok"}),
    ])
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=events, requests={"model:0": _row("model:0")})))
    model = _spans(trace, "model_request")[0]
    for k in ("answering_model", "fallback_index", "failed_attempts", "output_candidate"):
        assert k not in model["attributes"]
    assert not _spans(trace, "fallback_attempt")


def _approval_events(decision):
    rows = [
        ("start", "run_started", {"input": "hi"}),
        ("model:0:final", "model_message", {"model_request": 0, "text": "", "message": {"parts": [
            {"part_kind": "tool-call", "tool_name": "send", "tool_call_id": "c1", "args": {"to": "x"}}]}}),
        ("tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "send", "args": {"to": "x"}}),
        ("tool:c1:approval:waiting", "tool_approval_waiting", {
            "tool_call_id": "c1", "tool_name": "send", "args": {"to": "x"}, "deadline": "2026-11-16T09:00:30+00:00",
            "workflow_id": "r1"}),
    ]
    if decision is not None:
        rows.append(("tool:c1:approval:decision", decision[0], {"tool_call_id": "c1", **decision[1]}))
        status = "ok" if decision[0] == "tool_approved" else "denied"
        rows.append(("tool:c1:result", "tool_result", {"tool_call_id": "c1", "tool_name": "send", "status": status,
                                                         "content": "sent" if status == "ok" else "The user denied this tool call."}))
        rows.append(("model:1:final", "model_message", {"model_request": 1, "text": "done", "message": {"parts": []}}))
        rows.append(("end", "run_completed", {"output": "done"}))
    return _events(rows)


def test_approval_wait_span_runs_from_waiting_to_decision():
    events = _approval_events(("tool_approved", {"decided_by": "u1", "comment": "fine"}))
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=events, requests={})))
    [span] = _spans(trace, "tool_approval")
    by_key = {e["event_key"]: e for e in events}
    assert span["id"] == "r1:tool:c1:approval"
    assert span["parent_id"] == "r1:tool:c1"
    assert span["status"] == "ok"
    assert span["start"] == by_key["tool:c1:approval:waiting"]["ts"].isoformat()
    assert span["end"] == by_key["tool:c1:approval:decision"]["ts"].isoformat()
    assert span["attributes"]["decision"]["type"] == "tool_approved"
    assert span["attributes"]["decision"]["decided_by"] == "u1"
    assert span["attributes"]["deadline"] == "2026-11-16T09:00:30+00:00"
    assert span["attributes"]["waited"] is True
    assert "args" not in span["attributes"]
    assert [s["id"] for s in _spans(trace, "tool_approval")] == ["r1:tool:c1:approval"]  # no duplicate


def test_denied_call_is_ok_with_denied_result_status_and_timeout_is_error():
    denied = build_trace(TraceInput(root=RunData(
        run=_run(), events=_approval_events(("tool_denied", {"decided_by": "u1"})), requests={})))
    tool = _spans(denied, "tool_call")[0]
    assert tool["status"] == "ok" and tool["attributes"]["result_status"] == "denied"
    assert _spans(denied, "tool_approval")[0]["status"] == "ok"
    timed_out = build_trace(TraceInput(root=RunData(
        run=_run(), events=_approval_events(("tool_approval_timed_out", {})), requests={})))
    assert _spans(timed_out, "tool_approval")[0]["status"] == "error"


def test_open_approval_is_waiting_and_the_run_span_too():
    run = _run(status="running", display_status="waiting_approval", finished_at=None)
    trace = build_trace(TraceInput(root=RunData(run=run, events=_approval_events(None), requests={})))
    [span] = _spans(trace, "tool_approval")
    assert span["status"] == "waiting" and span["end"] is None
    assert trace["spans"][0]["status"] == "waiting"
    assert trace["spans"][0]["attributes"]["display_status"] == "waiting_approval"
    # A run that ended with the approval still open: incomplete.
    ended = build_trace(TraceInput(root=RunData(run=_run(status="failed"), events=_approval_events(None), requests={})))
    assert _spans(ended, "tool_approval")[0]["status"] == "incomplete"


def test_eval_denial_without_wait_is_a_zero_length_span():
    events = _events([
        ("start", "run_started", {"input": "hi"}),
        ("model:0:final", "model_message", {"model_request": 0, "text": "", "message": {"parts": []}}),
        ("tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "send", "args": {}}),
        ("tool:c1:approval:decision", "tool_denied", {"tool_call_id": "c1", "decided_by": "eval"}),
        ("tool:c1:result", "tool_result", {"tool_call_id": "c1", "tool_name": "send", "status": "denied", "content": "no"}),
        ("end", "run_completed", {"output": "x"}),
    ])
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=events, requests={})))
    [span] = _spans(trace, "tool_approval")
    assert span["start"] == span["end"]
    assert span["attributes"]["waited"] is False
    assert span["attributes"]["decision"]["decided_by"] == "eval"


def test_output_retry_marker_and_candidate_flag():
    events = _events([
        ("start", "run_started", {"input": "hi"}),
        ("model:0:final", "model_message", {"model_request": 0, "text": "", "output_candidate": True,
                                            "message": {"parts": []}}),
        ("output:0:retry", "output_retry", {"model_request": 0, "errors": ["$.title", "$.n"], "cause": "schema"}),
        ("model:1:final", "model_message", {"model_request": 1, "text": "", "output_candidate": True,
                                            "message": {"parts": []}}),
        ("end", "run_completed", {"output": "{}"}),
    ])
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=events, requests={})))
    [retry] = _spans(trace, "output_retry")
    assert retry["id"] == "r1:output:0:retry"
    assert retry["start"] == retry["end"]
    assert retry["attributes"]["errors"] == ["$.title", "$.n"]
    assert retry["attributes"]["error_count"] == 2
    assert retry["attributes"]["model_request"] == 0
    assert all(s["attributes"]["output_candidate"] is True for s in _spans(trace, "model_request"))


def test_eval_case_nests_its_child_run_and_judge_requests():
    case_start = T0 + timedelta(seconds=5)
    child = RunData(
        run=_run(id="c1", parent_run_id="e1", parent_step_id="case:k1", started_at=case_start, created_at=case_start),
        events=_events([
            ("start", "run_started", {"input": "q"}),
            ("model:0:final", "model_message", {"model_request": 0, "text": "a", "message": {"parts": []}}),
            ("end", "run_completed", {"output": "a"}),
        ], start=case_start),
        requests={"model:0": _row("model:0", run_id="c1")},
    )
    eval_events = _events([
        ("start", "run_started", {"kind": "eval"}),
        ("case:k1", "eval_case_result", {
            "case_id": "k1", "position": 0, "status": "passed", "assertions": {"passed": 2, "total": 2},
            "scores": 1, "labels": 0, "evaluator_failures": 0, "duration_ms": 900, "cost_usd": "0.0030000000",
            "error_code": None, "child_run_id": "c1"}),
        ("end", "run_completed", {"output": "{}"}),
    ])
    judge_rows = {
        "judge:k1:1": _row("judge:k1:1", run_id="e1", model="test:judge", inp=50, out=10, cost="0.002"),
        "judge:k1:0": _row("judge:k1:0", run_id="e1", model="test:judge", inp=40, out=8, cost="0.001"),
    }
    root = RunData(run=_run(id="e1", kind="eval", agent_id="a1"), events=eval_events, requests=judge_rows)
    trace = build_trace(TraceInput(root=root, children=[child]))
    [case] = _spans(trace, "eval_case")
    assert case["id"] == "e1:case:k1" and case["parent_id"] == "run:e1"
    assert case["status"] == "ok"
    assert case["start"] == case_start.isoformat()
    assert case["attributes"]["assertions"] == {"passed": 2, "total": 2}
    judges = _spans(trace, "judge")
    assert [j["id"] for j in judges] == ["e1:judge:k1:0", "e1:judge:k1:1"]
    assert all(j["parent_id"] == case["id"] for j in judges)
    assert judges[0]["attributes"]["evaluator_index"] == 0
    assert judges[0]["attributes"]["usage"]["input_tokens"] == 40
    [child_span] = _spans(trace, "child_run")
    assert child_span["parent_id"] == case["id"]
    totals = trace["totals"]
    assert totals["judge_requests"] == 2
    assert totals["judge_input_tokens"] == 90
    assert totals["judge_output_tokens"] == 18
    assert Decimal(totals["judge_cost_usd"]) == Decimal("0.003")
    assert totals["chat_requests"] == 1 and totals["chat_input_tokens"] == 10
    assert totals["input_tokens"] == 100
    split = sum(totals[f"{g}_input_tokens"] or 0 for g in ("chat", "memory", "moderation", "judge"))
    assert split + totals["embedding_input_tokens"] == totals["input_tokens"]


def test_failed_case_is_error_and_unrecorded_children_still_show():
    child = RunData(run=_run(id="c2", parent_run_id="e1", parent_step_id="case:k2", status="running", finished_at=None),
                    events=[], requests={})
    events = _events([
        ("start", "run_started", {"kind": "eval"}),
        ("case:k1", "eval_case_result", {"case_id": "k1", "position": 0, "status": "error", "error_code": "output_invalid"}),
    ])
    root = RunData(run=_run(id="e1", kind="eval", status="running", finished_at=None), events=events, requests={})
    trace = build_trace(TraceInput(root=root, children=[child]))
    [case] = _spans(trace, "eval_case")
    assert case["status"] == "error" and case["attributes"]["error_code"] == "output_invalid"
    [child_span] = _spans(trace, "child_run")
    assert child_span["parent_id"] == "run:e1"


def test_request_operation_judge():
    assert request_operation("judge:k1:0", None) == "judge"
    assert request_operation("model:0:failed:1", None) == "chat"


def test_exhausted_chain_is_an_error_request_with_attempt_spans():
    t_attempt = T0 + timedelta(seconds=12)
    events = _events([
        ("start", "run_started", {"input": "hi"}),
        ("end", "run_failed", {
            "error_code": "fallback_exhausted", "cause": "model_http_error", "model_request": 0,
            "failed_attempts": [
                {"model_id": "test:a", "outcome": "error", "error_type": "ModelHTTPError", "http_status": 503,
                 "timestamp": t_attempt.isoformat(), "duration_ms": 1500},
                {"model_id": "test:b", "outcome": "error", "error_type": "ModelHTTPError", "http_status": 400},
            ],
        }),
    ])
    trace = build_trace(TraceInput(root=RunData(run=_run(status="failed"), events=events, requests={})))
    [model] = _spans(trace, "model_request")
    assert model["id"] == "r1:model:0" and model["status"] == "error"
    assert model["attributes"]["exhausted"] is True and model["attributes"]["usage"] is None
    assert model["attributes"]["requested_model_id"] == "test:a"
    a0, a1 = _spans(trace, "fallback_attempt")
    assert (a0["parent_id"], a1["parent_id"]) == (model["id"], model["id"])
    assert a0["attributes"]["model_id"] == "test:a" and a0["attributes"]["timed"] is True
    assert a0["start"] == t_attempt.isoformat()
    assert a0["end"] == (t_attempt + timedelta(seconds=1.5)).isoformat()
    assert a1["attributes"]["timed"] is False and a1["attributes"]["http_status"] == 400
    assert trace["totals"]["model_requests"] == 0  # nothing answered or billed


def test_older_exhausted_run_without_attempts_has_no_request_span():
    events = _events([
        ("start", "run_started", {"input": "hi"}),
        ("end", "run_failed", {"error_code": "fallback_exhausted", "cause": "model_http_error"}),
    ])
    trace = build_trace(TraceInput(root=RunData(run=_run(status="failed"), events=events, requests={})))
    assert not _spans(trace, "model_request") and not _spans(trace, "fallback_attempt")


def test_malformed_attempt_entry_never_shifts_timings():
    t1 = T0 + timedelta(seconds=3)
    events = _events([
        ("start", "run_started", {"input": "hi"}),
        ("model:0:final", "model_message", {
            "model_request": 0, "text": "ok", "fallback_index": 2,
            "failed_attempts": ["garbage", {"model_id": "test:b", "outcome": "error", "error_type": "X"}],
            "message": {"parts": [], "failed_attempts": [
                {"timestamp": T0.isoformat(), "duration": "PT9S"},
                {"timestamp": t1.isoformat(), "duration": "PT1S"},
            ]},
        }),
        ("end", "run_completed", {"output": "ok"}),
    ])
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=events, requests={})))
    [attempt] = _spans(trace, "fallback_attempt")
    assert attempt["id"] == "r1:model:0:failed:1"
    assert attempt["attributes"]["fallback_position"] == 1
    assert attempt["start"] == t1.isoformat()
    assert attempt["end"] == (t1 + timedelta(seconds=1)).isoformat()
