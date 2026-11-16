"""LP.14: the `evaluate` step (AC.11's evaluators and `eap-judge`) and refine loops, plus the LP.11
verify MINORs (a nested loop's failed tick inside a foreach item is run-level; the telemetry cancel
check reads the event type).

`test:refine` answers `{"draft": "attempt <attempt + 1>", "after": <feedback>}`; `test:judge`
passes when its `<output>` block contains the rubric's MUST_CONTAIN text. Prices (test price
file): `test:refine` 100 in / 20 out at the `test:*` row (2 / 8 per Mtok) = USD 0.00036 a request;
`test:judge` 200 in / 50 out at 1 / 4 per Mtok = USD 0.0004 a request.
"""

from __future__ import annotations

import asyncio
import json
import pickle
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from eap_worker import workflow_run as wr
from eap_worker.errors import LoopTickFailed, RunFailure, classify
from eap_worker.evaluators import CaseOutcome, PlannedEvaluator, plan_evaluators
from eap_worker.models import scripted_refine_answer
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
)

EVENT = "eap.append_event"
BEGIN = "eap.workflow_step_begin"
SCORE = "eap.workflow_evaluate_score"
JUDGE = "eap-judge__model.request"
USAGE = "eap.workflow_judge_usage"
AGENT = ["eap.workflow_resolve_agent", "eap.agent_run", "DBOS.getResult"]
REFINE_COST = Decimal("0.00036")
JUDGE_COST = Decimal("0.0004")
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"draft": {"type": "string"}, "after": {"type": "string"}},
    "required": ["draft", "after"],
    "additionalProperties": False,
}


def _ref(path):
    return {"$ref": path}


def _wf(*steps, **extra):
    return {"name": "Evaluate", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps),
            **extra}


def _judge(rubric, **kw):
    return {"kind": "llm_judge", "rubric": rubric, "model": {"provider": "test", "name": "judge"}, **kw}


def _evaluate(evaluators, *, sid="ev", value="input.text", nxt=None, **kw):
    return {"id": sid, "type": "evaluate", "value": _ref(value), "evaluators": evaluators, "next": nxt, **kw}


def _refine(writer_id, *, max_iterations=5, rubric='MUST_CONTAIN:"attempt 3"', **kw):
    """The plan's refine loop: a writer, then an evaluate step judging its draft, until it passes."""
    write = {"id": "write", "type": "agent", "agent_id": writer_id,
             "input": {"attempt": _ref("loop.refine.index"), "feedback": _ref("loop.refine.state.feedback")},
             "next": "judge"}
    judge = _evaluate([_judge(rubric)], sid="judge", value="steps.write.output.draft")
    return {"id": "refine", "type": "loop", "max_iterations": max_iterations,
            "until": {"op": "eq", "left": _ref("steps.judge.output.passed"), "right": True},
            "state": {"feedback": ""}, "carry": {"feedback": _ref("steps.judge.output.reason")},
            "body": {"start": "write", "steps": [write, judge]}, **kw}


def _writer_spec():
    spec = agent_spec(model="refine", name="Writer")
    spec["output"] = {"json_schema": DRAFT_SCHEMA}
    return spec


# ---------------------------------------------------------------------------- pure


def _planned(*specs):
    return plan_evaluators([dict(s) for s in specs], ())


def test_evaluate_output_all_any_scores_and_reasons():
    planned = _planned({"kind": "regex", "pattern": "a"}, _judge("r"), {"kind": "contains", "value": "z"})
    outcome = CaseOutcome(
        assertions={"regex": {"value": True, "reason": None},
                    "llm_judge": {"value": False, "reason": "too short"},
                    "contains": {"value": False, "reason": "missing z"}},
        scores={"llm_judge": {"value": 0.25, "reason": "too short"}},
    )
    out = wr.evaluate_output(planned, outcome, "all")
    assert out["passed"] is False
    assert out["score"] == pytest.approx((1.0 + 0.25 + 0.0) / 3)
    assert out["reason"] == "too short\nmissing z"  # the failing evaluators' reasons only
    assert [r["name"] for r in out["results"]] == ["regex", "llm_judge", "contains"]
    assert out["results"][1] == {"name": "llm_judge", "kind": "llm_judge", "passed": False, "score": 0.25,
                                 "reason": "too short"}
    any_ = wr.evaluate_output(planned, outcome, "any")
    assert any_["passed"] is True and any_["reason"] is None  # the passing regex gave no reason


