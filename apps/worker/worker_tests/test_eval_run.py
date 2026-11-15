"""AC.11 eval runtime on the in-process worker: `eap.eval_run` with child agent runs, evaluators,
the `eap-judge` agent, results, events, usage and aggregates."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from eap_worker.eval_run import eval_child_run_id
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.eval_run_helpers import (
    JUDGE,
    child_runs,
    cleanup_evals,
    create_suite,
    results,
    start_eval,
    usage_keys,
)
from worker_tests.test_tool_approval import TICKET_ARGS, gated_spec
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent

SCHEMA = {"type": "object", "properties": {"label": {"type": "string", "enum": ["bug", "question"]}},
          "required": ["label"], "additionalProperties": False}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_evals(worker.engine, tenant)
    cleanup_definitions(worker.engine, tenant)


def _contiguous(events) -> None:
    assert [e[0] for e in events] == list(range(len(events)))
    assert len({e[1] for e in events}) == len(events)


def _run(worker, client, tenant, spec, cases, evaluators, **kwargs):
    agent_id = publish_agent(worker.engine, tenant, spec)
    suite_id = create_suite(worker.engine, tenant, agent_id, cases, evaluators)
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION, **kwargs)
    return agent_id, suite_id, run_id


def test_six_cases_in_batches_of_four_with_judge(worker, client, tenant, defs):
    cases = [{"name": f"case {i}", "input": f"word{i}", "expected_output": f"Echo: word{i}" if i % 2 == 0 else "no"}
             for i in range(6)]
    evaluators = [
        {"kind": "equals_expected"},
        {"kind": "contains", "value": "Echo:"},
        {"kind": "llm_judge", "rubric": "MUST_CONTAIN:word", "model": JUDGE, "name": "judge"},
    ]
    _, suite_id, run_id = _run(worker, client, tenant, agent_spec("echo"), cases, evaluators)
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    assert run["status"] == "completed", run

    rows = results(worker.engine, tenant, run_id)
    assert [r.case_name for r in rows] == [c["name"] for c in cases]
    assert [r.status for r in rows] == ["passed", "failed"] * 3, [(r.assertions, r.evaluator_failures) for r in rows[:1]]
    for i, r in enumerate(rows):
        assert r.output == f"Echo: word{i}"
        assert r.assertions["equals_expected"]["value"] is (i % 2 == 0)
        assert r.assertions["contains"]["value"] is True
        assert r.assertions["judge"]["value"] is True and r.scores["judge"]["value"] == 1.0
        assert r.child_run_id == eval_child_run_id(str(run_id), r.case_id)
        assert r.duration_ms is not None and r.cost_usd is not None

    # One judge usage row per case on the eval run, priced (test:judge: 200 in, 50 out).
    assert usage_keys(worker.engine, run_id) == sorted(f"judge:{r.case_id}:2" for r in rows)
    for r in rows:
        child_cost = worker.run_store.get_run(tenant, r.child_run_id)["cost_usd"]
        assert r.cost_usd == child_cost + Decimal("0.0004")

    # Children: one per case, no conversation (no memory), parent_step_id case:{id}.
    children = child_runs(worker.engine, run_id)
    assert len(children) == 6 and all(c.conversation_id is None for c in children)
    assert {c.parent_step_id for c in children} == {f"case:{r.case_id}" for r in rows}

    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    started = events[0]
    assert started[2] == "run_started" and started[3]["kind"] == "eval" and started[3]["cases"] == 6
    assert started[3]["max_concurrency"] == 4 and started[3]["agent_version"] == 1
    assert [e[1] for e in events[1:-1]] == [f"case:{r.case_id}" for r in rows]
    assert all(e[2] == "eval_case_result" for e in events[1:-1])
    end = events[-1]
    assert end[2] == "run_completed"
    agg = end[3]["aggregates"]
    assert agg["cases"] == 6 and agg["passed"] == 3 and agg["failed"] == 3 and agg["errors"] == 0
    assert agg["pass_rate"] == 0.5 and agg["scores_avg"] == {"judge": 1.0}
    assert json.loads(run["output"]) == agg and run["output_format"] == "json"
    # The eval run's own cost covers its children and its judge rows.
    assert run["cost_usd"] == sum((r.cost_usd for r in rows), Decimal(0))


def test_structured_agent_schema_and_field_evaluators(worker, client, tenant, defs):
    spec = {**agent_spec("structured"), "output": {"json_schema": SCHEMA}}
    cases = [
        {"name": "first enum", "input": "a"},
        {"name": "forced", "input": json.dumps({"output": {"label": "question"}})},
    ]
    evaluators = [{"kind": "schema_valid"}, {"kind": "field_equals", "path": "label", "value": "bug"}]
    _, _, run_id = _run(worker, client, tenant, spec, cases, evaluators)
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=120)["status"] == "completed"
    first, forced = results(worker.engine, tenant, run_id)
    assert first.output == {"label": "bug"} and first.status == "passed"
    assert forced.output == {"label": "question"} and forced.status == "failed"
    assert forced.assertions["schema_valid"]["value"] is True
    assert forced.assertions["field_equals"]["value"] is False


def test_judge_fail_and_judge_model_not_allowed_is_a_failure_entry(worker, client, tenant, defs):
    cases = [{"name": "one", "input": "hello"}]
    evaluators = [
        {"kind": "llm_judge", "rubric": "MUST_CONTAIN:goodbye", "model": JUDGE},
        # Not on the allowlist (EAP_ALLOWED_MODELS defaults to test:*): refused in the judge's model step.
        {"kind": "llm_judge", "rubric": "MUST_CONTAIN:hello", "model": {"provider": "openai", "name": "gpt-4o"}},
    ]
    _, _, run_id = _run(worker, client, tenant, agent_spec("echo"), cases, evaluators)
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=120)["status"] == "completed"
    (row,) = results(worker.engine, tenant, run_id)
    assert row.status == "failed"
    assert row.assertions["llm_judge"]["value"] is False and row.scores["llm_judge"]["value"] == 0.0
    assert row.evaluator_failures == [
        {"name": "llm_judge_2", "kind": "llm_judge", "error_type": "ModelNotAllowed",
         "error": "the judge request failed", "error_code": "model_not_allowed"}
    ]
    assert usage_keys(worker.engine, run_id) == [f"judge:{row.case_id}:0"]


def test_failing_case_is_error_and_the_eval_completes(worker, client, tenant, defs):
    cases = [{"name": "boom", "input": "x"}]
    _, _, run_id = _run(worker, client, tenant, agent_spec("fail-400"), cases,
                        [{"kind": "llm_judge", "rubric": "MUST_CONTAIN:x", "model": JUDGE}])
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    assert run["status"] == "completed"
    (row,) = results(worker.engine, tenant, run_id)
    assert row.status == "error" and row.error_code == "model_http_error"
    assert row.assertions == {} and usage_keys(worker.engine, run_id) == []  # no judge for an error
    assert json.loads(run["output"])["errors"] == 1


def test_gated_tool_is_denied_in_the_child(worker, client, tenant, defs):
    spec = gated_spec()
    cases = [{"name": "ticket", "input": json.dumps({"tool": "tickets.create", "args": TICKET_ARGS})}]
    _, _, run_id = _run(worker, client, tenant, spec, cases, [{"kind": "contains", "value": "outcome=denied"}])
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=120)["status"] == "completed"
    (row,) = results(worker.engine, tenant, run_id)
    assert row.status == "passed", row.output


def test_draft_target(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    draft = agent_spec("echo", name="Draft")
    suite_id = create_suite(worker.engine, tenant, agent_id, [{"name": "d", "input": "hi"}],
                            [{"kind": "equals_expected"}])
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION, version=None, draft_spec=draft)
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    assert run["status"] == "completed"
    (row,) = results(worker.engine, tenant, run_id)
    child = worker.run_store.get_run(tenant, row.child_run_id)
    assert child["draft_spec"] == draft and child["agent_version"] is None
    assert event_rows(worker.engine, run_id)[0][3]["draft"] is True
    # No expected output: equals_expected is skipped (as pydantic_evals), so the case passes.
    assert row.assertions == {} and row.evaluator_failures == [] and row.status == "passed"


def test_a_row_without_a_suite_hash_fails_closed(worker, client, tenant, defs):
    from sqlalchemy import text

    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent_id, [{"name": "a", "input": "one"}], [])
    from eap_run_store.evals import EvalStore

    snapshot_json = EvalStore(worker.engine).snapshot(tenant, suite_id, agent_id=agent_id).canonical_json()
    import uuid

    run_id = uuid.uuid4()
    worker.run_store.create_eval_run(tenant, agent_id, 1, suite_id, snapshot_json, run_id)
    with worker.engine.begin() as conn:  # a row not made by create_eval_run
        conn.execute(text("UPDATE runs SET eval_suite_hash = NULL WHERE id = :r"), {"r": str(run_id)})
    client.enqueue({"queue_name": "eap-agent-runs", "workflow_name": "eap.eval_run", "workflow_id": str(run_id),
                    "app_version": APP_VERSION}, str(run_id), tenant, snapshot_json)
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=60)
    assert run["status"] == "failed"
    end = event_rows(worker.engine, run_id)[-1]
    assert end[2] == "run_failed" and end[3]["error_code"] == "eval_suite_mismatch"
    assert child_runs(worker.engine, run_id) == []


def test_suite_edits_after_creation_do_not_change_the_run(worker, client, tenant, defs):
    from eap_run_store.evals import EvalStore

    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent_id, [{"name": "a", "input": "one"}],
                            [{"kind": "contains", "value": "one"}])
    store = EvalStore(worker.engine)
    snapshot_json = store.snapshot(tenant, suite_id, agent_id=agent_id).canonical_json()
    store.add_case(tenant, suite_id, {"name": "b", "input": "two"})
    store.update_suite(tenant, suite_id, evaluators=[{"kind": "contains", "value": "nothing"}])
    import uuid

    run_id = uuid.uuid4()
    worker.run_store.create_eval_run(tenant, agent_id, 1, suite_id, snapshot_json, run_id)
    client.enqueue({"queue_name": "eap-agent-runs", "workflow_name": "eap.eval_run", "workflow_id": str(run_id),
                    "app_version": APP_VERSION}, str(run_id), tenant, snapshot_json)
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=120)["status"] == "completed"
    rows = results(worker.engine, tenant, run_id)
    assert [(r.case_name, r.status) for r in rows] == [("a", "passed")]


def test_input_that_differs_from_the_recorded_suite_fails(worker, client, tenant, defs):
    from eap_run_store.evals import EvalStore

    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent_id, [{"name": "a", "input": "one"}], [])
    other = EvalStore(worker.engine).snapshot(tenant, suite_id, agent_id=agent_id)
    tampered = other.model_copy(update={"cases": (other.cases[0].model_copy(update={"input": "two"}),)})
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION, input_json=tampered.canonical_json())
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=60)
    assert run["status"] == "failed"
    end = event_rows(worker.engine, run_id)[-1]
    assert end[2] == "run_failed" and end[3]["error_code"] == "eval_suite_mismatch"
    assert results(worker.engine, tenant, run_id) == [] and child_runs(worker.engine, run_id) == []
