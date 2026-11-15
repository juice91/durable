"""LP.15 acceptance (tester): evaluate spans never carry judge text, scope ids in foreach and nested
loops, judge errors, and the LP.9 caps with many evaluate iterations."""

from __future__ import annotations

import json
from decimal import Decimal

from eap_schemas.workflow_scope import child_scope, scoped_judge_key

from eap_run_store.trace import MAX_LOOP_ITERATION_SPANS, MAX_SPANS, RunData, TraceInput, build_trace

from .loop_trace_fixtures import RUN, Log, workflow_run

RUBRIC = "RUBRIC-TEXT grade harshly"
PROMPT = "JUDGE-PROMPT you are a judge"
REASON = "REASON-TEXT too short"


def _row(key: str, cost: str | None = "0.001") -> dict:
    return {
        "run_id": RUN, "request_key": key, "operation": "chat", "model_id": "test:judge",
        "provider_model_name": None, "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None if cost is None else Decimal(cost), "price_source": "test",
    }


def _trace(log, requests, status="completed"):
    return build_trace(TraceInput(root=RunData(run=workflow_run(status), events=log.events, requests=requests)))


def _kind(t, kind):
    return [s for s in t["spans"] if s["kind"] == kind]


def _ev_output(passed=True):
    return {"passed": passed, "score": 0.5, "reason": REASON, "results": [
        {"name": "judge", "kind": "llm_judge", "passed": passed, "score": 0.5, "reason": REASON,
         "rubric": RUBRIC, "prompt": PROMPT, "instructions": PROMPT, "details": {"rubric": RUBRIC},
         "items": [RUBRIC], "skipped": False, "error_type": None},
    ]}


def test_no_rubric_prompt_or_reason_text_in_any_span_attribute():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "ev", "evaluate")
    log.step_completed((), "ev", _ev_output())
    log.add("end", "run_completed", {"output": None})
    t = _trace(log, {"judge:ev:1": _row("judge:ev:1")})
    for s in _kind(t, "evaluate") + _kind(t, "judge"):
        blob = json.dumps(s["attributes"])
        for text in (RUBRIC, PROMPT, REASON):
            assert text not in blob
    r = _kind(t, "evaluate")[0]["attributes"]["results"][0]
    assert set(r) <= {"name", "kind", "passed", "score", "skipped", "error_type", "error_code"}
    assert "rubric" not in r and "prompt" not in r and "details" not in r and "items" not in r


def test_evaluate_in_foreach_has_scoped_ids_and_judges():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "each", "foreach")
    requests = {}
    for i in range(3):
        s = child_scope((), "each", i)
        log.iteration_started((), "each", i, item=i, foreach=True)
        log.step_started(s, "ev", "evaluate")
        last = log.step_completed(s, "ev", _ev_output(i != 1))["event_key"]
        k = scoped_judge_key(s, "ev", 0)
        requests[k] = _row(k)
        log.iteration_completed((), "each", i, last, foreach=True)
    log.step_completed((), "each", [1, 2, 3])
    log.add("end", "run_completed", {"output": None})
    t = _trace(log, requests)
    by = {s["id"]: s for s in t["spans"]}
    for i in range(3):
        ev = by[f"{RUN}:step:each.{i}~ev"]
        assert ev["kind"] == "evaluate" and ev["parent_id"] == f"{RUN}:loop:each.{i}"
        assert ev["attributes"]["passed"] is (i != 1) and ev["attributes"]["judge_requests"] == 1
        j = by[f"{RUN}:judge:each.{i}:ev:0"]
        assert j["parent_id"] == ev["id"] and j["attributes"]["scope"] == f"each.{i}"
    assert t["totals"]["judge_requests"] == 3


def test_nested_loop_inside_foreach_scope_id():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "each", "foreach")
    o = child_scope((), "each", 1)
    log.iteration_started((), "each", 1, item=1, foreach=True)
    log.step_started(o, "refine", "loop")
    s = child_scope(o, "refine", 0)
    log.iteration_started(o, "refine", 0, state={})
    log.step_started(s, "ev", "evaluate")
    last = log.step_completed(s, "ev", _ev_output())["event_key"]
    log.iteration_completed(o, "refine", 0, last, state={}, until="true")
    done = log.step_completed(o, "refine", {"iterations": 1})
    log.iteration_completed((), "each", 1, done["event_key"], foreach=True)
    log.step_completed((), "each", [])
    log.add("end", "run_completed", {"output": None})
    k = scoped_judge_key(s, "ev", 1)
    t = _trace(log, {k: _row(k)})
    ev = _kind(t, "evaluate")
    assert len(ev) == 1 and ev[0]["id"] == f"{RUN}:step:each.1~refine.0~ev"
    assert ev[0]["attributes"]["scope"] == "each.1/refine.0"
    judge = _kind(t, "judge")
    assert len(judge) == 1 and judge[0]["parent_id"] == ev[0]["id"]


def test_judge_error_result_is_shown_without_text():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "ev", "evaluate")
    log.step_completed((), "ev", {"passed": False, "score": None, "reason": REASON, "results": [
        {"name": "judge", "kind": "llm_judge", "passed": None, "score": None, "reason": REASON,
         "error_type": "ModelHTTPError", "error_code": "judge_failed"}]})
    log.add("end", "run_completed", {"output": None})
    t = _trace(log, {"judge:ev:0": _row("judge:ev:0", cost=None)})
    a = _kind(t, "evaluate")[0]["attributes"]
    assert a["results"] == [{"name": "judge", "kind": "llm_judge", "passed": None, "score": None,
                             "error_type": "ModelHTTPError", "error_code": "judge_failed"}]
    assert "score" not in a and a["passed"] is False
    assert a["evaluators_passed"] == 0 and a["evaluators_failed"] == 0
    assert a["judge_requests"] == 1 and a["judge_cost_usd"] is None
    assert REASON not in json.dumps(a)
    assert len(_kind(t, "judge")) == 1


def test_many_evaluate_iterations_keep_caps_and_totals():
    n = 150
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "loop", "loop")
    requests = {}
    for i in range(n):
        s = child_scope((), "loop", i)
        log.iteration_started((), "loop", i, state={})
        log.step_started(s, "ev", "evaluate")
        last = log.step_completed(s, "ev", _ev_output())["event_key"]
        k = scoped_judge_key(s, "ev", 0)
        requests[k] = _row(k)
        log.iteration_completed((), "loop", i, last, state={}, until="false")
    log.step_completed((), "loop", {"iterations": n})
    log.step_started((), "after", "tool", tool_id="t")
    log.step_completed((), "after", "ok")
    log.add("end", "run_completed", {"output": "ok"})
    t = _trace(log, requests)
    ev, judges = _kind(t, "evaluate"), _kind(t, "judge")
    assert len(t["spans"]) <= MAX_SPANS
    assert 0 < len(ev) <= MAX_LOOP_ITERATION_SPANS and len(judges) == len(ev)
    ids = {s["id"] for s in t["spans"]}
    assert all(j["parent_id"] in ids for j in judges)
    loop = {s["id"]: s for s in t["spans"]}[f"{RUN}:step:loop"]
    assert loop["attributes"]["iterations_omitted"] == n - len(_kind(t, "loop_iteration"))
    assert t["truncated"]["iterations"] > 0
    assert {s["id"]: s for s in t["spans"]}[f"{RUN}:step:after"]["status"] == "ok"
    # Totals cover every judge row, shown or not.
    assert t["totals"]["judge_requests"] == n
    assert Decimal(t["totals"]["judge_cost_usd"]) == Decimal("0.15")