def test_evaluate_output_failures_and_skips():
    planned = _planned({"kind": "regex", "pattern": "a"}, _judge("r"), {"kind": "equals_expected"})
    outcome = CaseOutcome(assertions={"regex": {"value": True, "reason": None}})
    outcome.add_failure(planned[1], RuntimeError("x"), "the judge request failed", error_code="internal_error")
    out = wr.evaluate_output(planned, outcome, "all")
    assert out["passed"] is False and out["reason"] == "the judge request failed"
    assert out["results"][1] == {"name": "llm_judge", "kind": "llm_judge", "passed": False, "score": None,
                                 "reason": "the judge request failed", "error_type": "RuntimeError",
                                 "error_code": "internal_error"}
    assert out["results"][2] == {"name": "equals_expected", "kind": "equals_expected", "passed": None,
                                 "score": None, "reason": None, "skipped": True}
    assert out["score"] == 1.0  # only the regex has a score
    only_skipped = wr.evaluate_output(planned[2:], CaseOutcome(), "all")
    assert only_skipped == {"passed": True, "score": None, "reason": None, "results": [only_skipped["results"][0]]}
    assert wr.evaluate_output(planned[2:], CaseOutcome(), "any")["passed"] is False


def test_refine_answer():
    assert scripted_refine_answer('{"attempt": 2, "feedback": "do better"}') == {
        "draft": "attempt 3", "after": "do better"}
    assert scripted_refine_answer("not json") == {"draft": "attempt 1", "after": ""}
    strict = {"type": "object", "properties": {"draft": {"type": "string"}}, "additionalProperties": False}
    assert scripted_refine_answer('{"attempt": 0}', strict) == {"draft": "attempt 1"}


def test_a_failed_tick_classifies_run_level_with_its_cause():
    from sqlalchemy.exc import OperationalError

    cause = OperationalError("SELECT 1", {}, ConnectionResetError("reset"))
    try:
        try:
            raise cause
        except OperationalError as exc:
            raise LoopTickFailed() from exc
    except LoopTickFailed as exc:
        failure = classify(exc)
    assert (failure.code, failure.status, failure.run_level) == ("internal_error", "failed", True)
    assert failure.error_type == classify(cause).error_type  # the cause's classification, unchanged
    assert classify(LoopTickFailed()).run_level is True
    assert not classify(RuntimeError("x")).run_level
    assert type(pickle.loads(pickle.dumps(LoopTickFailed()))) is LoopTickFailed
    # The marker survives a child result (an iteration's) and is absent otherwise.
    result = wr._failure_result(failure, "x")
    assert result["run_level"] is True and wr._result_failure(result).run_level is True
    plain = wr._failure_result(RunFailure("failed", "tool_failed", "E", "m"), "x")
    assert "run_level" not in plain and wr._result_failure(plain).run_level is False


def test_the_telemetry_cancel_check_reads_the_event_type(monkeypatch):
    class Event:
        def __init__(self, type_):
            self.type = type_

    class Log:
        def __init__(self, event):
            self.event = event

        def get_event(self, tenant_id, run_id, key):
            return self.event

    class Runtime:
        def __init__(self, event):
            self.event_log = Log(event)

    for event, expected in ((None, False), (Event("cancel_requested"), True), (Event("step_started"), False)):
        monkeypatch.setattr(wr, "get_runtime", lambda event=event: Runtime(event))
        assert asyncio.run(wr._run_cancel_requested("r", "t")) is expected


# ---------------------------------------------------------------------------- on the worker


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _run(worker, client, tenant, spec, input_value):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION,
    )
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


def _events(worker, run_id):
    return {k: (t, d) for _, k, t, d in event_rows(worker.engine, run_id)}


def _usage_rows(worker, run_id):
    with worker.engine.connect() as conn:
        return {
            r.request_key: r
            for r in conn.execute(
                text("SELECT request_key, operation, model_id, cost_usd FROM run_model_requests WHERE run_id = :r"),
                {"r": str(run_id)},
            )
        }


