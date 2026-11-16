"""LP.15: `evaluate` spans and their `judge` spans in the event-derived trace (pure: build_trace on
recorded events in the LP.14 shapes: step output {passed, score, reason, results}, judge usage rows
`judge:{id}:{k}` / `judge:{S}:{id}:{k}` with operation `chat` on the workflow run)."""

from __future__ import annotations

import json
from decimal import Decimal

from eap_schemas.workflow_scope import child_scope, scoped_judge_key

from eap_run_store.trace import SPAN_KINDS, RunData, TraceInput, _judge_rows, build_trace

from .loop_trace_fixtures import RUN, Log, workflow_run

SECRET_REASON = "the draft lacks the marker SECRET-REASON"
SECRET_DRAFT = "SECRET-DRAFT attempt 1"


def _row(key: str, *, cost: str | None = "0.0004", tokens: tuple[int, int] = (50, 10)) -> dict:
    return {
        "run_id": RUN, "request_key": key, "operation": "chat", "model_id": "test:judge",
        "provider_model_name": None, "input_tokens": tokens[0], "output_tokens": tokens[1],
        "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None if cost is None else Decimal(cost), "price_source": "test",
    }


def _output(passed: bool, score: float | None = None) -> dict:
    return {
        "passed": passed,
        "score": score if score is not None else (1.0 if passed else 0.0),
        "reason": None if passed else SECRET_REASON,
        "results": [
            {"name": "contains", "kind": "contains", "passed": True, "score": 1.0, "reason": "contains it"},
            {"name": "judge", "kind": "llm_judge", "passed": passed, "score": 1.0 if passed else 0.0,
             "reason": SECRET_REASON},
            {"name": "equals", "kind": "equals", "passed": None, "score": None, "reason": None, "skipped": True},
        ],
    }


def _trace(events, requests, run=None):
    return build_trace(TraceInput(root=RunData(run=run or workflow_run(), events=events, requests=requests)))


def _by_id(trace):
    return {s["id"]: s for s in trace["spans"]}


def test_evaluate_kind_is_declared():
    assert "evaluate" in SPAN_KINDS


def test_top_level_evaluate_span_has_verdict_results_and_judge_usage():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {"text": SECRET_DRAFT}})
    log.step_started((), "ev", "evaluate")
    log.step_completed((), "ev", _output(False))
    log.add("end", "run_completed", {"output": None})
    requests = {scoped_judge_key((), "ev", 1): _row(scoped_judge_key((), "ev", 1))}
    trace = _trace(log.events, requests)
    spans = _by_id(trace)
    span = spans[f"{RUN}:step:ev"]
    assert span["kind"] == "evaluate" and span["parent_id"] == f"run:{RUN}" and span["status"] == "ok"
    a = span["attributes"]
    assert a["step_type"] == "evaluate" and a["passed"] is False and a["score"] == 0.0
    assert a["evaluators"] == 3 and a["evaluators_passed"] == 1 and a["evaluators_failed"] == 1
    assert a["evaluators_skipped"] == 1
    assert a["results"][1] == {"name": "judge", "kind": "llm_judge", "passed": False, "score": 0.0}
    assert a["results"][2]["skipped"] is True
    assert a["judge_requests"] == 1 and a["judge_input_tokens"] == 50 and a["judge_output_tokens"] == 10
    assert a["judge_cost_usd"] == "0.0004"
    # Attributes never carry text: no reason, no judged value.
    assert SECRET_REASON not in json.dumps(a) and SECRET_DRAFT not in json.dumps(a)
    judge = spans[f"{RUN}:judge:ev:1"]
    assert judge["kind"] == "judge" and judge["parent_id"] == span["id"]
    assert judge["start"] == judge["end"] == span["end"]
    j = judge["attributes"]
    assert j["request_key"] == "judge:ev:1" and j["step_id"] == "ev" and j["evaluator_index"] == 1
    assert j["evaluator_name"] == "judge" and "scope" not in j
    assert j["model_id"] == "test:judge" and j["cost_usd"] == "0.0004"
    assert j["usage"]["input_tokens"] == 50 and j["usage"]["output_tokens"] == 10
    assert SECRET_REASON not in json.dumps(judge)
    # The judge rows count in the totals as judge requests, apart from chat.
    assert trace["totals"]["judge_requests"] == 1 and trace["totals"]["judge_cost_usd"] == "0.0004"
    assert trace["totals"]["chat_requests"] == 0


