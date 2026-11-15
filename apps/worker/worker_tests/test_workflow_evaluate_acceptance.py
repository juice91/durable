"""LP.14 acceptance (tester): the evaluate step and refine loops, end to end on the worker.

Adds to the implementer's `test_workflow_evaluate.py`: every AC.11 evaluator kind, the judge's
outcomes (pass, fail, error), judge rows scoped inside a foreach, the refine loop's variants
(judge error, `after` field, quoted rubric), cost (cap, unpriced usage) and cancel.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from eap_worker import workflow_run as wr
from worker_tests.conftest import event_rows, wait_terminal
from worker_tests.test_workflow_evaluate import (  # noqa: F401 - defs is a fixture
    JUDGE_COST,
    REFINE_COST,
    _evaluate,
    _events,
    _judge,
    _refine,
    _ref,
    _run,
    _usage_rows,
    _wf,
    _writer_spec,
    defs,
)
from worker_tests.workflow_run_helpers import publish_agent

DENIED = {"kind": "llm_judge", "rubric": "MUST_CONTAIN:x", "model": {"provider": "openai", "name": "gpt-4o"}}
SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


def _out(run):
    assert run["status"] == "completed", run["error"]
    return json.loads(run["output"])


@pytest.mark.parametrize(
    "evaluator, value, passed",
    [
        ({"kind": "equals_expected"}, "same", True),
        ({"kind": "equals_expected"}, "other", False),
        ({"kind": "contains", "value": "AB", "case_sensitive": False}, "xxabxx", True),
        ({"kind": "contains", "value": "AB"}, "xxabxx", False),
        ({"kind": "regex", "pattern": "^a+b$"}, "aaab", True),
        ({"kind": "regex", "pattern": "^a+b$"}, "bbb", False),
        ({"kind": "schema_valid", "json_schema": SCHEMA}, {"n": 1}, True),
        ({"kind": "schema_valid", "json_schema": SCHEMA}, {"n": "x"}, False),
        ({"kind": "field_equals", "path": "n", "value": 3}, {"n": 3}, True),
        ({"kind": "field_equals", "path": "n", "value": 3}, {"n": 4}, False),
    ],
)
def test_every_deterministic_evaluator_kind_passes_and_fails(worker, client, tenant, defs, evaluator, value, passed):
    spec = _wf(_evaluate([evaluator], expected=_ref("input.want")))
    _id, run = _run(worker, client, tenant, spec, {"text": value, "want": "same"})
    out = _out(run)
    assert out["passed"] is passed and out["results"][0]["passed"] is passed
    assert out["score"] == (1.0 if passed else 0.0)
    assert out["reason"] if not passed else True  # a failing evaluator says why


@pytest.mark.parametrize("evaluator", [{"kind": "max_duration", "seconds": 5}, {"kind": "max_cost", "usd": "1"}])
def test_evaluators_needing_a_run_are_refused_by_the_spec_not_the_runtime(evaluator):
    """An evaluate step has no child run, so no duration or cost: the spec refuses them."""
    from pydantic import ValidationError

    from eap_schemas.workflows import WorkflowSpec

    with pytest.raises(ValidationError, match="not available in an evaluate step"):
        WorkflowSpec.model_validate(_wf(_evaluate([evaluator])))


def test_a_skipped_evaluator_is_left_out_of_the_verdict(worker, client, tenant, defs):
    spec = _wf(_evaluate([{"kind": "equals_expected"}, {"kind": "contains", "value": "h"}], expected=_ref("input.want")))
    _id, run = _run(worker, client, tenant, spec, {"text": "hi", "want": None})  # expected resolves to null
    out = _out(run)
    assert out["passed"] is True
    assert out["results"][0] == {"name": "equals_expected", "kind": "equals_expected", "passed": None,
                                 "score": None, "reason": None, "skipped": True}


def test_output_shape_is_exactly_passed_score_reason_results(worker, client, tenant, defs):
    spec = _wf(_evaluate([{"kind": "contains", "value": "h"}]))
    run_id, run = _run(worker, client, tenant, spec, {"text": "hi"})
    out = _out(run)
    assert set(out) == {"passed", "score", "reason", "results"}
    assert set(out["results"][0]) == {"name", "kind", "passed", "score", "reason"}
    assert _events(worker, run_id)["step:ev:completed"][1]["output"] == out


# ---------------------------------------------------------------------------- the judge


def test_judge_pass_fail_and_error_outcomes_with_their_rows(worker, client, tenant, defs):
    spec = _wf(_evaluate([_judge("MUST_CONTAIN:hi"), _judge("MUST_CONTAIN:zzz"), DENIED], **{"pass": "any"}))
    run_id, run = _run(worker, client, tenant, spec, {"text": "hi there"})
    out = _out(run)  # a failed or erroring judge never fails the step
    passed, failed, errored = out["results"]
    assert (passed["passed"], failed["passed"], errored["passed"]) == (True, False, False)
    assert failed["score"] == 0.0 and failed["reason"]
    assert errored["score"] is None and errored["reason"] == "the judge request failed"
    assert errored["error_type"] and errored["error_code"]
    assert out["passed"] is True  # "any"
    rows = _usage_rows(worker, run_id)  # one priced row per answered judge, none for the refused model
    assert set(rows) == {"judge:ev:0", "judge:ev:1"}
    assert all(r.operation == "chat" and r.cost_usd == JUDGE_COST for r in rows.values())
    assert worker.run_store.run_tree_cost(tenant, run_id) == 2 * JUDGE_COST


def test_a_judge_error_alone_gives_a_failed_verdict_and_no_row(worker, client, tenant, defs):
    run_id, run = _run(worker, client, tenant, _wf(_evaluate([DENIED])), {"text": "x"})
    out = _out(run)
    assert out["passed"] is False and out["reason"] == "the judge request failed" and out["score"] is None
    assert _usage_rows(worker, run_id) == {}


def test_judge_include_input_sees_the_run_input(worker, client, tenant, defs):
    spec = _wf(_evaluate([_judge('MUST_CONTAIN:"hi"', include_input=True)]))
    _id, run = _run(worker, client, tenant, spec, {"text": "hi"})
    assert _out(run)["passed"] is True


def test_judge_rows_are_scoped_inside_a_foreach(worker, client, tenant, defs):
    """Each item scores its own value; its row is `judge:{item scope}:{step}:{k}` on the workflow run."""
    body = [_evaluate([{"kind": "contains", "value": "a"}, _judge('MUST_CONTAIN:"a"')], value="loop.each.item")]
    each = {"id": "each", "type": "foreach", "items": _ref("input.items"), "max_items": 5, "concurrency": 2,
            "body": {"start": "ev", "steps": body}}
    run_id, run = _run(worker, client, tenant, _wf(each), {"items": ["a1", "b2", "a3"]})
    assert run["status"] == "completed", run["error"]
    rows = _usage_rows(worker, run_id)
    assert set(rows) == {f"judge:each.{i}:ev:1" for i in range(3)}  # k = 1: the judge follows the contains
    assert all(r.operation == "chat" for r in rows.values())
    events = _events(worker, run_id)
    verdicts = [events[f"loop:each.{i}:step:ev:completed"][1]["output"]["passed"] for i in range(3)]
    assert verdicts == [True, False, True]
    assert worker.run_store.run_tree_cost(tenant, run_id) == 3 * JUDGE_COST


# ---------------------------------------------------------------------------- refine


def test_refine_judge_error_carries_the_fixed_reason_and_hits_max_iterations(worker, client, tenant, defs):
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    refine = _refine(writer, max_iterations=3)
    refine["body"]["steps"][1]["evaluators"] = [DENIED]
    run_id, run = _run(worker, client, tenant, _wf(refine), {})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["step:refine:failed"][1]["error_code"] == "loop_max_iterations"
    for i in range(3):
        out = events[f"loop:refine.{i}:step:judge:completed"][1]["output"]
        assert out["passed"] is False and out["reason"] == "the judge request failed"
    assert events["loop:refine.2:step:write:started"][1]["input"] == {
        "attempt": 2, "feedback": "the judge request failed"}
    assert _usage_rows(worker, run_id) == {}  # no judge row, but the writers are billed
    assert worker.run_store.run_tree_cost(tenant, run_id) == 3 * REFINE_COST


def test_refine_exits_on_pass_after_exactly_n_iterations(worker, client, tenant, defs):
    """The quoted rubric needle varies: attempt 2 / attempt 4 give 2 / 4 iterations."""
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    for n in (2, 4):
        run_id, run = _run(worker, client, tenant, _wf(_refine(writer, rubric=f'MUST_CONTAIN:"attempt {n}"')), {})
        out = _out(run)
        assert (out["iterations"], out["exited"]) == (n, "condition")
        assert out["last"]["passed"] is True
        events = _events(worker, run_id)
        draft = events[f"loop:refine.{n - 1}:step:write:completed"][1]["output"]
        assert draft == {"draft": f"attempt {n}", "after": "The output does not contain the MUST_CONTAIN marker "
                         f'"attempt {n}".' if n > 1 else ""}
        assert f"loop:refine.{n}:started" not in events
        assert len(_usage_rows(worker, run_id)) == n


def test_refine_second_prompt_carries_the_first_reason_verbatim_in_after(worker, client, tenant, defs):
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    run_id, run = _run(worker, client, tenant, _wf(_refine(writer)), {})
    events = _events(worker, run_id)
    reason0 = events["loop:refine.0:step:judge:completed"][1]["output"]["reason"]
    assert events["loop:refine.1:step:write:completed"][1]["output"]["after"] == reason0
    assert events["loop:refine.0:step:write:completed"][1]["output"]["after"] == ""
    assert _out(run)["last"]["reason"] == 'The output contains the MUST_CONTAIN marker "attempt 3".'


def test_an_unquoted_rubric_needle_stops_at_whitespace_so_it_passes_at_once(worker, client, tenant, defs):
    """Documents the deviation: `MUST_CONTAIN:attempt 3` is the needle `attempt`, true on attempt 1."""
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    _id, run = _run(worker, client, tenant, _wf(_refine(writer, rubric="MUST_CONTAIN:attempt 3")), {})
    assert _out(run)["iterations"] == 1


# ---------------------------------------------------------------------------- cost


def test_a_cap_under_one_attempt_stops_the_refine_loop_before_attempt_two(worker, client, tenant, defs):
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    spec = _wf(_refine(writer), limits={"max_cost_usd": "0.0007"})  # one attempt: 0.00076
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    end = events["end"][1]
    assert end["error_code"] == "run_budget_exceeded" and (end["limit"], end["value"]) == ("max_cost_usd", "0.0007")
    assert Decimal(end["spent_usd"]) == REFINE_COST + JUDGE_COST
    assert "loop:refine.0:completed" in events and "loop:refine.1:started" not in events


def test_a_roomy_cap_does_not_stop_the_refine_loop(worker, client, tenant, defs):
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    _id, run = _run(worker, client, tenant, _wf(_refine(writer), limits={"max_cost_usd": "1"}), {})
    assert _out(run)["iterations"] == 3


def test_the_cap_refuses_a_top_level_evaluate_after_an_agent_spent_it(worker, client, tenant, defs):
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    ask = {"id": "write", "type": "agent", "agent_id": writer, "input": {"p": "go"}, "next": "ev"}
    spec = _wf(ask, _evaluate([_judge("MUST_CONTAIN:x")], value="steps.write.output.draft"),
               limits={"max_cost_usd": "0.0003"})
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["end"][1]["error_code"] == "run_budget_exceeded" and events["step:ev:failed"][1]
    assert _usage_rows(worker, run_id) == {}  # the judge never ran


def test_unpriced_judge_usage_is_recorded_with_no_cost_and_counts_as_zero(worker, client, tenant, defs):
    """A judge request whose model has no price: its row is written (NULL cost, no source), the
    run tree cost stays the priced sum, and nothing fails. Written twice: one row (first write wins)."""
    from eap_worker.pricing import request_usage_input

    run_id, run = _run(worker, client, tenant, _wf(_evaluate([_judge("MUST_CONTAIN:x")])), {"text": "x"})
    before = worker.run_store.run_tree_cost(tenant, run_id)
    assert before == JUDGE_COST
    usage = request_usage_input("x:unpriced", None, _Usage())
    item = [{"request_key": "judge:ev:7", "usage": usage}]
    for _ in range(2):
        assert asyncio.run(wr.judge_usage_step(str(run_id), tenant, item)) == 1
    with worker.engine.connect() as conn:
        rows = conn.execute(text("SELECT cost_usd, price_source, operation FROM run_model_requests "
                                 "WHERE run_id = :r AND request_key = 'judge:ev:7'"), {"r": str(run_id)}).all()
    assert len(rows) == 1 and rows[0][0] is None and rows[0][1] is None and rows[0][2] == "chat"
    assert worker.run_store.run_tree_cost(tenant, run_id) == before


class _Usage:
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens = 50, 10, 0, 0
    details: dict = {}


# ---------------------------------------------------------------------------- cancel


def _cancel_inside_judge(monkeypatch, worker, tenant, holder):
    real = wr.run_judge

    async def judge(evaluator, case, value):
        worker.event_log.append(tenant, holder["run_id"], "cancel", "cancel_requested", {"requested_by": "test"})
        return await real(evaluator, case, value)

    monkeypatch.setattr(wr, "run_judge", judge)


def test_a_cancel_written_during_a_refine_judge_ends_the_run_cancelled(monkeypatch, worker, client, tenant, defs):
    """The judge of iteration 0 is running when the cancel lands. The iteration finishes (the
    cooperative cancel is read at ticks); the tick before iteration 1 stops the run."""
    from worker_tests.test_workflow_loop_tick import _start

    holder: dict = {}
    _cancel_inside_judge(monkeypatch, worker, tenant, holder)
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    run_id = _start(worker, client, tenant, _wf(_refine(writer)), {}, before=lambda r: holder.update(run_id=r))
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    keys = [e[1] for e in event_rows(worker.engine, run_id)]
    by = {e[1]: e[3] for e in event_rows(worker.engine, run_id)}
    assert by["end"]["error_code"] == "run_cancelled" and by["step:refine:failed"]["error_code"] == "run_cancelled"
    assert "loop:refine.0:completed" in keys and "loop:refine.1:started" not in keys
    assert set(_usage_rows(worker, run_id)) == {"judge:refine.0:judge:0"}  # the judge that ran is billed


def test_a_cancel_before_a_top_level_evaluate_with_limits_runs_no_judge(worker, client, tenant, defs):
    from worker_tests.test_workflow_loop_tick import _cancel_requested, _start

    spec = _wf(_evaluate([_judge("MUST_CONTAIN:x")]), limits={"max_cost_usd": "1"})
    run_id = _start(worker, client, tenant, spec, {"text": "x"}, before=_cancel_requested(worker, tenant))
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    by = {e[1]: e[3] for e in event_rows(worker.engine, run_id)}
    assert by["end"]["error_code"] == "run_cancelled" and _usage_rows(worker, run_id) == {}