@pytest.mark.parametrize("text_, passed", [("hello world", True), ("goodbye", False)])
def test_deterministic_evaluators_without_a_judge(worker, client, worker_settings, tenant, defs, text_, passed):
    spec = _wf(_evaluate(
        [{"kind": "regex", "pattern": "^hello"}, {"kind": "contains"}, {"kind": "equals_expected", "name": "same"}],
        expected=_ref("input.want"),
    ))
    run_id, run = _run(worker, client, tenant, spec, {"text": text_, "want": "hello world"})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert out["passed"] is passed and [r["passed"] for r in out["results"]] == [passed] * 3
    assert out["score"] == (1.0 if passed else 0.0)
    assert [r["name"] for r in out["results"]] == ["regex", "contains", "same"]
    assert _events(worker, run_id)["step:ev:completed"][1]["output"] == out
    assert step_names(worker_settings.dbos_system_schema, run_id) == [
        "eap.start_workflow_run", EVENT, BEGIN, SCORE, EVENT, EVENT, "eap.finish_run"]
    assert _usage_rows(worker, run_id) == {}


def test_pass_any(worker, client, tenant, defs):
    spec = _wf(_evaluate([{"kind": "regex", "pattern": "^x"}, {"kind": "contains", "value": "lo"}], **{"pass": "any"}))
    _run_id, run = _run(worker, client, tenant, spec, {"text": "hello"})
    out = json.loads(run["output"])
    assert out["passed"] is True and [r["passed"] for r in out["results"]] == [False, True]


def test_an_evaluator_that_raises_is_a_failed_result_not_a_failed_step(worker, client, tenant, defs):
    spec = _wf(_evaluate([{"kind": "regex", "pattern": "(a"}, {"kind": "contains", "value": "h"}]))
    _run_id, run = _run(worker, client, tenant, spec, {"text": "hi"})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert out["passed"] is False
    assert out["results"][0]["passed"] is False and out["results"][0]["reason"]
    assert out["reason"] == out["results"][0]["reason"]


def test_a_value_that_does_not_resolve_fails_evaluate_failed(worker, client, tenant, defs):
    spec = _wf(_evaluate([{"kind": "contains", "value": "h"}], value="input.missing"))
    run_id, run = _run(worker, client, tenant, spec, {"text": "hi"})
    assert run["status"] == "failed"
    assert _events(worker, run_id)["step:ev:failed"][1]["error_code"] == "evaluate_failed"


def test_a_top_level_judge_is_recorded_on_the_run_and_ticked_with_limits(
        worker, client, worker_settings, tenant, defs):
    spec = _wf(_evaluate([{"kind": "contains", "value": "h"}, _judge('MUST_CONTAIN:"hi there"')]),
               limits={"max_cost_usd": "1"})
    run_id, run = _run(worker, client, tenant, spec, {"text": "hi there"})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert out["passed"] is True and out["results"][1]["score"] == 1.0
    assert step_names(worker_settings.dbos_system_schema, run_id) == [
        "eap.start_workflow_run", EVENT, BEGIN, "eap.workflow_loop_tick", SCORE, JUDGE, USAGE, EVENT, EVENT,
        "eap.finish_run"]
    rows = _usage_rows(worker, run_id)
    assert set(rows) == {"judge:ev:1"}
    assert (rows["judge:ev:1"].operation, rows["judge:ev:1"].model_id) == ("chat", "test:judge")
    assert rows["judge:ev:1"].cost_usd == JUDGE_COST
    assert worker.run_store.run_tree_cost(tenant, run_id) == JUDGE_COST