def test_refine_loop_evaluate_spans_per_iteration_with_scoped_judges():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "refine", "loop")
    requests = {}
    for i in range(3):
        scope = child_scope((), "refine", i)
        log.iteration_started((), "refine", i, state={"feedback": ""})
        log.step_started(scope, "judge", "evaluate")
        last = log.step_completed(scope, "judge", _output(i == 2))["event_key"]
        key = scoped_judge_key(scope, "judge", 1)
        requests[key] = _row(key)
        log.iteration_completed((), "refine", i, last, state={"feedback": ""}, until="true" if i == 2 else "false")
    log.step_completed((), "refine", {"iterations": 3, "exited": "condition", "last": {}, "state": {}})
    log.add("end", "run_completed", {"output": None})
    trace = _trace(log.events, requests)
    spans = _by_id(trace)
    for i in range(3):
        ev = spans[f"{RUN}:step:refine.{i}~judge"]
        assert ev["kind"] == "evaluate" and ev["parent_id"] == f"{RUN}:loop:refine.{i}"
        assert ev["attributes"]["scope"] == f"refine.{i}" and ev["attributes"]["passed"] is (i == 2)
        assert ev["attributes"]["judge_requests"] == 1
        judge = spans[f"{RUN}:judge:refine.{i}:judge:1"]
        assert judge["parent_id"] == ev["id"] and judge["attributes"]["scope"] == f"refine.{i}"
    assert trace["totals"]["judge_requests"] == 3 and trace["totals"]["judge_cost_usd"] == "0.0012"


def test_evaluate_without_judges_and_unpriced_judges():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "a", "evaluate")
    log.step_completed((), "a", {"passed": True, "score": 1.0, "reason": None,
                                 "results": [{"name": "c", "kind": "contains", "passed": True, "score": 1.0}]})
    log.step_started((), "b", "evaluate")
    log.step_completed((), "b", _output(True))
    log.add("end", "run_completed", {"output": None})
    requests = {
        "judge:b:1": _row("judge:b:1", cost=None),
        "judge:b:2": _row("judge:b:2", tokens=(5, 1)),
        # Not this step's: another step id that shares the prefix, and a non-index suffix.
        "judge:bb:1": _row("judge:bb:1"),
        "judge:b:x": _row("judge:b:x"),
    }
    spans = _by_id(_trace(log.events, requests))
    a = spans[f"{RUN}:step:a"]["attributes"]
    assert a["judge_requests"] == 0 and a["judge_cost_usd"] is None and a["judge_input_tokens"] is None
    b = spans[f"{RUN}:step:b"]["attributes"]
    assert b["judge_requests"] == 2 and b["judge_input_tokens"] == 55 and b["judge_output_tokens"] == 11
    assert b["judge_cost_usd"] is None  # one judge row is unpriced
    judges = [s for s in spans.values() if s["kind"] == "judge"]
    assert sorted(s["attributes"]["request_key"] for s in judges) == ["judge:b:1", "judge:b:2"]


def test_failed_and_open_evaluate_steps():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "ev", "evaluate")
    log.step_failed((), "ev", "evaluate_failed")
    log.add("end", "run_failed", {"error_code": "evaluate_failed", "step_id": "ev"})
    span = _by_id(_trace(log.events, {}, workflow_run("failed")))[f"{RUN}:step:ev"]
    assert span["kind"] == "evaluate" and span["status"] == "error"
    assert span["attributes"]["error_code"] == "evaluate_failed" and "passed" not in span["attributes"]

    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "ev", "evaluate")
    span = _by_id(_trace(log.events, {}, workflow_run("running")))[f"{RUN}:step:ev"]
    assert span["kind"] == "evaluate" and span["status"] == "running" and span["end"] is None


def test_malformed_evaluate_output_is_tolerated():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "ev", "evaluate")
    log.step_completed((), "ev", {"passed": "yes", "score": True,
                                  "results": ["x", {"name": {"n": 1}, "kind": "contains", "passed": True}]})
    log.add("end", "run_completed", {"output": None})
    a = _by_id(_trace(log.events, {}))[f"{RUN}:step:ev"]["attributes"]
    assert "passed" not in a and "score" not in a
    # `evaluators` counts the entries read, like the passed/failed/skipped counts (review MINOR 5).
    assert a["results"] == [{"kind": "contains", "passed": True}] and a["evaluators"] == 1
    assert a["evaluators_passed"] == 1 and a["evaluators_failed"] == 0


class _CountingRequests(dict):
    """A requests dict that counts how often it is iterated."""

    scans = 0

    def items(self):  # type: ignore[override]
        type(self).scans += 1
        return super().items()


def test_judge_rows_are_grouped_once_per_run():
    # Review MINOR 4: the run's requests are scanned once, not once per evaluate span.
    loop = child_scope((), "refine", 0)
    keys = [scoped_judge_key((), "ev", 1), scoped_judge_key((), "ev", 0), "judge:ev:x", "judge:evx:0",
            scoped_judge_key(loop, "ev", 0), scoped_judge_key(child_scope((), "refine", 1), "ev", 2), "chat:0"]
    _CountingRequests.scans = 0
    rd = RunData(run=workflow_run(), events=[], requests=_CountingRequests({k: _row(k) for k in keys}))
    assert [i for i, _ in _judge_rows(rd, (), "ev")] == [0, 1]
    assert [r["request_key"] for _, r in _judge_rows(rd, loop, "ev")] == [scoped_judge_key(loop, "ev", 0)]
    assert [i for i, _ in _judge_rows(rd, child_scope((), "refine", 1), "ev")] == [2]
    assert _judge_rows(rd, (), "evx") == [(0, rd.requests["judge:evx:0"])]
    assert _judge_rows(rd, child_scope((), "refine", 9), "ev") == []
    assert _CountingRequests.scans == 1