def test_refine_passes_on_the_third_attempt(worker, client, worker_settings, tenant, defs):
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    run_id, run = _run(worker, client, tenant, _wf(_refine(writer)), {})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert (out["iterations"], out["exited"]) == (3, "condition")
    assert out["last"]["passed"] is True  # `last`: the judge step ended each iteration's path
    events = _events(worker, run_id)
    drafts = [events[f"loop:refine.{i}:step:write:completed"][1]["output"]["draft"] for i in range(3)]
    assert drafts == ["attempt 1", "attempt 2", "attempt 3"]
    first_reason = events["loop:refine.0:step:judge:completed"][1]["output"]["reason"]
    assert first_reason == 'The output does not contain the MUST_CONTAIN marker "attempt 3".'
    second_prompt = events["loop:refine.1:step:write:started"][1]["input"]
    assert second_prompt == {"attempt": 1, "feedback": first_reason}
    children = {c["parent_step_id"]: c for c in worker.run_store.list_runs(tenant, parent_run_id=run_id)}
    assert json.loads(children["refine.1/write"]["input"])["feedback"] == first_reason  # the writer's prompt
    assert out["state"] == {"feedback": 'The output contains the MUST_CONTAIN marker "attempt 3".'}
    rows = _usage_rows(worker, run_id)  # the judge rows, on the workflow run
    assert set(rows) == {f"judge:refine.{i}:judge:0" for i in range(3)}
    assert all(r.operation == "chat" and r.cost_usd == JUDGE_COST for r in rows.values())
    assert worker.run_store.run_tree_cost(tenant, run_id) == 3 * (REFINE_COST + JUDGE_COST)
    assert step_names(worker_settings.dbos_system_schema, f"{run_id}:loop:refine.0") == [
        EVENT, BEGIN, *AGENT, EVENT, BEGIN, JUDGE, USAGE, EVENT]


def test_refine_max_iterations_two_fails_loop_max_iterations(worker, client, tenant, defs):
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    run_id, run = _run(worker, client, tenant, _wf(_refine(writer, max_iterations=2)), {})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["step:refine:failed"][1]["error_code"] == "loop_max_iterations"
    assert len(_usage_rows(worker, run_id)) == 2


def test_a_cost_cap_below_three_attempts_stops_the_refine_loop(worker, client, tenant, defs):
    """One attempt costs USD 0.00076 (writer + judge): with a cap of 0.001 the tick before attempt
    2 sees 0.00076 and lets it start, the tick before attempt 3 sees 0.00152 and stops the loop.
    Without the judge's rows the cost would be 0.00072 and the third attempt would start."""
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    spec = _wf(_refine(writer), limits={"max_cost_usd": "0.001"})
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "failed" and "cost limit" in run["error"], run["error"]
    events = _events(worker, run_id)
    assert "loop:refine.1:completed" in events and "loop:refine.2:started" not in events
    end = events["end"][1]
    assert (end["error_code"], end["limit"], end["value"]) == ("run_budget_exceeded", "max_cost_usd", "0.001")
    assert Decimal(end["spent_usd"]) == 2 * (REFINE_COST + JUDGE_COST)


# ---------------------------------------------------------------------------- LP.11 verify MINOR 1


def test_a_failed_nested_tick_inside_a_foreach_item_is_never_collected(monkeypatch, worker, client, tenant, defs):
    """Tick 1 is the foreach's initial window; tick 2 is the inner loop's first iteration inside
    item 0. A DB error there fails the run `internal_error` even under `collect`: it is about the
    run, not the item, so the foreach never completes with an error entry in item 0's place."""
    from sqlalchemy.exc import OperationalError

    real = wr.loop_tick_step
    calls = {"n": 0}

    async def tick(run_id, tenant_id, scope_text, step_id, with_cost):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OperationalError("SELECT 1", {}, ConnectionResetError("connection reset by peer"))
        return await real(run_id, tenant_id, scope_text, step_id, with_cost)

    monkeypatch.setattr(wr, "loop_tick_step", tick)
    inner = {"id": "lp", "type": "loop", "max_iterations": 2,
             "until": {"op": "eq", "left": _ref("loop.lp.index"), "right": 1},
             "body": {"start": "x", "steps": [{"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}]}}
    each = {"id": "each", "type": "foreach", "items": _ref("input.items"), "max_items": 3, "on_item_error": "collect",
            "body": {"start": "lp", "steps": [inner]}}
    run_id, run = _run(worker, client, tenant, _wf(each), {"items": ["a", "b", "c"]})
    assert run["status"] == "failed", run
    events = _events(worker, run_id)
    assert "step:each:completed" not in events
    assert events["loop:each.0:failed"][1]["error_code"] == "internal_error"
    assert "loop:each.1:started" not in events  # no item starts after a run-level failure
    assert events["step:each:failed"][1]["error_code"] == "internal_error"
    assert events["end"][1]["error_code"] == "internal_error"
    assert "run_level" not in events["end"][1]  # the marker never reaches an event
